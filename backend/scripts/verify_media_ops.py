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
"""Exercises the split-and-rejoin ffmpeg work against a real clip.

``media_ops`` is the only part of the upscale path that touches the
customer's actual pictures, and its unit tests run against synthetic
``testsrc`` clips: clean, silent, perfectly constant frame rate. Real
footage is none of those things. It carries an audio track a few
milliseconds longer than its picture, grain that changes what a re-encode
costs, and a container that may or may not admit how many frames it holds.
This script runs the shipped cut, join and audio restore over one such file
and checks the properties the upscale relies on, none of which a synthetic
clip can put under pressure.

Four things are checked, in the order the service does them:

* **The cut is frame-exact.** Each segment holds exactly the frames it was
  asked for, at the source's rate and size, with no audio. A count alone
  would not catch a cut that starts a frame early and ends a frame early
  too, so the first frame of each segment is also matched against the
  source frame it claims to be, and against its neighbours - if a
  neighbouring frame is the better match, the cut is off by one and every
  segment after it is misaligned.
* **The join loses nothing.** The rejoined clip holds every frame of the
  source, in one piece, at the same rate.
* **The audio comes back.** The master carries a track, and its length
  agrees with the picture to within a frame - that is what ``-shortest``
  is there for.
* **The picture survives the audio step.** The video packets of the master
  are compared byte for byte with the joined clip's. Anything but an exact
  match means the 4K picture was re-encoded on the way past, which is
  precisely what stream copy is specified to avoid.

No network, no VPE, no allowlist: this runs anywhere ffmpeg does.

    python -m scripts.verify_media_ops --source shot.mp4

Exit code is 0 only if every check passed. Everything written is kept in
``--out`` so a failure can be looked at rather than only read about.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

# pylint: disable=wrong-import-position
from src.videos.vpe.capabilities import VpeCapabilityId, get_capability
from src.videos.vpe.media_ops import (
    VpeMediaOpError,
    concat_segments,
    cut_segment,
    has_audio_stream,
    restore_audio,
)
from src.videos.vpe.preflight import VpeProbeError, probe_media
from src.videos.vpe.segmentation import VpeSegment, plan_segments

# pylint: enable=wrong-import-position

_MD5 = re.compile(r"MD5=([0-9a-f]+)")
_PSNR = re.compile(r"average:([0-9.]+|inf)")

# A crf 12 re-encode of real footage lands well above 40 dB. Anything under
# this is not "the same frame, encoded again" - it is a different frame.
_FIDELITY_FLOOR_DB = 30.0
# How much better a neighbouring frame has to score before the cut is called
# misaligned rather than merely noisy.
_MISALIGNMENT_MARGIN_DB = 0.5
# Below this spread, consecutive frames are too alike for the comparison to
# say anything - a locked-off shot of a wall matches its neighbours as well
# as it matches itself.
_DISTINGUISHABLE_DB = 1.0

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class Check:
    """One verified property and how it turned out.

    Attributes:
        name: Short description of what was checked.
        status: PASS, FAIL, or SKIP when the clip could not exercise it.
        detail: The measurement, or why it was skipped.
    """

    name: str
    status: str
    detail: str


class Report:
    """Collects check results and decides the exit code."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def record(self, name: str, status: str, detail: str) -> None:
        """Records one result and prints it as it happens.

        Args:
            name: What was checked.
            status: PASS, FAIL or SKIP.
            detail: The measurement behind the verdict.
        """
        self.checks.append(Check(name=name, status=status, detail=detail))
        print(f"  {status:4}  {name}: {detail}")

    def expect(self, name: str, passed: bool, detail: str) -> bool:
        """Records a pass or a fail from a condition.

        Args:
            name: What was checked.
            passed: Whether it held.
            detail: The measurement behind the verdict.

        Returns:
            The condition, so a caller can stop on a failure.
        """
        self.record(name, PASS if passed else FAIL, detail)
        return passed

    @property
    def failed(self) -> list[Check]:
        """Returns every check that did not hold."""
        return [check for check in self.checks if check.status == FAIL]


def _run(command: list[str]) -> tuple[int, str]:
    """Runs a command, returning its exit code and combined output.

    Args:
        command: Argument vector to execute.

    Returns:
        The exit code and the captured output.
    """
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _video_packet_md5(clip: Path) -> str:
    """Hashes a clip's video packets without decoding them.

    Stream-copying a video leaves its packets untouched, so two files whose
    video hashes match hold the identical picture regardless of what else
    changed around it. Decoding and hashing frames would answer a weaker
    question, since a re-encode can look identical and still be one.

    Args:
        clip: The file to hash.

    Returns:
        The hexadecimal digest.

    Raises:
        RuntimeError: If ffmpeg produced no digest.
    """
    code, out = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(clip),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "md5",
            "-",
        ],
    )
    match = _MD5.search(out)
    if code != 0 or not match:
        raise RuntimeError(f"could not hash the video stream of {clip}: {out}")
    return match.group(1)


