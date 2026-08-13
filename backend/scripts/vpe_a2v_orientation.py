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
"""Measures what dialogue-driven generation does with a portrait still.

This is the gate on Phase 2, and it exists because the documented answer is
unfavourable enough to kill the phase and is not itself measured.

``veo-exp-a2v-generation`` is registered as ``FIXED_FRAME_UNSTATED``: a
1280x720 output frame with no ``aspectRatio`` parameter anywhere in its
schema. The doc says off-spec stills are "resized and padded internally"
rather than rejected, which read literally means a 720x1280 portrait frame
comes back pillarboxed into landscape - roughly 405x720 of real subject
inside a 1280x720 canvas - and comes back that way *silently*, with a 200
and no warning. For a 9:16 slate that is close to a non-feature, and the
silence is worse than the cropping.

None of that has been observed. The seam work is the precedent for why that
matters: the doc-derived expectation there was wrong in both directions, and
one measurement replaced a paragraph of inference. So this script submits a
matched pair - the same audio and prompt, once with a 16:9 still and once
with a 9:16 still - and reports what actually came back.

The landscape arm is the control. Without it a failure cannot be attributed:
a refusal might be the portrait frame, or it might be the audio, the prompt,
the bucket or the allowlist. If the control succeeds and the portrait arm
does not, the orientation is the cause.

Three outcomes, and they point three different ways:

* **Rejected** with a clean error - the capability is landscape-only and
  says so. That is an over-restriction report for the VPE programme.
* **Pillarboxed to 1280x720** - the doc is right, Phase 2 delivers ~405x720
  of subject for a vertical slate, and it belongs behind Phase 3.
* **Genuine 720x1280 out** - the doc understates the model and Phase 2 is
  worth building now.

Only the third justifies the service path, and the difference between them
is one generation per arm.

    python -m scripts.vpe_a2v_orientation --project P --bucket gs://B \\
        --audio line.wav --landscape still_16x9.png --portrait still_9x16.png

``--dry-run`` builds and validates both payloads without calling VPE, which
needs no allowlist and checks everything except the two jobs.

Note the audio constraint, which is a product fact and not just a schema
one: the capability takes **exactly 8.0 seconds**. Longer tracks are
truncated, shorter ones padded with silence. This script refuses a track
that is not 8s rather than letting the API silently clip a performance
mid-word and then blaming the result on orientation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.videos.vpe.capabilities import VpeCapabilityId, get_capability
from src.videos.vpe.client import VpeClient
from src.videos.vpe.payloads import VpeMediaRef, VpeRequest, build_payload

_PNG = "image/png"
_JPEG = "image/jpeg"
_WAV = "audio/wav"
_MP3 = "audio/mp3"

_MIME_BY_SUFFIX = {
    ".png": _PNG,
    ".jpg": _JPEG,
    ".jpeg": _JPEG,
    ".wav": _WAV,
    ".mp3": _MP3,
    ".m4a": "audio/mpeg",
    ".aac": "audio/mpeg",
}

# The capability's own rule, restated here so a mismatch is caught before a
# billed call rather than silently absorbed by the API.
_REQUIRED_AUDIO_SECONDS = 8.0
_AUDIO_TOLERANCE_SECONDS = 0.05

_DIMENSIONS = re.compile(r"^(\d+)x(\d+)$")


@dataclass
class Arm:
    """One half of the matched pair.

    Attributes:
        name: ``landscape`` or ``portrait``.
        still: The local first frame for this arm.
        width: Measured width of that still.
        height: Measured height of that still.
        gcs_uri: Where the still was uploaded.
        output_uri: Where VPE wrote the clip, if it did.
        out_width: Measured width of the produced clip.
        out_height: Measured height of the produced clip.
        seconds: Wall-clock time for the job.
        error: The failure, if the arm did not produce a clip.
        payload: The request body actually sent.
    """

    name: str
    still: Path
    width: int = 0
    height: int = 0
    gcs_uri: str = ""
    output_uri: str = ""
    out_width: int = 0
    out_height: int = 0
    seconds: float = 0.0
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def produced(self) -> bool:
        """Returns True if this arm came back with a measurable clip."""
        return bool(self.out_width and self.out_height)


def _run(command: list[str]) -> tuple[int, str]:
    """Runs a command and returns its exit code and merged output.

    Args:
        command: Argv to execute.

    Returns:
        The exit code and the combined stdout/stderr.
    """
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _probe_dimensions(path: str) -> tuple[int, int]:
    """Reads the pixel dimensions of a local or remote media file.

    Reads what the decoder *presents* rather than what the container
    declares, so a display matrix that ffmpeg would honour on decode is
    already accounted for.

    Args:
        path: A local path or a gs:// URI ffprobe can open.

    Returns:
        Width and height in pixels.

    Raises:
        RuntimeError: If nothing readable came back.
    """
    code, out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            path,
        ],
    )
    match = _DIMENSIONS.search(out.strip())
    if code != 0 or not match:
        raise RuntimeError(f"could not read dimensions of {path}: {out}")
    return int(match.group(1)), int(match.group(2))


def _probe_audio_seconds(path: Path) -> float:
    """Reads the duration of an audio file.

    Args:
        path: The track to measure.

    Returns:
        Duration in seconds.

    Raises:
        RuntimeError: If nothing readable came back.
    """
    # The exit code is not the signal: ffprobe reports success on a file it
    # could not time, and an unparseable duration is the real failure.
    _, out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
    )
    try:
        return float(out.strip())
    except ValueError as exc:
        raise RuntimeError(f"could not read duration of {path}: {out}") from exc


def _mime_for(path: Path) -> str:
    """Maps a file extension onto the mime type the capability accepts.

    Args:
        path: The file to classify.

    Returns:
        The mime type string.

    Raises:
        RuntimeError: If the extension is not one the capability lists.
    """
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
    if not mime:
        raise RuntimeError(
            f"{path.name}: {path.suffix} is not a mime type this capability"
            " accepts",
        )
    return mime


def _upload(local: Path, bucket: str, prefix: str) -> str:
    """Copies an input into the VPE bucket.

    Args:
        local: The file to upload.
        bucket: Destination bucket URI.
        prefix: Subdirectory within the bucket.

    Returns:
        The uploaded object's URI.

    Raises:
        RuntimeError: If the copy fails.
    """
    uri = f"{bucket.rstrip('/')}/{prefix}/{local.name}"
    code, out = _run(["gcloud", "storage", "cp", str(local), uri])
    if code != 0:
        raise RuntimeError(f"upload failed for {local.name}: {out}")
    return uri


def _build(
    arm: Arm, audio_uri: str, audio_mime: str, storage_uri: str, prompt: str
) -> dict[str, Any]:
    """Builds the request body for one arm.

    Goes through the shipped `build_payload` rather than hand-rolling JSON,
    so a green dry run says the service's own path accepts these inputs and
    not merely that this script's idea of them is well-formed.

    Args:
        arm: The arm to build for, with its still already uploaded.
        audio_uri: The uploaded speech track.
        audio_mime: That track's mime type.
        storage_uri: Where VPE should write this arm's output.
        prompt: The character description.

    Returns:
        The request body.
    """
    request = VpeRequest(
        capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
        storage_uri=storage_uri,
        prompt=prompt,
        image=VpeMediaRef(gcs_uri=arm.gcs_uri, mime_type=_mime_for(arm.still)),
        reference_audios=(
            VpeMediaRef(gcs_uri=audio_uri, mime_type=audio_mime),
        ),
    )
    return build_payload(request)


def _generate(
    client: VpeClient, arm: Arm, payload: dict[str, Any], timeout: float
) -> Arm:
    """Submits one arm and records where it landed.

    A refusal is a result here, not an exception to propagate: the whole
    point is to learn which of the three outcomes this is, and a portrait
    arm that errors while the landscape control succeeds is the cleanest
    possible answer.

    Args:
        client: The VPE client.
        arm: The arm to run.
        payload: Its request body.
        timeout: Seconds to keep polling.

    Returns:
        The same arm, with its outcome filled in.
    """
    started = time.time()
    try:
        operation = client.submit(payload)
        result = client.wait_for_completion_sync(
            operation,
            timeout_seconds=timeout,
        )
        video = getattr(result, "primary_video", None)
        arm.output_uri = str(getattr(video, "gcs_uri", "") or "")
        if not arm.output_uri:
            arm.error = "finished but named no output file"
    # Deliberately broad: a refusal is the measurement here, not an error to
    # propagate. Narrowing this to VpeError would let an unanticipated
    # failure abort the run after the control arm has already been billed.
    except Exception as exc:  # pylint: disable=broad-exception-caught
        arm.error = f"{type(exc).__name__}: {exc}"
    arm.seconds = round(time.time() - started, 1)

    if arm.output_uri:
        try:
            arm.out_width, arm.out_height = _probe_dimensions(arm.output_uri)
        except RuntimeError as exc:
            arm.error = str(exc)
    return arm


def _verdict(landscape: Arm, portrait: Arm) -> tuple[str, str]:
    """Decides which of the three outcomes this run produced.

    Args:
        landscape: The control arm.
        portrait: The arm under test.

    Returns:
        A short verdict code and a sentence explaining it.
    """
    if not landscape.produced:
        return (
            "INCONCLUSIVE",
            "The landscape control did not produce a clip either, so nothing"
            " here can be attributed to orientation. Fix the control first:"
            f" {landscape.error or 'no output'}",
        )

    if not portrait.produced:
        return (
            "REJECTED",
            "The portrait still was refused while the identical landscape"
            " request succeeded, so the capability is landscape-only in"
            " practice. That is an over-restriction worth reporting to the"
            f" VPE programme. Error: {portrait.error or 'no output'}",
        )

    if portrait.out_height > portrait.out_width:
        return (
            "PORTRAIT_OK",
            f"The portrait arm came back {portrait.out_width}x"
            f"{portrait.out_height}, which is genuinely vertical. The"
            " documentation understates the model and Phase 2 is worth"
            " building for a 9:16 slate.",
        )

    # Landscape out from a portrait in. Work out how much of that frame is
    # actually subject rather than padding, since that is the number the
    # customer conversation turns on.
    source_ratio = portrait.width / portrait.height
    subject_width = round(portrait.out_height * source_ratio)
    return (
        "PILLARBOXED",
        f"A {portrait.width}x{portrait.height} still came back as"
        f" {portrait.out_width}x{portrait.out_height} with no error, so the"
        " documented padding is real. Roughly"
        f" {subject_width}x{portrait.out_height} of that frame is subject and"
        f" the rest is padding - about"
        f" {round(100 * subject_width / portrait.out_width)}% of the width."
        " Phase 2 delivers little for a vertical slate and belongs behind"
        " Phase 3.",
    )


def _report(
    landscape: Arm, portrait: Arm, verdict: str, reason: str, out: Path
) -> None:
    """Writes the run down so it can be quoted without being re-run.

    Args:
        landscape: The control arm.
        portrait: The arm under test.
        verdict: The verdict code.
        reason: The sentence explaining it.
        out: Directory to write into.
    """
    out.mkdir(parents=True, exist_ok=True)
    document = {
        "capability": "veo-exp-a2v-generation",
        "verdict": verdict,
        "reason": reason,
        "arms": {
            arm.name: {
                "input": f"{arm.width}x{arm.height}",
                "input_gcs": arm.gcs_uri,
                "output": (
                    f"{arm.out_width}x{arm.out_height}"
                    if arm.produced
                    else None
                ),
                "output_gcs": arm.output_uri or None,
                "seconds": arm.seconds,
                "error": arm.error or None,
                "payload": arm.payload,
            }
            for arm in (landscape, portrait)
        },
    }
    (out / "a2v_orientation.json").write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """Runs the matched pair and reports which outcome it is.

    Args:
        argv: Command line arguments, or None to read sys.argv.

    Returns:
        Process exit code. Non-zero only when the run could not answer the
        question, not when the answer is unfavourable.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--landscape", required=True, type=Path)
    parser.add_argument("--portrait", required=True, type=Path)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--out", default="a2v_report", type=Path)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument(
        "--prompt",
        default=(
            "A person speaking directly to camera in a warmly lit room."
            " Static camera, no cuts."
        ),
        help="Must describe the character in the still; misalignment"
        " degrades lip-sync.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    capability = get_capability(VpeCapabilityId.DIALOGUE_DRIVEN)
    print(f"capability: {capability.model_name} ({capability.orientation})")

    seconds = _probe_audio_seconds(args.audio)
    if abs(seconds - _REQUIRED_AUDIO_SECONDS) > _AUDIO_TOLERANCE_SECONDS:
        # Refused rather than warned: an off-length track is silently
        # truncated or silence-padded by the API, and a run measuring
        # orientation must not also be measuring that.
        print(
            f"error: {args.audio.name} is {seconds:.2f}s. This capability"
            f" takes exactly {_REQUIRED_AUDIO_SECONDS}s - longer is"
            " truncated, shorter is padded with silence - so an off-length"
            " track would confound the result. Trim or pad it first.",
            file=sys.stderr,
        )
        return 2

    arms = [
        Arm(name="landscape", still=args.landscape),
        Arm(name="portrait", still=args.portrait),
    ]
    for arm in arms:
        arm.width, arm.height = _probe_dimensions(str(arm.still))
        print(f"{arm.name:>9}: {arm.still.name} {arm.width}x{arm.height}")

    if arms[0].width <= arms[0].height:
        print("error: --landscape is not landscape", file=sys.stderr)
        return 2
    if arms[1].height <= arms[1].width:
        print("error: --portrait is not portrait", file=sys.stderr)
        return 2

    prefix = f"a2v_orientation_{int(time.time())}"
    audio_mime = _mime_for(args.audio)

    if args.dry_run:
        for arm in arms:
            arm.gcs_uri = f"{args.bucket.rstrip('/')}/{prefix}/{arm.still.name}"
            arm.payload = _build(
                arm,
                f"{args.bucket.rstrip('/')}/{prefix}/{args.audio.name}",
                audio_mime,
                f"{args.bucket.rstrip('/')}/{prefix}/out_{arm.name}",
                args.prompt,
            )
        print("\nboth payloads built and validated; no calls made")
        _report(arms[0], arms[1], "DRY_RUN", "No calls made.", args.out)
        return 0

    audio_uri = _upload(args.audio, args.bucket, prefix)
    client = VpeClient(project_id=args.project, location=args.location)

    for arm in arms:
        arm.gcs_uri = _upload(arm.still, args.bucket, prefix)
        arm.payload = _build(
            arm,
            audio_uri,
            audio_mime,
            f"{args.bucket.rstrip('/')}/{prefix}/out_{arm.name}",
            args.prompt,
        )
        print(f"\nsubmitting {arm.name}...")
        _generate(client, arm, arm.payload, args.timeout)
        if arm.produced:
            print(
                f"{arm.name:>9}: -> {arm.out_width}x{arm.out_height}"
                f" in {arm.seconds}s",
            )
        else:
            print(f"{arm.name:>9}: no clip ({arm.error}) in {arm.seconds}s")

    verdict, reason = _verdict(arms[0], arms[1])
    _report(arms[0], arms[1], verdict, reason, args.out)

    print(f"\nVERDICT: {verdict}\n{reason}")
    print(f"\nwritten to {args.out / 'a2v_orientation.json'}")
    return 0 if verdict != "INCONCLUSIVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
