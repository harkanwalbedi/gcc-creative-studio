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
"""Tests for Media Utils."""


from unittest.mock import MagicMock, patch

import pytest

from src.common.media_utils import (
    concatenate_videos,
    generate_image_thumbnail_bytes,
    generate_image_thumbnail_from_gcs,
    generate_thumbnail,
    get_video_dimensions,
    get_video_metadata,
)


def test_generate_image_thumbnail_bytes_success():
    with patch("src.common.media_utils.PILImage.open") as mock_open:
        mock_img = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_img

        # mock_img.save should write to output
        def fake_save(output, format, **kwargs):
            output.write(b"thumbdata")

        mock_img.save.side_effect = fake_save
        mock_img.mode = "RGB"

        res = generate_image_thumbnail_bytes(b"fakebytes", "image/png")

        assert res == b"thumbdata"
        mock_img.thumbnail.assert_called_once_with((512, 512))


def test_generate_image_thumbnail_bytes_error():
    with patch("src.common.media_utils.PILImage.open") as mock_open:
        mock_open.side_effect = Exception("Pillow Error")
        res = generate_image_thumbnail_bytes(b"fakebytes", "image/png")
        assert res is None


def test_generate_image_thumbnail_from_gcs_success():
    mock_gcs = MagicMock()
    mock_gcs.download_bytes_from_gcs.return_value = b"fakeimage"
    mock_gcs.upload_bytes_to_gcs.return_value = (
        "gs://bucket/image_thumbnail.png"
    )

    with patch(
        "src.common.media_utils.generate_image_thumbnail_bytes",
    ) as mock_gen_bytes:
        mock_gen_bytes.return_value = b"fakethumb"

        res = generate_image_thumbnail_from_gcs(
            mock_gcs,
            "gs://bucket/image.png",
            "image/png",
        )

        assert res == "gs://bucket/image_thumbnail.png"
        mock_gcs.upload_bytes_to_gcs.assert_called_once_with(
            b"fakethumb",
            "image_thumbnail.png",
            "image/png",
        )


def test_generate_image_thumbnail_from_gcs_download_fail():
    mock_gcs = MagicMock()
    mock_gcs.download_bytes_from_gcs.return_value = None
    res = generate_image_thumbnail_from_gcs(
        mock_gcs,
        "gs://bucket/image.png",
        "image/png",
    )
    assert res is None


def test_generate_thumbnail_video_success():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0

        res = generate_thumbnail("/tmp/video.mp4")

        assert res == "/tmp/thumbnail_video.png"
        mock_run.assert_called_once()


def test_generate_thumbnail_video_empty():
    res = generate_thumbnail("")
    assert res is None


def test_concatenate_videos_success():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        with patch(
            "src.common.media_utils.open", create=True
        ) as mock_file_open:
            mock_run.return_value.returncode = 0
            res = concatenate_videos(
                ["/tmp/v1.mp4", "/tmp/v2.mp4"], "/tmp/output.mp4"
            )
            assert res == "/tmp/output.mp4"
            mock_run.assert_called_once()


def test_concatenate_videos_too_few():
    res = concatenate_videos(["/tmp/v1.mp4"], "/tmp/output.mp4")
    assert res is None


def test_get_video_dimensions_success():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        mock_run.return_value.stdout = (
            '{"streams": [{"width": 1920, "height": 1080}]}'
        )

        width, height = get_video_dimensions("/tmp/video.mp4")

        assert width == 1920
        assert height == 1080


def test_generate_image_thumbnail_from_gcs_exception():
    mock_gcs = MagicMock()
    mock_gcs.download_bytes_from_gcs.side_effect = Exception("Generic Error")
    res = generate_image_thumbnail_from_gcs(
        mock_gcs,
        "gs://bucket/image.png",
        "image/png",
    )
    assert res is None


def test_generate_thumbnail_ffmpeg_not_found():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError()
        res = generate_thumbnail("/tmp/video.mp4")
        assert res is None


def test_generate_thumbnail_called_process_error():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(
            1, "cmd", stderr="error"
        )
        res = generate_thumbnail("/tmp/video.mp4")
        assert res is None


def test_concatenate_videos_ffmpeg_not_found():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        with patch(
            "src.common.media_utils.open", create=True
        ) as mock_file_open:
            mock_run.side_effect = FileNotFoundError()
            res = concatenate_videos(
                ["/tmp/v1.mp4", "/tmp/v2.mp4"], "/tmp/output.mp4"
            )
            assert res is None


def test_concatenate_videos_called_process_error():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        with patch(
            "src.common.media_utils.open", create=True
        ) as mock_file_open:
            import subprocess

            mock_run.side_effect = subprocess.CalledProcessError(
                1,
                "cmd",
                stderr="error",
            )
            res = concatenate_videos(
                ["/tmp/v1.mp4", "/tmp/v2.mp4"], "/tmp/output.mp4"
            )
            assert res is None


def test_get_video_metadata_counts_frames_rather_than_trusting_the_container():
    """A nominal 10s clip declares 10.005s but holds exactly 240 frames.

    The padding differs from clip to clip, so a rounded container duration
    reads 10 for one clip and 9 for the next.
    """
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        mock_run.return_value.stdout = """
            {
                "streams": [
                    {
                        "width": 1920,
                        "height": 1080,
                        "nb_frames": "240",
                        "avg_frame_rate": "24/1",
                        "duration": "10.005000"
                    }
                ],
                "format": {"duration": "10.005000"}
            }
        """

        metadata = get_video_metadata("/tmp/video.mp4")

        assert metadata == {
            "width": 1920,
            "height": 1080,
            "duration_seconds": 10.0,
        }


def test_get_video_metadata_handles_a_fractional_frame_rate():
    """29.97fps is 30000/1001, and 300 frames of it run just over 10s."""
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        mock_run.return_value.stdout = (
            '{"streams": [{"width": 1280, "height": 720, '
            '"nb_frames": "300", "avg_frame_rate": "30000/1001"}]}'
        )

        metadata = get_video_metadata("/tmp/video.mp4")

        assert metadata["duration_seconds"] == pytest.approx(10.01, abs=0.001)


def test_get_video_metadata_falls_back_to_the_declared_duration():
    """Fragmented files carry no frame count at all."""
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        mock_run.return_value.stdout = (
            '{"streams": [{"width": 1920, "height": 1080, '
            '"avg_frame_rate": "0/0"}], "format": {"duration": "8.024000"}}'
        )

        metadata = get_video_metadata("/tmp/video.mp4")

        assert metadata["duration_seconds"] == 8.024


def test_get_video_metadata_returns_none_when_the_file_cannot_be_probed():
    with patch("src.common.media_utils.subprocess.run") as mock_run:
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(
            1,
            "cmd",
            stderr="error",
        )
        assert get_video_metadata("/tmp/missing.mp4") is None
