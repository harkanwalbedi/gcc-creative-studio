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
"""The ffmpeg side of split-and-rejoin.

Cutting a clip into segments, putting the upscaled pieces back together and
restoring the sound. ``ffmpeg`` is already in the backend image, added for
the Omni audio-stripping work, so none of this costs new infrastructure.

**Audio is never split.** The segments go up to the API silent and the
original audio track is laid over the finished 4K video in one piece at the
end. Cutting the audio alongside the video would mean rejoining it too, and
an audio join is far less forgiving than a video one: a video seam has to
survive a single frame at 1/24th of a second, while a click or a gap in a
waveform is audible on any system. It also removes any dependence on whether
the upscaler preserves an incoming audio track, which is not documented.
Upscaling does not change a clip's duration, so the original track still
lines up frame for frame.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


class VpeMediaOpError(RuntimeError):
    """Raised when an ffmpeg or ffprobe invocation fails."""


def _run(command: list[str]) -> str:
    """Runs a command and returns its combined output.

    Args:
        command: Argument vector to execute.

    Returns:
        Standard output followed by standard error.

    Raises:
        VpeMediaOpError: If the command exits non-zero.
    """
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (done.stdout or "") + (done.stderr or "")
    if done.returncode != 0:
        raise VpeMediaOpError(
            f"{command[0]} failed ({done.returncode}): {output.strip()}",
        )
    return output


def _ffmpeg(*args: str) -> None:
    """Runs ffmpeg quietly, raising with its diagnostics on failure.

    Args:
        *args: Arguments following ``ffmpeg -v error -y``.
    """
    _run(["ffmpeg", "-v", "error", "-y", *args])


def has_audio_stream(source: Path | str) -> bool:
    """Reports whether a file carries at least one audio stream.

    Args:
        source: The file to inspect.

    Returns:
        True if an audio stream is present.
    """
    output = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(source),
        ],
    )
    try:
        return bool(json.loads(output).get("streams"))
    except json.JSONDecodeError as error:
        raise VpeMediaOpError(
            f"Could not read audio streams from {source}: {error}",
        ) from error


def written_frames(target: Path | str) -> int:
    """Counts the video frames in a file ffmpeg has just written.

    Reads the container's own count rather than decoding. ffmpeg writes an
    accurate ``nb_frames`` into the files it produces, so this costs a
    metadata read - which is the only reason it is affordable to do after
    every cut.

    Args:
        target: The file to count.

    Returns:
        The frame count, or zero if the file carries no video stream at all.

    Raises:
        VpeMediaOpError: If ffprobe's output cannot be read.
    """
    output = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_frames",
            "-of",
            "json",
            str(target),
        ],
    )
    try:
        streams = json.loads(output).get("streams") or []
    except json.JSONDecodeError as error:
        raise VpeMediaOpError(
            f"Could not read a frame count from {target}: {error}",
        ) from error
    if not streams:
        return 0
    try:
        return int(streams[0].get("nb_frames") or 0)
    except (TypeError, ValueError):
        return 0


def cut_segment(
    source: Path | str,
    target: Path | str,
    *,
    first_frame: int,
    frames: int,
    fps: int,
) -> Path:
    """Cuts an exact frame range out of a clip, without audio.

    Selects on frame number rather than seeking by timestamp. The boundary
    has to be frame-exact: a seek landing a frame early duplicates or drops
    one at the join, manufacturing precisely the discontinuity the whole
    approach depends on avoiding.

    Args:
        source: The clip to cut from.
        target: Where to write the segment.
        first_frame: Index of the first frame to keep, zero based.
        frames: How many frames to keep.
        fps: Frame rate of the source, which the segment holds.

    Returns:
        The path written.

    Raises:
        VpeMediaOpError: If the cut did not write the frames it was asked
            for.
    """
    last_frame = first_frame + frames - 1
    _ffmpeg(
        "-i",
        str(source),
        "-vf",
        f"select='between(n\\,{first_frame}\\,{last_frame})'"
        f",setpts=N/{fps}/TB",
        # setpts has already put the kept frames back onto a constant grid,
        # so the encoder is told to hold that rate rather than infer one.
        # -r alone would conflict with a non-CFR mode.
        "-fps_mode",
        "cfr",
        "-r",
        str(fps),
        "-an",
        "-c:v",
        "libx264",
        # Near-lossless. This is an intermediate the upscaler reads, so
        # compression artefacts introduced here would be magnified into the
        # 4K master rather than hidden by it.
        "-crf",
        "12",
        "-pix_fmt",
        "yuv420p",
        str(target),
    )
    # ffmpeg exits zero having written whatever it could, so the only proof
    # the cut is the length that was planned is to count it. The count the
    # planner divided up comes from the container, which declares the samples
    # in the track; `select` keeps the frames the file actually presents, and
    # an mp4 edit list - what an `ffmpeg -ss ... -c copy` trim leaves behind -
    # makes those two disagree. Unchecked, a short final segment goes to the
    # API under the minimum it accepts, after the segments before it have
    # already been submitted and paid for.
    written = written_frames(target)
    if written != frames:
        raise VpeMediaOpError(
            f"Cutting {frames} frames from frame {first_frame} of {source}"
            f" wrote {written} instead. The source presents fewer frames than"
            " its container declares, which is what an 'ffmpeg -ss ... -c"
            " copy' trim leaves behind; re-encode it before upscaling.",
        )
    return Path(target)


def concat_segments(
    segments: list[Path | str],
    target: Path | str,
    *,
    listing: Path | str | None = None,
) -> Path:
    """Joins upscaled segments in order, without re-encoding.

    Stream copy is deliberate. Re-encoding here would resample every frame
    and could just as easily smooth a real seam out of existence as add one,
    which would make the joined file useless as evidence and lossier as a
    deliverable.

    Args:
        segments: The pieces to join, in playback order.
        target: Where to write the joined file.
        listing: Where to write ffmpeg's concat list, defaulting to beside
            the target.

    Returns:
        The path written.

    Raises:
        VpeMediaOpError: If no segments were supplied.
    """
    if not segments:
        raise VpeMediaOpError("Nothing to concatenate.")

    target = Path(target)
    listing = Path(listing) if listing else target.with_suffix(".concat.txt")
    # Absolute paths, because the concat demuxer resolves each entry
    # relative to the listing file rather than the working directory.
    listing.write_text(
        "".join(f"file '{Path(segment).resolve()}'\n" for segment in segments),
        encoding="utf-8",
    )
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


def restore_audio(
    video: Path | str,
    audio_source: Path | str,
    target: Path | str,
) -> Path:
    """Lays the original audio track over a finished video.

    The video is stream-copied and only the audio is encoded, so the 4K
    picture is untouched by this step.

    Args:
        video: The finished silent video.
        audio_source: The original clip, whose audio track is taken.
        target: Where to write the result.

    Returns:
        The path written, or the video unchanged if it has no audio to
        restore.
    """
    if not has_audio_stream(audio_source):
        return Path(video)

    _ffmpeg(
        "-i",
        str(video),
        "-i",
        str(audio_source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        # Pad audio if shorter so -shortest never truncates the video master,
        # while still cutting audio if it outruns the video track.
        "-af",
        "apad",
        "-shortest",
        str(target),
    )
    return Path(target)


def prepare_dialogue_audio(
    audio_source: Path | str,
    target: Path | str,
) -> Path:
    """Normalizes an audio track to exactly 8.00s 48kHz stereo WAV.

    Args:
        audio_source: The source audio clip.
        target: Where to write the normalized WAV file.

    Returns:
        The target Path.
    """
    _ffmpeg(
        "-i",
        str(audio_source),
        "-t",
        "8.00",
        "-af",
        "apad=whole_dur=8.00",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(target),
    )
    return Path(target)


def prepare_dialogue_frame(
    image_source: Path | str,
    target: Path | str,
    is_portrait: bool = False,
) -> Path:
    """Formats an input image into a 1280x720 frame for dialogue generation.

    If the image is portrait (9:16), it is scaled to 405x720 and centered on
    a 1280x720 black canvas so the model receives a standard landscape frame.

    Args:
        image_source: Path to source image.
        target: Path to save the 1280x720 image.
        is_portrait: True if the target output is 9:16 portrait.

    Returns:
        The target Path.
    """
    if is_portrait:
        filter_str = "scale=405:720,pad=1280:720:(1280-405)/2:0:black"
    else:
        filter_str = (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(1280-iw)/2:(720-ih)/2:black"
        )

    _ffmpeg(
        "-i",
        str(image_source),
        "-vf",
        filter_str,
        str(target),
    )
    return Path(target)


def postprocess_dialogue_video(
    video_source: Path | str,
    target: Path | str,
    is_portrait: bool = False,
) -> Path:
    """Postprocesses generated dialogue video.

    If portrait mode was requested, crops the active center 405x720 region
    and scales it to 720x1280 vertical video.

    Args:
        video_source: Path to the raw 1280x720 video from the model.
        target: Path to save the final video.
        is_portrait: True if portrait post-cropping is required.

    Returns:
        The target Path.
    """
    if is_portrait:
        _ffmpeg(
            "-i",
            str(video_source),
            "-vf",
            "crop=405:720:(1280-405)/2:0,scale=720:1280",
            "-c:a",
            "copy",
            str(target),
        )
    else:
        _ffmpeg(
            "-i",
            str(video_source),
            "-c",
            "copy",
            str(target),
        )
    return Path(target)
