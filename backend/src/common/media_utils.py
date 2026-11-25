# Copyright 2025 Google LLC
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
"""Media processing and utility functions."""

import io
import json
import logging
import os
import pathlib
import re
import subprocess
from fractions import Fraction
from urllib.parse import parse_qs, urlparse

from PIL import Image as PILImage

from google.api_core import exceptions

from src.common.storage_service import GcsService

logger = logging.getLogger(__name__)

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_video_id(url: str | None) -> str | None:
    """Extracts the 11-character YouTube video ID from a URL if valid."""
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None

    if parsed.scheme not in ("http", "https"):
        return None
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None

    video_id: str | None = None
    if hostname == "youtu.be" or hostname.endswith(".youtu.be"):
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            video_id = segments[0]
    elif (
        hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
        or hostname == "youtube-nocookie.com"
        or hostname.endswith(".youtube-nocookie.com")
    ):
        segments = [seg for seg in parsed.path.split("/") if seg]
        if not segments or segments[0] == "watch":
            qs = parse_qs(parsed.query)
            v_vals = qs.get("v")
            if v_vals:
                video_id = v_vals[0]
        elif (
            segments[0] in ("shorts", "embed", "live", "v")
            and len(segments) >= 2
        ):
            video_id = segments[1]

    if video_id and _YOUTUBE_ID_RE.fullmatch(video_id):
        return video_id
    return None


def get_youtube_aspect_ratio(url: str | None) -> str:
    """Infers the aspect ratio ('9:16' for Shorts, else '16:9') from a YouTube URL."""
    if not url or not isinstance(url, str):
        return "16:9"
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return "16:9"
    segments = [seg.lower() for seg in parsed.path.split("/") if seg]
    if segments and segments[0] == "shorts":
        return "9:16"
    return "16:9"


def generate_image_thumbnail_bytes(
    image_bytes: bytes, mime_type: str
) -> bytes | None:
    """Generates a thumbnail from image bytes using PIL.

    Args:
        image_bytes: The raw bytes of the image.
        mime_type: The mime type of the image (e.g., 'image/png', 'image/jpeg').

    Returns:
        The raw bytes of the generated thumbnail, or None if it fails.

    """
    try:
        with PILImage.open(io.BytesIO(image_bytes)) as img:
            # Convert to RGB if RGBA and saving as JPEG
            if mime_type == "image/jpeg" and img.mode == "RGBA":
                img = img.convert("RGB")

            img.thumbnail((512, 512))
            output = io.BytesIO()

            format_to_save = "PNG" if mime_type == "image/png" else "JPEG"
            img.save(output, format=format_to_save, optimize=True)
            return output.getvalue()
    except Exception as e:
        logger.error("Error generating image thumbnail: %s", e)
        return None


def generate_image_thumbnail_from_gcs(
    gcs_service: GcsService,
    gcs_uri: str,
    mime_type: str,
) -> str | None:
    """Generates a thumbnail for the given GCS URI and uploads it.

    Args:
        gcs_service: The GcsService instance to use for download/upload.
        gcs_uri: The GCS URI of the source image.
        mime_type: The mime type of the image.

    Returns:
        The GCS URI of the generated thumbnail, or None if it fails.

    """
    try:
        image_bytes = gcs_service.download_bytes_from_gcs(gcs_uri)
        if not image_bytes:
            return None

        thumbnail_bytes = generate_image_thumbnail_bytes(image_bytes, mime_type)
        if not thumbnail_bytes:
            return None

        if not gcs_uri.startswith("gs://"):
            return None

        # gs://bucket/blob_name
        parts = gcs_uri.split("/", 3)  # gs:, , bucket, blob_name
        if len(parts) < 4:
            return None
        blob_name = parts[3]

        path = pathlib.Path(blob_name)
        # Use simple string manipulation to avoid path issues on different OS
        # if needed, pathlib is generally fine.
        new_blob_name = str(path.parent / f"{path.stem}_thumbnail{path.suffix}")
        if path.parent == pathlib.Path():
            new_blob_name = f"{path.stem}_thumbnail{path.suffix}"

        return gcs_service.upload_bytes_to_gcs(
            thumbnail_bytes,
            new_blob_name,
            mime_type,
        )

    except Exception as e:
        logger.error("Thumbnail generation failed: %s", e)
        return None


