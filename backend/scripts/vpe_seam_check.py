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
"""Measures whether a split-and-rejoined upscale has a visible seam.

The upscaler accepts 4 to 8 seconds. Real production shots are 9 to 10, so
only 4 of the customer's 16 measured shots fit, and a plain upscale button
would be greyed out on the other 12. Splitting a shot, upscaling each half
and rejoining is the obvious way out - but only if the halves come back
looking like each other. They are independent jobs, and nothing in the
documentation promises deterministic grain, sharpening or face texture
between two of them.

That is the whole question this script answers, and it answers it with a
number rather than an opinion. Each segment is upscaled, the results are
concatenated, and the per-frame difference is measured across the join:

* ``tblend=all_mode=difference`` gives the difference between each frame and
  the one before it, and ``signalstats`` averages it. Motion makes that
  number large everywhere, so the absolute value says nothing on its own.
* What says something is the comparison. Frames inside a segment establish
  what a normal frame-to-frame step looks like for this footage. The step
  across the seam is then read against that distribution.

A seam that sits inside the normal spread is invisible and split-and-rejoin
is viable. A seam that is a large multiple of the p95 is a pop, and the
honest answer to the customer becomes "the upscaler is for 4-8s shots".

Nothing here is Creative Studio code: it goes through ``build_payload`` and
``VpeClient`` exactly as the app will, so a green run says the shipped path
works and not merely that the service does.

    python -m scripts.vpe_seam_check --project P --bucket gs://B \\
        --source shot.mp4 --out seam_report

``--dry-run`` does the split and the analysis wiring without calling VPE,
which needs no allowlist and checks everything but the two jobs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent import futures
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

# pylint: disable=wrong-import-position
from src.videos.vpe.capabilities import VpeCapabilityId
from src.videos.vpe.client import VpeClient
from src.videos.vpe.payloads import VpeMediaRef, VpeRequest, build_payload
from src.videos.vpe.preflight import probe_media

# pylint: enable=wrong-import-position

_MP4 = "video/mp4"
# 24 fps throughout VPE, and the upscaler's window is 4 to 8 seconds.
_FPS = 24
_MIN_SEGMENT_FRAMES = 4 * _FPS
_MAX_SEGMENT_FRAMES = 8 * _FPS
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


@dataclass
class Segment:
    """One piece of the source clip, and where its upscale ended up."""

    index: int
    first_frame: int
    frames: int
    local: Path
    gcs_uri: str = ""
    output_uri: str = ""
    upscaled: Path | None = None
    seconds: float = 0.0


def _run(command: list[str]) -> tuple[int, str]:
    """Runs a command, returning its exit code and combined output.

    Args:
        command: Argument vector to execute.

    Returns:
        The exit code and the captured output.
    """
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _ffmpeg(*args: str) -> None:
    """Runs ffmpeg, raising with its own diagnostics if it fails.

    Args:
        *args: Arguments after ``ffmpeg -v error -y``.

    Raises:
        RuntimeError: If ffmpeg exits non-zero.
    """
    code, out = _run(["ffmpeg", "-v", "error", "-y", *args])
    if code != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)}\n{out}")


def _plan_segments(frames: int, wanted: int) -> list[tuple[int, int]]:
    """Divides a frame count into upscalable pieces of similar length.

    Even division matters more than hitting ``wanted`` exactly. Ten seconds
    split at a flat 192 gives 192 + 48, and a 2 second tail is both outside
    the 4 second minimum and a different enough chunk of time that its grain
    would have every reason to differ. Balanced halves avoid that.

    Args:
        frames: Total frames in the source.
        wanted: Preferred segment length in frames.

    Returns:
        A list of (first_frame, length) pairs covering every frame.

    Raises:
        ValueError: If no division into legal segments exists.
    """
    if frames <= _MAX_SEGMENT_FRAMES:
        raise ValueError(
            f"{frames} frames is already inside the upscaler's window"
            f" ({_MIN_SEGMENT_FRAMES}-{_MAX_SEGMENT_FRAMES} frames);"
            " no split is needed and there is no seam to measure."
        )
    count = max(2, -(-frames // wanted))
    while count * _MAX_SEGMENT_FRAMES < frames:
        count += 1
    if frames < count * _MIN_SEGMENT_FRAMES:
        raise ValueError(
            f"{frames} frames cannot be divided into {count} segments of at"
            f" least {_MIN_SEGMENT_FRAMES} frames each."
        )

    base, extra = divmod(frames, count)
    plan: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        length = base + (1 if index < extra else 0)
        plan.append((start, length))
        start += length
    return plan


def _cut(source: Path, into: Path, first: int, frames: int, index: int) -> Path:
    """Cuts an exact frame range out of the source.

    Selecting on frame number rather than seeking by timestamp because the
    boundary has to be frame-exact: a seek that lands a frame early would
    duplicate or drop one at the join and manufacture the very discontinuity
    this script exists to measure.

    Args:
        source: The clip to cut from.
        into: Directory to write the segment into.
        first: Index of the first frame to keep, zero based.
        frames: How many frames to keep.
        index: Segment number, used for the filename.

    Returns:
        The path to the segment.
    """
    target = into / f"segment_{index:02d}.mp4"
    last = first + frames - 1
    _ffmpeg(
        "-i",
        str(source),
        "-vf",
        f"select='between(n\\,{first}\\,{last})',setpts=N/{_FPS}/TB",
        # setpts has already put the kept frames back on a 24 fps grid, so
        # the encoder is told to hold that rate rather than infer one.
        "-fps_mode",
        "cfr",
        "-r",
        str(_FPS),
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "12",
        "-pix_fmt",
        "yuv420p",
        str(target),
    )
    return target


def _upload(local: Path, bucket: str, prefix: str) -> str:
    """Copies a segment into the VPE bucket.

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


