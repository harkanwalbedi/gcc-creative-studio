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
"""Tests for the ffmpeg side of split-and-rejoin.

These run ffmpeg for real rather than asserting on the argument vectors.
The failures worth catching here are the ones where a plausible command
produces a subtly wrong file - a segment a frame short, a rejoin that drops
the boundary frame, a master that lost its sound - and none of those are
visible in the arguments.
"""

import json
import subprocess
from pathlib import Path

import pytest

from src.videos.vpe.media_ops import (
    VpeMediaOpError,
    concat_segments,
    cut_segment,
    has_audio_stream,
    restore_audio,
    written_frames,
)

_FPS = 24


def _frame_count(path: Path) -> int:
    """Counts a file's video frames by decoding it.

    Args:
        path: The file to count.

    Returns:
        The number of frames present.
    """
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(json.loads(out.stdout)["streams"][0]["nb_read_frames"])


@pytest.fixture(name="source", scope="module")
def _source(tmp_path_factory) -> Path:
    """Builds a 240 frame clip with an audio track.

    Ten seconds at 24 fps, which is the customer's real shot length and the
    case that has to be split.

    Args:
        tmp_path_factory: pytest's session-scoped directory factory.

    Returns:
        Path to the generated clip.
    """
    root = tmp_path_factory.mktemp("vpe_media_ops")
    target = root / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x180:rate={_FPS}:duration=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-frames:v",
            "240",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


class TestHasAudioStream:
    """Whether there is any sound to preserve."""

    def test_a_clip_with_sound_is_detected(self, source: Path):
        """The fixture carries an AAC track."""
        assert has_audio_stream(source) is True

    def test_a_silent_clip_is_detected(self, source: Path, tmp_path: Path):
        """A clip whose audio was stripped reports no stream."""
        silent = tmp_path / "silent.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-an",
                "-c:v",
                "copy",
                str(silent),
            ],
            check=True,
            capture_output=True,
        )
        assert has_audio_stream(silent) is False

    def test_an_unreadable_file_raises(self, tmp_path: Path):
        """A missing file is an error, not silently "no audio"."""
        with pytest.raises(VpeMediaOpError):
            has_audio_stream(tmp_path / "nothing.mp4")


class TestCutSegment:
    """A segment has to hold exactly the frames it was asked for."""

    def test_the_frame_count_is_exact(self, source: Path, tmp_path: Path):
        """120 frames requested is 120 frames written, not 119 or 121."""
        target = cut_segment(
            source,
            tmp_path / "a.mp4",
            first_frame=0,
            frames=120,
            fps=_FPS,
        )
        assert _frame_count(target) == 120

    def test_a_mid_clip_cut_is_exact(self, source: Path, tmp_path: Path):
        """Cutting from frame 120 is where an off-by-one would show."""
        target = cut_segment(
            source,
            tmp_path / "b.mp4",
            first_frame=120,
            frames=120,
            fps=_FPS,
        )
        assert _frame_count(target) == 120

    def test_segments_are_silent(self, source: Path, tmp_path: Path):
        """Audio is never split, so it must not travel with a segment."""
        target = cut_segment(
            source,
            tmp_path / "c.mp4",
            first_frame=0,
            frames=96,
            fps=_FPS,
        )
        assert has_audio_stream(target) is False

    def test_a_cut_running_past_the_end_raises(
        self,
        source: Path,
        tmp_path: Path,
    ):
        """ffmpeg exits zero having written whatever it could.

        Asking for 120 frames from frame 200 of a 240 frame clip writes 40.
        Left unchecked that segment reaches the API under the 96 frame
        minimum, after its siblings have already been submitted and billed.
        """
        with pytest.raises(VpeMediaOpError, match="wrote 40"):
            cut_segment(
                source,
                tmp_path / "past_the_end.mp4",
                first_frame=200,
                frames=120,
                fps=_FPS,
            )

    def test_a_container_that_over_declares_its_length_raises(
        self,
        source: Path,
        tmp_path: Path,
    ):
        """Regression: the planner and the cut can measure different films.

        An ``ffmpeg -ss ... -c copy`` trim leaves an mp4 edit list behind.
        The track still holds every sample, so the container - which is what
        the planner divides up - keeps declaring the original length, while
        the decoder presents the shorter film ``select`` actually cuts. A
        plan of two 120 frame halves then has a second half that is 96
        frames long and nothing says so.
        """
        trimmed = tmp_path / "trimmed.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-ss",
                "1",
                "-i",
                str(source),
                "-c",
                "copy",
                str(trimmed),
            ],
            check=True,
            capture_output=True,
        )
        # The premise. If ffmpeg ever stops writing the edit list this test
        # is measuring nothing, and should say so rather than pass quietly.
        assert written_frames(trimmed) == 240
        assert _frame_count(trimmed) < 240

        # The first half is entirely inside the film that survives, so the
        # split gets underway looking healthy.
        cut_segment(
            source=trimmed,
            target=tmp_path / "half_0.mp4",
            first_frame=0,
            frames=120,
            fps=_FPS,
        )
        with pytest.raises(VpeMediaOpError, match="re-encode it"):
            cut_segment(
                source=trimmed,
                target=tmp_path / "half_1.mp4",
                first_frame=120,
                frames=120,
                fps=_FPS,
            )