def _extract_frame(clip: Path, index: int, target: Path) -> Path:
    """Writes one numbered frame out as a PNG.

    Args:
        clip: The file to read.
        index: Zero-based frame number.
        target: Where to write the PNG.

    Returns:
        The path written.

    Raises:
        RuntimeError: If ffmpeg wrote nothing.
    """
    code, out = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(clip),
            "-vf",
            f"select='eq(n\\,{index})'",
            "-vsync",
            "0",
            "-frames:v",
            "1",
            str(target),
        ],
    )
    if code != 0 or not target.exists():
        raise RuntimeError(f"could not read frame {index} of {clip}: {out}")
    return target


def _psnr(one: Path, other: Path) -> float:
    """Measures how alike two stills are, in decibels.

    Args:
        one: First image.
        other: Second image.

    Returns:
        Average PSNR, or infinity for identical images.

    Raises:
        RuntimeError: If ffmpeg reported no measurement.
    """
    code, out = _run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(one),
            "-i",
            str(other),
            "-lavfi",
            "psnr",
            "-f",
            "null",
            "-",
        ],
    )
    match = _PSNR.search(out)
    if code != 0 or not match:
        raise RuntimeError(f"could not compare {one} with {other}: {out}")
    value = match.group(1)
    return float("inf") if value == "inf" else float(value)


def _audio_seconds(clip: Path) -> float | None:
    """Returns the length of a clip's first audio stream, if it has one.

    Args:
        clip: The file to measure.

    Returns:
        The duration in seconds, or None if there is no audio or the
        container does not say.
    """
    code, out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "json",
            str(clip),
        ],
    )
    if code != 0:
        return None
    try:
        streams = json.loads(out).get("streams") or []
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    try:
        return float(streams[0].get("duration"))
    except (TypeError, ValueError):
        return None


def _plan(frames: int) -> tuple[tuple[VpeSegment, ...], str]:
    """Chooses the segments to cut, preferring the shipped planner.

    A clip already inside the upscaler's window is planned as a single
    segment by the service, quite correctly - but a single segment never
    exercises the join. Such a clip is halved here instead, and the report
    says so, because this script is verifying the ffmpeg work rather than
    the planning.

    Args:
        frames: Total frames in the source.

    Returns:
        The segments and a note describing where they came from.

    Raises:
        ValueError: If the clip is too short to divide at all.
    """
    constraints = get_capability(VpeCapabilityId.UPSCALE).input_video
    if frames > constraints.max_frames:
        segments = plan_segments(frames, constraints)
        return segments, "planned by plan_segments, as the service would"
    if frames < 2:
        raise ValueError(f"{frames} frame(s) cannot be split")
    first = frames // 2
    return (
        (
            VpeSegment(index=0, first_frame=0, frames=first),
            VpeSegment(index=1, first_frame=first, frames=frames - first),
        ),
        (
            "halved by this script: the clip fits the upscaler whole, so the"
            " service would not split it, but a single segment would leave"
            " the join untested"
        ),
    )


def _check_alignment(
    report: Report,
    source: Path,
    segment: VpeSegment,
    cut: Path,
    into: Path,
) -> None:
    """Checks a segment starts on the source frame it claims to.

    Compares the segment's first frame with the source frame it should be,
    and with the frames either side of it. A frame count cannot catch a cut
    that is uniformly one frame early, and such a cut is exactly what
    produces a duplicated or dropped frame at the join.

    Args:
        report: Where to record the results.
        source: The original clip.
        segment: The planned segment.
        cut: The segment file that was written.
        into: Directory for the extracted stills.
    """
    name = f"segment {segment.index} starts on source frame"
    mine = _extract_frame(cut, 0, into / f"seg{segment.index:02d}_first.png")

    scores: dict[int, float] = {}
    for offset in (-1, 0, 1):
        frame = segment.first_frame + offset
        if frame < 0:
            continue
        try:
            theirs = _extract_frame(
                source,
                frame,
                into / f"src{frame:06d}.png",
            )
        except RuntimeError:
            # Past the end of the source; not a candidate.
            continue
        scores[offset] = _psnr(mine, theirs)

    here = scores.get(0)
    if here is None:
        report.record(name, FAIL, "the source has no such frame")
        return

    others = {offset: value for offset, value in scores.items() if offset != 0}
    readings = ", ".join(
        f"{offset:+d}: {value:.1f} dB"
        for offset, value in sorted(scores.items())
    )
    best_other = max(others.values(), default=float("-inf"))

    if best_other > here + _MISALIGNMENT_MARGIN_DB:
        report.record(
            name,
            FAIL,
            f"a neighbouring frame matches better ({readings}) - the cut is"
            f" misaligned, so frame {segment.first_frame} is not what this"
            " segment begins with",
        )
        return
    if here < _FIDELITY_FLOOR_DB:
        report.record(
            name,
            FAIL,
            f"{here:.1f} dB against source frame {segment.first_frame},"
            f" under the {_FIDELITY_FLOOR_DB:g} dB floor ({readings})",
        )
        return
    if others and here - best_other < _DISTINGUISHABLE_DB:
        report.record(
            name,
            SKIP,
            f"{readings} - consecutive frames of this footage are too alike"
            " to tell an off-by-one from a clean cut; try a shot with motion",
        )
        return
    report.record(
        name,
        PASS,
        f"frame {segment.first_frame} at {here:.1f} dB, better than its"
        f" neighbours ({readings})",
    )