def _download(uri: str, local: Path) -> None:
    """Fetches a produced clip back for analysis.

    Args:
        uri: The object to fetch.
        local: Where to write it.

    Raises:
        RuntimeError: If the copy fails.
    """
    code, out = _run(["gcloud", "storage", "cp", uri, str(local)])
    if code != 0:
        raise RuntimeError(f"download failed for {uri}: {out}")


def _upscale(
    client: VpeClient,
    segment: Segment,
    bucket: str,
    prefix: str,
    aspect_ratio: str,
    timeout: float,
) -> Segment:
    """Upscales one segment to 4K and records where it landed.

    Args:
        client: The VPE client.
        segment: The segment to upscale, already uploaded.
        bucket: Destination bucket URI.
        prefix: Subdirectory for this run's outputs.
        aspect_ratio: Sent explicitly - a 1080p source errors without it.
        timeout: Seconds to keep polling.

    Returns:
        The same segment, with its output URI filled in.
    """
    request = VpeRequest(
        capability_id=VpeCapabilityId.UPSCALE,
        storage_uri=f"{bucket.rstrip('/')}/{prefix}/out_{segment.index:02d}",
        video=VpeMediaRef(gcs_uri=segment.gcs_uri, mime_type=_MP4),
        resolution="4k",
        aspect_ratio=aspect_ratio,
    )
    started = time.time()
    operation = client.submit(build_payload(request))
    result = client.wait_for_completion_sync(operation, timeout_seconds=timeout)
    segment.seconds = round(time.time() - started, 1)
    video = getattr(result, "primary_video", None)
    segment.output_uri = str(getattr(video, "gcs_uri", "") or "")
    if not segment.output_uri:
        raise RuntimeError(
            f"segment {segment.index} finished but named no output file"
        )
    return segment


def _concat(segments: list[Segment], into: Path) -> Path:
    """Joins the upscaled segments in order, without re-encoding.

    Stream copy on purpose: a re-encode would apply its own quantisation
    across the join and could either mask a real seam or invent one.

    Args:
        segments: Segments carrying downloaded ``upscaled`` paths.
        into: Directory to write the joined clip and its list file into.

    Returns:
        The joined clip.
    """
    listing = into / "concat.txt"
    listing.write_text(
        "".join(f"file '{item.upscaled}'\n" for item in segments),
    )
    target = into / "rejoined.mp4"
    _ffmpeg(
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        str(target),
    )
    return target


def _frame_deltas(clip: Path) -> list[float]:
    """Measures how much each frame differs from the one before it.

    Args:
        clip: The clip to measure.

    Returns:
        One average difference per frame transition, in frame order.

    Raises:
        RuntimeError: If ffmpeg produced no measurements.
    """
    # ffmpeg's exit code is not the signal here: it reports success on a
    # clip it could not measure. An empty result is the real failure.
    _, out = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(clip),
            "-vf",
            "tblend=all_mode=difference,signalstats,"
            "metadata=print:key=lavfi.signalstats.YAVG:file=-",
            "-f",
            "null",
            "-",
        ],
    )
    values = [float(match) for match in _YAVG.findall(out)]
    if not values:
        raise RuntimeError(f"no frame statistics came back for {clip}: {out}")
    return values


