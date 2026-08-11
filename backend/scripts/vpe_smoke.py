# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Exercises the VPE client against the live API from an allowlisted project.

Nothing else in this repository can call VPE: the programme is allowlist
gated, so the request builders are verified only against the documented
samples. This script is how that verification becomes real. It deliberately
goes through ``payloads.build_payload`` and ``VpeClient`` rather than
hand-rolled curl, so a green run says the shipped code is correct rather
than merely that the service works.

Run the checks first, they cost nothing:

    python -m scripts.vpe_smoke check --project P --bucket gs://B

Then a single capability, or everything:

    python -m scripts.vpe_smoke run --project P --bucket gs://B --only upscale
    python -m scripts.vpe_smoke run --project P --bucket gs://B --all

``--print-curl`` renders the request without sending it, which needs no
allowlist and is the fastest way to see exactly what would go on the wire.

Every attempt is written to the report directory as request JSON, response
JSON and a row in results.csv, because a failure body is as useful as a
success here: the documented errors are the only description we have of how
this service rejects things.
"""

from __future__ import annotations

import argparse
import csv
import json
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

# pylint: disable=wrong-import-position
from src.videos.vpe.capabilities import (  # noqa: E402
    VPE_CAPABILITY_LIST,
    VpeCapabilityId,
    get_capability,
)
from src.videos.vpe.client import VpeClient  # noqa: E402
from src.videos.vpe.payloads import (  # noqa: E402
    VpeConditioningFrame,
    VpeMediaRef,
    VpeRequest,
    VpeSeamlessFlags,
    build_payload,
)

MP4 = "video/mp4"
PNG = "image/png"
WAV = "audio/wav"

# The service agents VPE needs. The last two do not exist until a batch
# prediction job and a tuning job have each been started once in the
# project, which is why a missing one reports as "principal does not exist"
# rather than as a permission error.
_SERVICE_AGENTS = (
    "gcp-sa-aiplatform",
    "gcp-sa-vertex-bp",
    "gcp-sa-vertex-tune",
)


def _run(command: Sequence[str]) -> tuple[int, str]:
    """Runs a command, returning its status and combined output."""
    done = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, (done.stdout + done.stderr).strip()


@dataclass
class Fixture:
    """A local file to be generated and uploaded before a job can run."""

    name: str
    ffmpeg_args: list[str]
    wants: str = ""

    def build(self, into: Path, supplied: Path | None = None) -> Path:
        """Provides the fixture, preferring real media over a test pattern.

        The generated defaults are ffmpeg test patterns and a sine tone.
        They prove a payload is accepted, which is all a smoke test needs,
        but they cannot show whether a capability does anything useful: a
        colour-bar plate has no face to animate and a tone has nothing to
        lip-sync to. Where a real clip or frame is supplied it is used
        instead, and the run becomes a judgement of output quality rather
        than only of wire correctness.

        Args:
            into: Directory to render or copy into.
            supplied: A real file to use in place of the generated one.

        Returns:
            The path to use.

        Raises:
            RuntimeError: If ffmpeg fails to render the fallback.
        """
        path = into / self.name
        if supplied is not None:
            # Copied rather than referenced so the report holds exactly what
            # was sent, even if the original moves later.
            shutil.copyfile(supplied, path)
            return path
        if path.exists():
            return path
        code, out = _run(
            ["ffmpeg", "-y", "-v", "error", *self.ffmpeg_args, str(path)],
        )
        if code != 0:
            raise RuntimeError(f"ffmpeg failed for {self.name}: {out}")
        return path


# Every capability states exact input requirements, and violating them is
# reported as a capacity error rather than as a validation error, so the
# fixtures are generated to spec rather than reused from the gallery.
# 24 fps throughout; frame counts chosen to sit inside each documented bound.
_LANDSCAPE = "1280x720"
_UPSCALE_SRC = Fixture(
    "upscale_src.mp4",
    # 6s: inside the upscaler's 4-8s window.
    [
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={_LANDSCAPE}:rate=24:duration=6",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
    ],
    wants="a 24fps clip of 4-8s, 720p or 1080p, either orientation",
)
_TRANSFORM_SRC = Fixture(
    "transform_src.mp4",
    [
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={_LANDSCAPE}:rate=24:duration=8",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
    ],
    wants="a 24fps landscape 1280x720 clip of up to 8s",
)
_MASK_SRC = Fixture(
    "mask.mp4",
    # A grayscale mask matching the source's size and length.
    [
        "-f",
        "lavfi",
        "-i",
        f"color=c=gray:size={_LANDSCAPE}:rate=24:duration=8",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
    ],
    wants="a greyscale mask video matching transform_src.mp4 in size and length",
)
_PERF_SRC = Fixture(
    "perf_actor.mp4",
    # Performance estimation demands exactly 192 frames.
    [
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={_LANDSCAPE}:rate=24:duration=8",
        "-frames:v",
        "192",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
    ],
    wants="a 24fps landscape 1280x720 clip of an actor, exactly 192 frames",
)
_STILL = Fixture(
    "still.png",
    ["-f", "lavfi", "-i", f"testsrc=size={_LANDSCAPE}", "-frames:v", "1"],
    wants="a 1280x720 still, ideally a character close-up for lip-sync",
)
_VOICE = Fixture(
    "voice.wav",
    [
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=220:duration=8",
        "-ac",
        "2",
        "-ar",
        "48000",
    ],
    wants="up to 8s of real recorded speech - a tone proves nothing about lip-sync",
)
_PORTRAIT = Fixture(
    "portrait_src.mp4",
    # For the orientation probe: the one question the docs cannot answer.
    [
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=720x1280:rate=24:duration=6",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
    ],
    wants="a 24fps 720x1280 vertical clip of 4-8s",
)


@dataclass
class Case:
    """One capability exercised with one request."""

    key: str
    capability_id: VpeCapabilityId
    fixtures: tuple[Fixture, ...]
    make_request: Callable[[dict[str, str], str], VpeRequest]
    note: str = ""
    # Cases whose input is another case's output run after it.
    depends_on: str | None = None
    feeds: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _cases() -> list[Case]:
    """Builds one case per capability, in dependency order."""

    def upscale(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=out,
            video=VpeMediaRef(gcs_uri=uris["upscale_src.mp4"], mime_type=MP4),
            resolution="4k",
            aspect_ratio="16:9",
        )

    def upscale_portrait(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=out,
            video=VpeMediaRef(gcs_uri=uris["portrait_src.mp4"], mime_type=MP4),
            resolution="4k",
            aspect_ratio="9:16",
        )

    def omni_cine(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=out,
            prompt="A calm wide shot. Keep everything else the same.",
            video=VpeMediaRef(gcs_uri=uris["transform_src.mp4"], mime_type=MP4),
        )

    def omni_cine_portrait(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=out,
            prompt="A calm wide shot. Keep everything else the same.",
            video=VpeMediaRef(gcs_uri=uris["portrait_src.mp4"], mime_type=MP4),
        )

    def transform(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=out,
            prompt="A cinematic tracking shot across a sunlit stone terrace.",
            video=VpeMediaRef(gcs_uri=uris["transform_src.mp4"], mime_type=MP4),
            video_transform_strength=0.6,
            num_diffusion_steps=20,
            seed=777,
        )

    def transform_masked(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=out,
            prompt="A cinematic tracking shot across a sunlit stone terrace.",
            video=VpeMediaRef(gcs_uri=uris["transform_src.mp4"], mime_type=MP4),
            video_transform_mask_gcs_uri=uris["mask.mp4"],
            video_transform_strength=0.6,
            num_diffusion_steps=20,
            seed=777,
        )

    def transform_keyframes(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=out,
            prompt="A slow push in across a sunlit stone terrace.",
            image=VpeMediaRef(gcs_uri=uris["still.png"], mime_type=PNG),
            conditioning_frames=(
                VpeConditioningFrame(
                    image=VpeMediaRef(gcs_uri=uris["still.png"], mime_type=PNG),
                    frame_num=24,
                ),
            ),
            seed=777,
        )

    def perf_estimation(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.PERF_ESTIMATION,
            storage_uri=out,
            video=VpeMediaRef(gcs_uri=uris["perf_actor.mp4"], mime_type=MP4),
            seed=777,
        )

    def perf_generation(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.PERF_GENERATION,
            storage_uri=out,
            prompt="A cinematic character performing in a studio.",
            reference_images=(
                VpeMediaRef(gcs_uri=uris["still.png"], mime_type=PNG),
            ),
            perf_mesh_gcs_uri=uris.get("__blue_mesh__", ""),
            seed=78,
        )

    def dialogue(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
            storage_uri=out,
            prompt="A person speaking to camera, static camera.",
            image=VpeMediaRef(gcs_uri=uris["still.png"], mime_type=PNG),
            reference_audios=(
                VpeMediaRef(gcs_uri=uris["voice.wav"], mime_type=WAV),
            ),
        )

    def textures(_uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TEXTURES,
            storage_uri=out,
            prompt="Slow drifting clouds, seamless repeating pattern.",
            seed=42,
            seamless=VpeSeamlessFlags(
                loop=True,
                tessellate_horizontal=True,
                tessellate_vertical=True,
            ),
        )

    def upscale_seamless(uris: dict[str, str], out: str) -> VpeRequest:
        return VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
            storage_uri=out,
            video=VpeMediaRef(
                gcs_uri=uris.get("__texture__", ""), mime_type=MP4
            ),
            resolution="4k",
            aspect_ratio="16:9",
            seamless=VpeSeamlessFlags(
                loop=True,
                tessellate_horizontal=True,
                tessellate_vertical=True,
            ),
        )

    return [
        Case(
            "upscale",
            VpeCapabilityId.UPSCALE,
            (_UPSCALE_SRC,),
            upscale,
            "The Phase 1 capability. 6s landscape, 24fps.",
        ),
        Case(
            "upscale_portrait",
            VpeCapabilityId.UPSCALE,
            (_PORTRAIT,),
            upscale_portrait,
            "Confirms the documented portrait support really works.",
        ),
        Case(
            "omni_cine",
            VpeCapabilityId.OMNI_CINE,
            (_TRANSFORM_SRC,),
            omni_cine,
            "Landscape baseline.",
        ),
        Case(
            "omni_cine_portrait",
            VpeCapabilityId.OMNI_CINE,
            (_PORTRAIT,),
            omni_cine_portrait,
            "ORIENTATION PROBE: docs never say landscape-only and expose no "
            "aspectRatio. Check whether the output is genuinely 9:16 or a "
            "pillarboxed 1280x720.",
        ),
        Case(
            "video_transform",
            VpeCapabilityId.VIDEO_TRANSFORM,
            (_TRANSFORM_SRC,),
            transform,
            "Whole-frame restyle.",
        ),
        Case(
            "video_transform_masked",
            VpeCapabilityId.VIDEO_TRANSFORM,
            (_TRANSFORM_SRC, _MASK_SRC),
            transform_masked,
            "Localised edit via a grayscale mask video.",
        ),
        Case(
            "video_transform_keyframes",
            VpeCapabilityId.VIDEO_TRANSFORM,
            (_STILL,),
            transform_keyframes,
            "Multi-keyframe I2V; frameNum must be a multiple of 8.",
        ),
        Case(
            "perf_estimation",
            VpeCapabilityId.PERF_ESTIMATION,
            (_PERF_SRC,),
            perf_estimation,
            "Produces the blue mesh.",
            feeds="__blue_mesh__",
        ),
        Case(
            "perf_generation",
            VpeCapabilityId.PERF_GENERATION,
            (_STILL,),
            perf_generation,
            "Consumes the blue mesh.",
            depends_on="perf_estimation",
        ),
        Case(
            "dialogue_driven",
            VpeCapabilityId.DIALOGUE_DRIVEN,
            (_STILL, _VOICE),
            dialogue,
            "Highest creative value: lip-sync to a supplied track.",
        ),
        Case(
            "video_textures",
            VpeCapabilityId.VIDEO_TEXTURES,
            (),
            textures,
            "Seamless loop plus tessellation.",
            feeds="__texture__",
        ),
        Case(
            "upscale_seamless",
            VpeCapabilityId.UPSCALE_SEAMLESS,
            (),
            upscale_seamless,
            "Flags must match the source generation or seams appear.",
            depends_on="video_textures",
        ),
    ]


def cmd_check(args: argparse.Namespace) -> int:
    """Verifies the environment before anything is spent."""
    project = args.project
    bucket = args.bucket.rstrip("/")
    ok = True

    print(f"project : {project}")
    print(f"bucket  : {bucket}\n")

    code, out = _run(
        [
            "gcloud",
            "projects",
            "describe",
            project,
            "--format=value(projectNumber)",
        ]
    )
    if code != 0:
        print(f"  FAIL  cannot read the project: {out.splitlines()[0][:120]}")
        return 1
    number = out.strip()
    print(f"  ok    project number {number}")

    code, out = _run(
        [
            "gcloud",
            "services",
            "list",
            "--enabled",
            f"--project={project}",
            "--format=value(config.name)",
        ]
    )
    enabled = set(out.split())
    for api in ("aiplatform.googleapis.com", "storage.googleapis.com"):
        if api in enabled:
            print(f"  ok    {api} enabled")
        else:
            ok = False
            print(f"  FAIL  {api} NOT enabled -> gcloud services enable {api}")

    code, out = _run(
        [
            "gcloud",
            "storage",
            "buckets",
            "describe",
            bucket,
            "--format=value(name)",
        ]
    )
    if code == 0:
        print(f"  ok    bucket reachable")
    else:
        ok = False
        print(f"  FAIL  bucket unreachable: {out.splitlines()[0][:120]}")

    code, policy = _run(
        [
            "gcloud",
            "storage",
            "buckets",
            "get-iam-policy",
            bucket,
            "--format=json",
        ]
    )
    for agent in _SERVICE_AGENTS:
        member = f"service-{number}@{agent}.iam.gserviceaccount.com"
        if member in policy:
            print(f"  ok    {agent} has a binding on the bucket")
        else:
            ok = False
            print(
                f"  FAIL  {agent} has NO binding. If this principal does "
                f"not exist yet, start and immediately cancel a batch "
                f"prediction (bp) or tuning (tune) job first, then:"
            )
            print(
                f"          gcloud storage buckets add-iam-policy-binding "
                f"{bucket} \\\n            --member=serviceAccount:{member} "
                f"\\\n            --role=roles/storage.admin"
            )

    print(
        "\nallowlist probe (a 400 means you ARE allowlisted; 403/404 means "
        "you are not):"
    )
    client = VpeClient(
        project_id=project, location=args.location, dry_run=False
    )
    url = client.endpoint_url("predictLongRunning")
    print(f"  {url}")
    # Application default credentials, not the gcloud user login: VpeClient
    # authenticates through google.auth.default(), so probing with a different
    # identity can report allowlisted when the code path is not, or the
    # reverse. Fall back to the user login only if ADC is absent.
    code, token_out = _run(
        ["gcloud", "auth", "application-default", "print-access-token"],
    )
    if code != 0 or not token_out.strip():
        code, token_out = _run(["gcloud", "auth", "print-access-token"])
    if code == 0:
        # gcloud prefixes warnings onto stdout often enough that the token has
        # to be taken as the last line rather than the whole output.
        token = token_out.strip().splitlines()[-1].strip()
        probe = [
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-X",
            "POST",
            url,
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: application/json",
            "-H",
            "X-Vertex-AI-LLM-Request-Type: shared",
            "-d",
            '{"instances":[{}],"parameters":{}}',
        ]
        _, status = _run(probe)
        verdict = {
            "400": "ALLOWLISTED (reached the model)",
            "403": "NOT allowlisted",
            "404": "NOT allowlisted (model not found)",
        }.get(status.strip(), "unexpected")
        print(f"  HTTP {status.strip()} -> {verdict}")
        ok = ok and status.strip() == "400"

    print("\n" + ("all checks passed" if ok else "FIX THE FAILURES ABOVE"))
    return 0 if ok else 1


def _supplied_media(media_dir: str | None, fixture: Fixture) -> Path | None:
    """Finds a real file to use in place of a generated fixture.

    Matched by filename so the mapping is visible in a directory listing
    rather than buried in a flag. Anything absent falls back to the
    generated pattern, so a partial set is fine - supplying only a real
    voice track and a real face still makes the lip-sync result meaningful
    while everything else stays synthetic.

    Args:
        media_dir: Directory of real media, or None.
        fixture: The fixture wanting a file.

    Returns:
        The path to use, or None to generate.
    """
    if not media_dir:
        return None
    candidate = Path(media_dir) / fixture.name
    return candidate if candidate.is_file() else None


def cmd_media(args: argparse.Namespace) -> int:
    """Lists the real media the run can use, and what each slot wants."""
    supplied = Path(args.media_dir) if args.media_dir else None
    print(
        "Real media beats the generated test patterns wherever output "
        "quality matters.\nDrop a file with the matching name into "
        "--media-dir; anything missing is generated.\n",
    )
    for fixture in (
        _UPSCALE_SRC,
        _PORTRAIT,
        _TRANSFORM_SRC,
        _MASK_SRC,
        _PERF_SRC,
        _STILL,
        _VOICE,
    ):
        found = ""
        if supplied is not None:
            path = supplied / fixture.name
            found = "  <- SUPPLIED" if path.is_file() else "  (generated)"
        print(f"  {fixture.name:20} {fixture.wants}{found}")
    return 0


def _upload(local: Path, bucket: str, prefix: str) -> str:
    """Copies a fixture into the VPE bucket, returning its URI."""
    uri = f"{bucket.rstrip('/')}/{prefix}/{local.name}"
    code, out = _run(["gcloud", "storage", "cp", str(local), uri])
    if code != 0:
        raise RuntimeError(f"upload failed for {local.name}: {out}")
    return uri


def cmd_run(args: argparse.Namespace) -> int:
    """Builds, sends and records one or many capability calls."""
    bucket = args.bucket.rstrip("/")
    report = Path(args.report_dir)
    (report / "requests").mkdir(parents=True, exist_ok=True)
    (report / "responses").mkdir(parents=True, exist_ok=True)
    fixtures_dir = report / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    selected = _cases()
    if args.only:
        wanted = set(args.only)
        selected = [c for c in selected if c.key in wanted]
        if not selected:
            print(
                f"no case matches {sorted(wanted)}; known: "
                f"{[c.key for c in _cases()]}"
            )
            return 2

    client = VpeClient(
        project_id=args.project, location=args.location, dry_run=args.print_curl
    )

    uris: dict[str, str] = {}
    if args.print_curl:
        # Chained cases normally take these from the preceding job's output.
        # A printed run has no preceding job, so stand in a plausible URI:
        # without one the builder rightly refuses an empty gcsUri and the two
        # chained payloads - the ones worth inspecting most - never render.
        uris["__blue_mesh__"] = (
            f"{bucket}/vpe-smoke/out/perf_estimation/" "sample_0.mp4"
        )
        uris["__texture__"] = (
            f"{bucket}/vpe-smoke/out/video_textures/" "sample_0.mp4"
        )
    rows: list[dict[str, Any]] = []
    csv_path = report / "results.csv"

    for case in selected:
        print(f"\n=== {case.key} ({case.capability_id.value}) ===")
        if case.note:
            print(f"    {case.note}")
        # A printed run never sends anything, so a dependency can only ever
        # be PRINTED; requiring SUCCESS there would hide the two chained
        # payloads, which are the ones most worth eyeballing before a live run.
        satisfied = {"SUCCESS", "PRINTED"} if args.print_curl else {"SUCCESS"}
        if case.depends_on and case.depends_on not in {
            r["case"] for r in rows if r["status"] in satisfied
        }:
            print(f"    SKIPPED: needs {case.depends_on} to have succeeded")
            rows.append(
                {
                    "case": case.key,
                    "model": case.capability_id.value,
                    "status": "SKIPPED",
                    "detail": f"depends on {case.depends_on}",
                    "seconds": 0,
                    "output": "",
                }
            )
            _write_csv(csv_path, rows)
            continue

        try:
            for fixture in case.fixtures:
                local = fixture.build(
                    fixtures_dir,
                    _supplied_media(args.media_dir, fixture),
                )
                if fixture.name not in uris and not args.print_curl:
                    uris[fixture.name] = _upload(local, bucket, "vpe-smoke/in")
                elif args.print_curl:
                    uris.setdefault(
                        fixture.name, f"{bucket}/vpe-smoke/in/{fixture.name}"
                    )

            out_dir = f"{bucket}/vpe-smoke/out/{case.key}/"
            request = case.make_request(uris, out_dir)
            payload = build_payload(request)
        except Exception as exc:  # noqa: BLE001 - report, never abort the run
            print(f"    BUILD FAILED: {exc}")
            rows.append(
                {
                    "case": case.key,
                    "model": case.capability_id.value,
                    "status": "BUILD_FAILED",
                    "detail": str(exc),
                    "seconds": 0,
                    "output": "",
                }
            )
            _write_csv(csv_path, rows)
            continue

        (report / "requests" / f"{case.key}.json").write_text(
            json.dumps(payload, indent=2)
        )

        if args.print_curl:
            print(_as_curl(client, payload))
            rows.append(
                {
                    "case": case.key,
                    "model": case.capability_id.value,
                    "status": "PRINTED",
                    "detail": "",
                    "seconds": 0,
                    "output": "",
                }
            )
            _write_csv(csv_path, rows)
            continue

        started = time.time()
        try:
            operation = client.submit(payload)
            print(f"    operation {operation.operation_id}")
            result = client.wait_for_completion_sync(
                operation,
                timeout_seconds=args.timeout,
                on_poll=lambda _op: print("    still running...", flush=True),
            )
            elapsed = round(time.time() - started, 1)
            uri = _result_uri(result)
            if case.feeds:
                uris[case.feeds] = uri
            print(f"    SUCCESS in {elapsed}s -> {uri}")
            (report / "responses" / f"{case.key}.json").write_text(
                json.dumps(_jsonable(result), indent=2, default=str)
            )
            rows.append(
                {
                    "case": case.key,
                    "model": case.capability_id.value,
                    "status": "SUCCESS",
                    "detail": "",
                    "seconds": elapsed,
                    "output": uri,
                }
            )
        except Exception as exc:  # noqa: BLE001 - a failure body is the point
            elapsed = round(time.time() - started, 1)
            print(f"    FAILED in {elapsed}s: {type(exc).__name__}: {exc}")
            (report / "responses" / f"{case.key}.error.txt").write_text(
                f"{type(exc).__name__}: {exc}"
            )
            rows.append(
                {
                    "case": case.key,
                    "model": case.capability_id.value,
                    "status": "FAILED",
                    "detail": f"{type(exc).__name__}: {exc}"[:500],
                    "seconds": elapsed,
                    "output": "",
                }
            )
        _write_csv(csv_path, rows)

    print(f"\nreport written to {report}/")
    print(f"  results.csv, requests/*.json, responses/*")
    return 0


def _result_uri(result: Any) -> str:
    """Best-effort extraction of the output location."""
    for attr in ("output_directory_uri", "directory_uri"):
        value = getattr(result, attr, None)
        if value:
            return str(value)
    return str(getattr(result, "raw", "") or "")[:200]


def _jsonable(value: Any) -> Any:
    """Renders a result object for the report."""
    for attr in ("raw", "body", "__dict__"):
        found = getattr(value, attr, None)
        if found:
            return found
    return str(value)


def _as_curl(client: VpeClient, payload: dict[str, Any]) -> str:
    """Renders the request as a copy-pasteable curl command."""
    url = client.endpoint_url("predictLongRunning")
    body = json.dumps(payload)
    return (
        "curl -X POST \\\n"
        f"  {shlex.quote(url)} \\\n"
        '  -H "Authorization: Bearer $(gcloud auth print-access-token)" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -H "X-Vertex-AI-LLM-Request-Type: shared" \\\n'
        f"  -d {shlex.quote(body)}"
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Rewrites the results file after every case, so a crash keeps data."""
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        prog="vpe_smoke",
        description="Exercise the VPE client against an allowlisted project.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--project", required=True)
        target.add_argument(
            "--bucket",
            required=True,
            help="gs://bucket inside the allowlisted project",
        )
        target.add_argument("--location", default="us-central1")

    check = sub.add_parser("check", help="verify prerequisites, spend nothing")
    common(check)
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="build, send and record capability calls")
    common(run)
    run.add_argument(
        "--only",
        nargs="*",
        metavar="CASE",
        help=f"one or more of: " f"{', '.join(c.key for c in _cases())}",
    )
    run.add_argument(
        "--all",
        action="store_true",
        help="every case (the default when --only is omitted)",
    )
    run.add_argument(
        "--print-curl",
        action="store_true",
        help="render requests without sending; needs no " "allowlist",
    )
    run.add_argument(
        "--media-dir",
        default=None,
        help="directory of real media to use instead of generated test "
        "patterns; see the 'media' subcommand for the filenames",
    )
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--report-dir", default="vpe_smoke_out")
    run.set_defaults(func=cmd_run)

    media = sub.add_parser(
        "media",
        help="list what real media each case can use",
    )
    media.add_argument("--media-dir", default=None)
    media.set_defaults(func=cmd_media)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