def _check_cuts(
    report: Report,
    source: Path,
    probe,
    segments: tuple[VpeSegment, ...],
    into: Path,
) -> list[Path]:
    """Cuts every segment and checks each one against its plan.

    Args:
        report: Where to record the results.
        source: The original clip.
        probe: The source's measurement.
        segments: The planned segments.
        into: Directory to write segments and stills into.

    Returns:
        The segment files, in order.
    """
    fps = int(probe.fps)
    cuts: list[Path] = []
    for segment in segments:
        cut = cut_segment(
            source,
            into / f"segment_{segment.index:02d}.mp4",
            first_frame=segment.first_frame,
            frames=segment.frames,
            fps=fps,
        )
        cuts.append(cut)
        measured = probe_media(str(cut))
        index = segment.index
        report.expect(
            f"segment {index} holds exactly {segment.frames} frames",
            measured.frame_count == segment.frames,
            f"measured {measured.frame_count}"
            + ("" if measured.frame_count_exact else " (derived, not stated)"),
        )
        report.expect(
            f"segment {index} keeps the source rate",
            measured.fps == probe.fps,
            f"{measured.fps} against the source's {probe.fps}",
        )
        report.expect(
            f"segment {index} keeps the source frame size",
            measured.frame_size == probe.frame_size,
            f"{measured.frame_size} against the source's {probe.frame_size}",
        )
        # The upscaler is sent silent segments on purpose: the original
        # track is laid over the finished master in one piece instead.
        report.expect(
            f"segment {index} carries no audio",
            not has_audio_stream(cut),
            "silent, as the API is meant to receive it",
        )
        _check_alignment(report, source, segment, cut, into)

    report.expect(
        "the segments cover the whole source",
        sum(segment.frames for segment in segments) == probe.frame_count,
        f"{sum(segment.frames for segment in segments)} frames planned across"
        f" {len(segments)} segments against {probe.frame_count} in the source",
    )
    return cuts


def _check_join(report: Report, probe, cuts: list[Path], into: Path) -> Path:
    """Joins the segments and checks nothing was lost.

    Args:
        report: Where to record the results.
        probe: The source's measurement.
        cuts: The segment files, in order.
        into: Directory to write the joined clip into.

    Returns:
        The joined clip.
    """
    joined = concat_segments(list(cuts), into / "rejoined.mp4")
    measured = probe_media(str(joined))
    report.expect(
        "the join returns every frame of the source",
        measured.frame_count == probe.frame_count,
        f"{measured.frame_count} frames against the source's"
        f" {probe.frame_count}",
    )
    report.expect(
        "the join keeps the source rate",
        measured.fps == probe.fps,
        f"{measured.fps} against the source's {probe.fps}",
    )
    report.expect(
        "the join keeps the source frame size",
        measured.frame_size == probe.frame_size,
        f"{measured.frame_size} against the source's {probe.frame_size}",
    )
    expected = probe.frame_count / float(probe.fps)
    actual = measured.container_seconds or 0.0
    within = 1.0 / float(probe.fps)
    report.expect(
        "the joined clip runs for as long as its frames say",
        abs(actual - expected) <= within,
        f"container reports {actual:.3f}s against {expected:.3f}s of frames,"
        f" inside one frame ({within:.3f}s)",
    )
    return joined