def _analyse(deltas: list[float], boundaries: list[int]) -> dict:
    """Reads the seam transitions against the within-segment distribution.

    ``tblend`` emits one value per *transition*, so N frames give N-1
    entries and ``deltas[i]`` is the step from frame i to frame i+1. The
    step into the first frame of a new segment is therefore at
    ``boundary - 1``, not at ``boundary``. Reading it one late measures an
    ordinary step inside the second segment, which reports a clean join for
    any footage at all - the detector was silently doing exactly that until
    a deliberately mismatched pair failed to trip it.

    Args:
        deltas: Per-transition differences from ``_frame_deltas``.
        boundaries: Frame indices where one segment becomes the next.

    Returns:
        The verdict, the seam readings and the baseline they were read
        against.
    """
    seam_indices = [boundary - 1 for boundary in boundaries]
    seam_set = set(seam_indices)
    # A seam transition is not part of the baseline it is judged against.
    inside = [
        value for index, value in enumerate(deltas) if index not in seam_set
    ]
    ordered = sorted(inside)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    median = statistics.median(inside)

    seams = []
    worst = 0.0
    for boundary, index in zip(boundaries, seam_indices):
        if not 0 <= index < len(deltas):
            continue
        value = deltas[index]
        ratio = value / p95 if p95 else float("inf")
        worst = max(worst, ratio)
        seams.append(
            {
                "frame": boundary,
                "delta": round(value, 4),
                "times_p95": round(ratio, 2),
            },
        )

    if worst <= 1.5:
        verdict = "INVISIBLE"
        reading = (
            "Every seam sits inside the normal frame-to-frame variation of"
            " this footage. Split-and-rejoin is viable."
        )
    elif worst <= 4.0:
        verdict = "MARGINAL"
        reading = (
            "The seams stand out from the baseline but not hugely. Watch the"
            " rejoined clip before deciding; grain differences read worse in"
            " motion than a number suggests."
        )
    else:
        verdict = "VISIBLE"
        reading = (
            "The seams are a large multiple of normal frame-to-frame"
            " variation. Independent upscales do not match, so"
            " split-and-rejoin would ship a visible pop."
        )

    return {
        "verdict": verdict,
        "reading": reading,
        "worst_seam_times_p95": round(worst, 2),
        "baseline_median": round(median, 4),
        "baseline_p95": round(p95, 4),
        "baseline_samples": len(inside),
        "seams": seams,
    }


def _save_seam_frames(clip: Path, boundaries: list[int], into: Path) -> None:
    """Writes the frames either side of each join, for a human to look at.

    A number can say two frames differ; only the frames themselves say
    whether the difference is grain, colour or sharpening, and that is what
    decides how to fix it.

    Args:
        clip: The rejoined clip.
        boundaries: Frame indices where one segment becomes the next.
        into: Directory to write PNGs into.
    """
    into.mkdir(parents=True, exist_ok=True)
    for boundary in boundaries:
        for offset in (-1, 0):
            frame = boundary + offset
            if frame < 0:
                continue
            _ffmpeg(
                "-i",
                str(clip),
                "-vf",
                f"select='eq(n\\,{frame})'",
                "-vsync",
                "0",
                "-frames:v",
                "1",
                str(into / f"seam_{boundary:04d}_frame_{frame:04d}.png"),
            )


def _probe_source(source: Path, segment_frames: int) -> tuple[int, str]:
    """Checks the source is something the upscaler could take in pieces.

    Args:
        source: The clip to split.
        segment_frames: Preferred segment length, for the error message.

    Returns:
        The frame count and the aspect ratio to send.

    Raises:
        ValueError: If the clip is off-spec in a way splitting cannot fix.
    """
    probe = probe_media(str(source))
    if probe.fps != Fraction(_FPS, 1):
        raise ValueError(
            f"{source} is {probe.fps} fps; the upscaler requires exactly"
            f" {_FPS}. Splitting cannot fix a frame rate."
        )
    size = probe.frame_size
    if size is None or (size.width, size.height) not in (
        (1280, 720),
        (720, 1280),
        (1920, 1080),
        (1080, 1920),
    ):
        raise ValueError(
            f"{source} is {size}; the upscaler takes 1280x720 or 1920x1080,"
            " either orientation."
        )
    if probe.frame_count is None:
        raise ValueError(f"{source} carries no frame count")
    if probe.frame_count <= _MAX_SEGMENT_FRAMES:
        raise ValueError(
            f"{source} is {probe.frame_count} frames, which the upscaler"
            f" already accepts whole. Pick a longer shot: at {segment_frames}"
            " frames a segment there is nothing to rejoin."
        )
    return probe.frame_count, "9:16" if size.is_portrait else "16:9"