def generate_thumbnail(video_path: str) -> str | None:
    """Generates a thumbnail from a video file using ffmpeg.

    Args:
        video_path: The path to the video file.

    Returns:
        The path to the generated thumbnail, or None if it fails.

    """
    if not video_path:
        return None

    thumbnail_filename = (
        "thumbnail_"
        + os.path.splitext(os.path.basename(video_path))[0]
        + ".png"
    )
    thumbnail_path = os.path.join(
        os.path.dirname(video_path), thumbnail_filename
    )

    command = [
        "ffmpeg",
        "-i",
        video_path,
        "-ss",
        "00:00:00.000",  # Capture frame at 0 milisecond
        "-vframes",
        "1",
        "-y",  # Overwrite output file if it exists
        thumbnail_path,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return thumbnail_path
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found. Please ensure ffmpeg is installed and in "
            "your PATH.",
        )
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Error generating thumbnail: %s", e.stderr)
        return None


def concatenate_videos(video_paths: list[str], output_path: str) -> str | None:
    """Concatenates multiple video files into a single file using ffmpeg.

    Args:
        video_paths: An ordered list of local paths to the video files to be
            joined.
        output_path: The local path for the final concatenated video.

    Returns:
        The path to the concatenated video, or None on failure.

    """
    if not video_paths or len(video_paths) < 2:
        logger.error("Concatenation requires at least two video files.")
        return None

    # Create a temporary file to list the input videos for ffmpeg
    list_file_path = os.path.join(
        os.path.dirname(output_path), "concat_list.txt"
    )
    with open(list_file_path, "w", encoding="utf-8") as f:
        for path in video_paths:
            absolute_path = os.path.abspath(path)
            # ffmpeg requires file paths to be escaped
            f.write(f"file '{absolute_path}'\n")

    command = [
        "ffmpeg",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file_path,
        "-c",
        "copy",  # Copy codecs to avoid re-encoding, which is much faster
        "-y",  # Overwrite output file if it exists
        output_path,
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info("Successfully concatenated videos to %s", output_path)
        return output_path
    except FileNotFoundError:
        logger.error(
            "ffmpeg not found. Please ensure ffmpeg is installed and in "
            "your PATH.",
        )
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Error concatenating videos: %s", e.stderr)
        return None
    finally:
        # Clean up the temporary list file
        if os.path.exists(list_file_path):
            os.remove(list_file_path)


def get_video_dimensions(video_path: str) -> tuple[int, int]:
    """Uses ffprobe to get the width and height of a video file."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        video_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    width = data["streams"][0]["width"]
    height = data["streams"][0]["height"]
    return width, height


def _measure_duration_seconds(stream: dict, container: dict) -> float | None:
    """Works out how long a video stream runs, in seconds.

    Args:
        stream: The ffprobe entry for the first video stream.
        container: The ffprobe entry for the file as a whole.

    Returns:
        The stream's length in seconds, or None if neither the frames nor a
        declared duration could be read.

    """
    try:
        frames = int(stream.get("nb_frames", 0))
        frame_rate = float(Fraction(stream.get("avg_frame_rate", "0")))
        if frames > 0 and frame_rate > 0:
            return frames / frame_rate
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # Fragmented files carry no frame count, so fall back to whatever the file
    # declares even though that value is the padded one.
    declared = stream.get("duration") or container.get("duration")
    try:
        return float(declared)
    except (TypeError, ValueError):
        return None


def get_video_metadata(video_path: str) -> dict | None:
    """Uses ffprobe to measure what a video file actually contains.

    The length is counted in frames rather than read off the container. A
    nominal 10s clip at 24fps holds 240 frames but declares 10.005s, and the
    padding differs from clip to clip, so rounding the declared value gives 10
    for one clip and 9 for the next. Frames over frame rate is the length that
    was encoded.

    Args:
        video_path: Path to a local video file.

    Returns:
        A dict with "width", "height" and "duration_seconds" (any of which may
        be None), or None if the file cannot be probed at all - a caller is
        better off keeping what it already had than recording a guess.

    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,nb_frames,avg_frame_rate,duration:"
        "format=duration",
        "-of",
        "json",
        video_path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "duration_seconds": _measure_duration_seconds(
                stream,
                data.get("format") or {},
            ),
        }
    except Exception as e:
        logger.error("Error probing video %s: %s", video_path, e)
        return None


def strip_audio(video_path: str, output_path: str) -> str | None:
    """Writes a copy of a video with its audio track removed.

    Gemini Omni refuses to edit a clip that carries speech when reference
    images are also supplied: "The model is currently unable to process speech
    edits." Every Omni-generated clip has a native audio track, so compositing
    a character into one of its own outputs fails unless the audio is dropped
    first.

    The video stream is copied rather than re-encoded, so this is fast and
    lossless.

    Returns:
        The path to the silent copy, or None on failure.

    """
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        video_path,
        "-an",  # drop audio
        "-c:v",
        "copy",  # no re-encode
        output_path,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info("Stripped audio from %s", video_path)
        return output_path
    except FileNotFoundError:
        logger.error("ffmpeg not found. Cannot strip audio.")
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Error stripping audio: %s", e.stderr)
        return None