def _check_audio(
    report: Report,
    source: Path,
    joined: Path,
    into: Path,
) -> Path:
    """Restores the audio and checks the picture came through untouched.

    Args:
        report: Where to record the results.
        source: The original clip, whose audio is restored.
        joined: The silent rejoined clip.
        into: Directory to write the master into.

    Returns:
        The finished master, which is the joined clip itself when the
        source had no audio to restore.
    """
    target = into / "master.mp4"
    silent_hash = _video_packet_md5(joined)
    master = restore_audio(joined, source, target)

    if not has_audio_stream(source):
        report.record(
            "the source carries audio to restore",
            SKIP,
            f"{source.name} is silent, so the audio path is untested;"
            " run this again on a clip with a soundtrack",
        )
        report.expect(
            "a silent source is handed back untouched",
            master == joined and not target.exists(),
            f"restore_audio returned {master.name} without writing a new file",
        )
        return master

    report.expect(
        "the master carries an audio track",
        has_audio_stream(master),
        f"restored into {master.name}",
    )
    measured = probe_media(str(master))
    joined_probe = probe_media(str(joined))
    report.expect(
        "restoring audio changed no frames",
        measured.frame_count == joined_probe.frame_count,
        f"{measured.frame_count} frames against the joined clip's"
        f" {joined_probe.frame_count}",
    )
    report.expect(
        "the master's picture is the joined picture, byte for byte",
        _video_packet_md5(master) == silent_hash,
        f"video packets hash to {silent_hash[:12]}... in both, so the audio"
        " step stream-copied rather than re-encoded",
    )

    audio_seconds = _audio_seconds(master)
    picture_seconds = (
        measured.frame_count / float(measured.fps)
        if measured.frame_count and measured.fps
        else None
    )
    if audio_seconds is None or picture_seconds is None:
        report.record(
            "the track ends with the picture",
            SKIP,
            "the container does not state one of the two durations",
        )
        return master
    within = 1.0 / float(measured.fps)
    report.expect(
        "the track ends with the picture",
        abs(audio_seconds - picture_seconds) <= within,
        f"audio {audio_seconds:.3f}s against picture {picture_seconds:.3f}s,"
        f" inside one frame ({within:.3f}s)",
    )
    return master


def main(argv: list[str] | None = None) -> int:
    """Runs the cut, the join and the audio restore over one real clip.

    Args:
        argv: Command line arguments, or None to read ``sys.argv``.

    Returns:
        A process exit code: 0 if every check passed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", default="media_ops_check")
    args = parser.parse_args(argv)

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg and ffprobe are required")
        return 2

    source = Path(args.source)
    if not source.exists():
        print(f"no such file: {source}")
        return 2
    into = Path(args.out)
    into.mkdir(parents=True, exist_ok=True)

    try:
        probe = probe_media(str(source))
    except VpeProbeError as error:
        print(f"cannot measure {source}: {error}")
        return 2

    if probe.fps is None or probe.frame_count is None:
        print(f"{source} is not a video this script can cut")
        return 2
    if probe.fps.denominator != 1:
        # 23.976 is 24000/1001, and cut_segment takes a whole number because
        # every VPE capability is 24 fps exactly. Rounding here would write
        # segments whose timestamps drift from the source they came from.
        print(
            f"{source} is {probe.fps} fps. This script needs a whole-number"
            " frame rate, as the upscaler does."
        )
        return 2

    try:
        segments, note = _plan(probe.frame_count)
    except ValueError as error:
        print(f"cannot use this source: {error}")
        return 2

    audio = "with" if probe.has_audio else "no"
    print(
        f"{source.name}: {probe.frame_count} frames, {probe.frame_size},"
        f" {probe.fps} fps, {audio} audio"
    )
    print(
        f"{len(segments)} segments"
        f" {[segment.frames for segment in segments]} - {note}\n"
    )

    report = Report()
    try:
        cuts = _check_cuts(report, source, probe, segments, into)
        joined = _check_join(report, probe, cuts, into)
        master = _check_audio(report, source, joined, into)
    except (VpeMediaOpError, VpeProbeError, RuntimeError, OSError) as error:
        print(f"\nthe run stopped: {error}")
        report.record("the run completed", FAIL, str(error))
        master = None

    written = into / "media_ops_check.json"
    written.write_text(
        json.dumps(
            {
                "source": str(source),
                "source_frames": probe.frame_count,
                "source_fps": str(probe.fps),
                "source_has_audio": probe.has_audio,
                "plan": note,
                "segments": [
                    {
                        "index": segment.index,
                        "first_frame": segment.first_frame,
                        "frames": segment.frames,
                    }
                    for segment in segments
                ],
                "master": str(master) if master else None,
                "checks": [asdict(check) for check in report.checks],
                "failed": len(report.failed),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    passed = sum(1 for check in report.checks if check.status == PASS)
    skipped = sum(1 for check in report.checks if check.status == SKIP)
    print(f"\n{passed} passed, {len(report.failed)} failed, {skipped} skipped")
    if report.failed:
        for check in report.failed:
            print(f"  FAIL  {check.name}: {check.detail}")
        print(f"\n  everything written is under {into}")
        return 1
    if master:
        print(f"  master: {master}")
    print(f"  report: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