def main(argv: list[str] | None = None) -> int:
    """Runs the split, the upscales, the rejoin and the measurement.

    Args:
        argv: Command line arguments, or None to read ``sys.argv``.

    Returns:
        A process exit code: 0 if the seam is invisible, 1 if it is not.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--out", default="seam_report")
    parser.add_argument("--segment-frames", type=int, default=120)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="split and wire up the analysis without calling VPE",
    )
    args = parser.parse_args(argv)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg and ffprobe are required")
        return 2
    if not _MIN_SEGMENT_FRAMES <= args.segment_frames <= _MAX_SEGMENT_FRAMES:
        print(
            f"--segment-frames must be {_MIN_SEGMENT_FRAMES}-"
            f"{_MAX_SEGMENT_FRAMES} ({_MIN_SEGMENT_FRAMES//_FPS}-"
            f"{_MAX_SEGMENT_FRAMES//_FPS}s at {_FPS} fps)"
        )
        return 2

    source = Path(args.source)
    report = Path(args.out)
    (report / "segments").mkdir(parents=True, exist_ok=True)

    try:
        frames, aspect_ratio = _probe_source(source, args.segment_frames)
        plan = _plan_segments(frames, args.segment_frames)
    except (ValueError, RuntimeError) as error:
        print(f"cannot use this source: {error}")
        return 2

    print(
        f"{source.name}: {frames} frames -> {len(plan)} segments"
        f" {[length for _, length in plan]} at {aspect_ratio}"
    )
    segments = [
        Segment(
            index=index,
            first_frame=first,
            frames=length,
            local=_cut(source, report / "segments", first, length, index),
        )
        for index, (first, length) in enumerate(plan)
    ]

    # Seams are at the joins, so the last segment contributes none.
    boundaries: list[int] = []
    running = 0
    for segment in segments[:-1]:
        running += segment.frames
        boundaries.append(running)

    if args.dry_run:
        print(f"dry run: {len(segments)} segments cut, seams would be at")
        print(f"  frames {boundaries}")
        for segment in segments:
            probe = probe_media(str(segment.local))
            print(
                f"  segment {segment.index}: {probe.frame_count} frames,"
                f" {probe.frame_size}, {probe.fps} fps"
            )
        return 0

    prefix = f"seam-check/{int(time.time())}"
    client = VpeClient(project_id=args.project, location=args.location)
    for segment in segments:
        segment.gcs_uri = _upload(segment.local, args.bucket, prefix)

    print(f"upscaling {len(segments)} segments concurrently...")
    with futures.ThreadPoolExecutor(max_workers=len(segments)) as pool:
        running_jobs = [
            pool.submit(
                _upscale,
                client,
                segment,
                args.bucket,
                prefix,
                aspect_ratio,
                args.timeout,
            )
            for segment in segments
        ]
        try:
            for job in running_jobs:
                done = job.result()
                print(f"  segment {done.index} in {done.seconds}s")
        except (RuntimeError, OSError) as error:
            print(f"upscale failed: {error}")
            return 1

    for segment in segments:
        segment.upscaled = report / f"upscaled_{segment.index:02d}.mp4"
        _download(segment.output_uri, segment.upscaled)

    rejoined = _concat(segments, report)
    analysis = _analyse(_frame_deltas(rejoined), boundaries)
    _save_seam_frames(rejoined, boundaries, report / "seam_frames")

    (report / "seam_analysis.json").write_text(
        json.dumps(
            {
                "source": str(source),
                "source_frames": frames,
                "aspect_ratio": aspect_ratio,
                "segments": [
                    {
                        "index": item.index,
                        "first_frame": item.first_frame,
                        "frames": item.frames,
                        "seconds": item.seconds,
                        "output": item.output_uri,
                    }
                    for item in segments
                ],
                "rejoined": str(rejoined),
                **analysis,
            },
            indent=2,
        ),
    )

    verdict = analysis["verdict"]
    median = analysis["baseline_median"]
    p95 = analysis["baseline_p95"]
    samples = analysis["baseline_samples"]
    print(f"\n{verdict}: {analysis['reading']}")
    print(f"  baseline: median {median}, p95 {p95} over {samples} transitions")
    for seam in analysis["seams"]:
        frame = seam["frame"]
        delta = seam["delta"]
        times = seam["times_p95"]
        print(f"  seam at frame {frame}: {delta} ({times}x p95)")
    seam_frames = report / "seam_frames"
    print(f"\n  rejoined clip: {rejoined}")
    print(f"  seam frames:   {seam_frames}")
    return 0 if analysis["verdict"] == "INVISIBLE" else 1


if __name__ == "__main__":
    sys.exit(main())
