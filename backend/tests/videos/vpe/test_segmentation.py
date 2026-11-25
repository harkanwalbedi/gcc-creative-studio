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
"""Tests for dividing a clip into upscalable segments."""

import pytest

from src.videos.vpe.capabilities import VpeCapabilityId, get_capability
from src.videos.vpe.segmentation import (
    VpeSegmentationError,
    plan_segments,
)

_UPSCALE = get_capability(VpeCapabilityId.UPSCALE).input_video


def _plan(frames: int, **kwargs):
    """Plans segments for the upscaler.

    Args:
        frames: Total frames in the source.
        **kwargs: Passed through to plan_segments.

    Returns:
        The planned segments.
    """
    return plan_segments(frames, _UPSCALE, **kwargs)


class TestCoverage:
    """Whatever the division, every frame must appear exactly once."""

    @pytest.mark.parametrize(
        "frames",
        [96, 120, 192, 193, 200, 240, 288, 384, 385, 500, 1000],
    )
    def test_segments_tile_the_source_exactly(self, frames: int):
        """No gap and no overlap, or the rejoin drops or repeats frames."""
        segments = _plan(frames)
        assert segments[0].first_frame == 0
        assert sum(segment.frames for segment in segments) == frames
        for earlier, later in zip(segments, segments[1:]):
            assert later.first_frame == earlier.last_frame + 1

    @pytest.mark.parametrize(
        "frames",
        [96, 120, 192, 193, 200, 240, 288, 384, 385, 500, 1000],
    )
    def test_every_segment_is_legal(self, frames: int):
        """A segment outside the window is a job the API will reject."""
        for segment in _plan(frames):
            assert _UPSCALE.accepts_frame_count(segment.frames)

    @pytest.mark.parametrize("frames", [96, 120, 192, 240, 385, 1000])
    def test_indices_are_sequential(self, frames: int):
        """The index is what names the piece's file and orders the rejoin."""
        segments = _plan(frames)
        assert [s.index for s in segments] == list(range(len(segments)))


class TestNoSplitNeeded:
    """A clip already inside the window is left alone."""

    @pytest.mark.parametrize("frames", [96, 120, 144, 192])
    def test_a_legal_clip_is_one_segment(self, frames: int):
        """Splitting an 8 second clip would add a seam for no reason."""
        segments = _plan(frames)
        assert len(segments) == 1
        assert segments[0].frames == frames


class TestBalance:
    """Balanced beats greedy, and the reason is not aesthetic."""

    def test_ten_seconds_splits_into_equal_halves(self):
        """The customer's real case: 240 frames as 120 and 120."""
        assert [s.frames for s in _plan(240)] == [120, 120]

    def test_greedy_would_have_been_illegal_here(self):
        """Filling to 192 first would leave a 48 frame tail, below the min.

        This is the case that makes balance a correctness requirement rather
        than a preference, so it is asserted directly.
        """
        segments = _plan(240)
        assert all(s.frames >= _UPSCALE.min_frames for s in segments)
        assert 192 not in [s.frames for s in segments]

    def test_lengths_differ_by_at_most_one_frame(self):
        """An indivisible count spreads the remainder rather than dumping it."""
        lengths = [s.frames for s in _plan(385)]
        assert max(lengths) - min(lengths) <= 1

    def test_a_long_clip_uses_the_fewest_segments_it_can(self):
        """Each extra segment is another paid job and another seam."""
        assert len(_plan(384)) == 2
        assert len(_plan(385)) == 3


class TestPreferredLength:
    """The caller can ask for shorter pieces, within reason."""

    def test_a_preference_is_honoured(self):
        """Asking for 120 frame pieces of a 240 frame clip gives two."""
        assert len(_plan(240, preferred_frames=120)) == 2

    def test_a_preference_below_the_minimum_is_clamped(self):
        """A 1 frame preference must not produce 240 illegal segments."""
        for segment in _plan(240, preferred_frames=1):
            assert _UPSCALE.accepts_frame_count(segment.frames)

    def test_a_preference_above_the_maximum_is_clamped(self):
        """Asking for more than the API accepts cannot widen the window."""
        for segment in _plan(240, preferred_frames=10_000):
            assert _UPSCALE.accepts_frame_count(segment.frames)


class TestRefusals:
    """What cannot be done is refused rather than approximated."""

    def test_a_clip_below_the_minimum_is_refused(self):
        """Padding to reach 96 frames would invent content."""
        with pytest.raises(VpeSegmentationError, match="below"):
            _plan(50)

    def test_the_message_names_both_numbers(self):
        """A refusal a user sees should say what it got and what it needs."""
        with pytest.raises(VpeSegmentationError) as caught:
            _plan(50)
        assert "50" in str(caught.value)
        assert "96" in str(caught.value)

    @pytest.mark.parametrize("frames", [0, -1])
    def test_a_clip_with_no_frames_is_refused(self, frames: int):
        """An unmeasurable clip must not silently become one empty segment."""
        with pytest.raises(VpeSegmentationError):
            _plan(frames)


class TestSegmentGeometry:
    """The frame arithmetic the cut depends on."""

    def test_last_frame_is_inclusive(self):
        """A 120 frame segment from 0 ends at 119, not 120."""
        assert _plan(240)[0].last_frame == 119

    def test_the_second_segment_starts_where_the_first_ended(self):
        """Off by one here duplicates or drops a frame at the join."""
        first, second = _plan(240)
        assert second.first_frame == first.last_frame + 1 == 120

    def test_seconds_divides_by_the_frame_rate(self):
        """120 frames at 24 fps is 5 seconds."""
        assert _plan(240)[0].seconds(24) == 5.0