class TestWrittenFrames:
    """The post-condition's own measurement."""

    def test_it_reads_the_count_ffmpeg_wrote(self, source: Path):
        """The fixture is 240 frames and declares as much."""
        assert written_frames(source) == 240

    def test_a_file_with_no_video_stream_counts_zero(self, tmp_path: Path):
        """Zero rather than an error, so the caller's own check reports it.

        A cut that produced no video stream at all is a failed cut, and it
        should be described as the wrong frame count like any other.
        """
        audio_only = tmp_path / "audio_only.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "aac",
                str(audio_only),
            ],
            check=True,
            capture_output=True,
        )
        assert written_frames(audio_only) == 0

    def test_an_unreadable_file_raises(self, tmp_path: Path):
        """A missing file is an error, not a count of zero."""
        with pytest.raises(VpeMediaOpError):
            written_frames(tmp_path / "nothing.mp4")


class TestConcatSegments:
    """The rejoin must return exactly the source's frames."""

    def test_two_halves_rejoin_to_the_whole(
        self,
        source: Path,
        tmp_path: Path,
    ):
        """240 frames out as 120 + 120 must come back as 240."""
        first = cut_segment(
            source, tmp_path / "s0.mp4", first_frame=0, frames=120, fps=_FPS
        )
        second = cut_segment(
            source, tmp_path / "s1.mp4", first_frame=120, frames=120, fps=_FPS
        )
        joined = concat_segments([first, second], tmp_path / "joined.mp4")
        assert _frame_count(joined) == 240

    def test_three_segments_rejoin(self, source: Path, tmp_path: Path):
        """More than one seam is the case a two-piece test would miss."""
        pieces = [
            cut_segment(
                source,
                tmp_path / f"t{index}.mp4",
                first_frame=index * 80,
                frames=80,
                fps=_FPS,
            )
            for index in range(3)
        ]
        joined = concat_segments(pieces, tmp_path / "joined3.mp4")
        assert _frame_count(joined) == 240

    def test_it_works_from_an_unrelated_working_directory(
        self,
        source: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: the concat list needs absolute paths.

        ffmpeg's concat demuxer resolves each ``file`` entry relative to the
        listing, not the process working directory, so relative entries
        break as soon as the two differ. This failed in the field before the
        paths were resolved.
        """
        first = cut_segment(
            source, tmp_path / "r0.mp4", first_frame=0, frames=120, fps=_FPS
        )
        second = cut_segment(
            source, tmp_path / "r1.mp4", first_frame=120, frames=120, fps=_FPS
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        joined = concat_segments([first, second], tmp_path / "rel.mp4")
        assert _frame_count(joined) == 240

    def test_an_empty_list_is_refused(self, tmp_path: Path):
        """Concatenating nothing should not produce an empty master."""
        with pytest.raises(VpeMediaOpError, match="Nothing"):
            concat_segments([], tmp_path / "empty.mp4")


class TestRestoreAudio:
    """A 4K master that plays silent is the failure this prevents."""

    def test_sound_comes_back(self, source: Path, tmp_path: Path):
        """The rejoined video is silent until the original track is laid on."""
        segment = cut_segment(
            source, tmp_path / "v.mp4", first_frame=0, frames=240, fps=_FPS
        )
        assert has_audio_stream(segment) is False
        restored = restore_audio(segment, source, tmp_path / "final.mp4")
        assert has_audio_stream(restored) is True

    def test_the_picture_is_not_touched(self, source: Path, tmp_path: Path):
        """Audio is encoded, video is copied, so the frames must survive."""
        segment = cut_segment(
            source, tmp_path / "v2.mp4", first_frame=0, frames=240, fps=_FPS
        )
        restored = restore_audio(segment, source, tmp_path / "final2.mp4")
        assert _frame_count(restored) == 240

    def test_a_silent_source_returns_the_video_unchanged(
        self,
        source: Path,
        tmp_path: Path,
    ):
        """Nothing to restore, so no needless remux of the master."""
        silent_source = tmp_path / "silent_src.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-an",
                "-c:v",
                "copy",
                str(silent_source),
            ],
            check=True,
            capture_output=True,
        )
        video = cut_segment(
            source, tmp_path / "v3.mp4", first_frame=0, frames=96, fps=_FPS
        )
        result = restore_audio(video, silent_source, tmp_path / "final3.mp4")
        assert result == video
        assert not (tmp_path / "final3.mp4").exists()

    def test_audio_shorter_than_video_does_not_truncate_the_master(
        self,
        tmp_path: Path,
    ):
        """Regression: ``-shortest`` alone cuts the master to the audio.

        A field clip surfaced an audio track shorter than its video by more
        than a frame - not the few-millisecond overrun this module was built
        for, but the opposite direction. Without padding, ``-shortest``
        picked the audio as the shorter stream and the 4K master came back
        missing the frames past where the sound ran out. ``apad`` closes
        that gap so the video length always wins.
        """
        video = tmp_path / "video.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size=320x180:rate={_FPS}:duration=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-frames:v",
                "240",
                str(video),
            ],
            check=True,
            capture_output=True,
        )
        short_audio_source = tmp_path / "short_audio.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=5",
                "-c:a",
                "aac",
                str(short_audio_source),
            ],
            check=True,
            capture_output=True,
        )
        restored = restore_audio(
            video, short_audio_source, tmp_path / "final4.mp4"
        )
        assert _frame_count(restored) == 240
