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
"""Tests for screening a gallery row against a VPE capability.

Every case runs against the real capability registry rather than a stub.
The point of screening is to agree with the shipped constraints, and a
fixture capability would let the two drift apart without a test noticing.
"""

import pytest

from src.videos.vpe.capabilities import VpeCapabilityId, get_capability
from src.videos.vpe.gating import (
    VpeScreening,
    accepted_resolution_names,
    screen_stored_video,
)
from src.videos.vpe.preflight import VpeReasonCode

_UPSCALE = get_capability(VpeCapabilityId.UPSCALE)


def _screen(duration: float | None, resolution: str | None):
    """Screens a row against the upscaler.

    Args:
        duration: Stored duration in seconds, or None.
        resolution: Stored resolution name, or None.

    Returns:
        The screening result.
    """
    return screen_stored_video(
        _UPSCALE,
        duration_seconds=duration,
        resolution=resolution,
    )


class TestAcceptedResolutionNames:
    """The registry's exact frame sizes, widened into stored names."""

    def test_upscaler_accepts_the_two_smaller_names(self):
        """1280x720 and 1920x1080 name as 1K and 2K, and nothing else."""
        assert accepted_resolution_names(_UPSCALE) == frozenset({"1K", "2K"})


class TestDurationScreening:
    """Duration is the one rule the row can answer soundly."""

    def test_a_ten_second_clip_is_ruled_out(self):
        """The customer's real case: 240 frames against a 4-8s window."""
        result = _screen(10.0, "1K")
        assert result.screening is VpeScreening.RULED_OUT
        assert result.offer is False
        assert result.findings[0].code is VpeReasonCode.FRAME_COUNT_TOO_HIGH

    def test_a_long_clip_is_told_it_can_be_split(self):
        """Too long has a way out, and the remedy says so."""
        assert "Split" in _screen(10.0, "1K").findings[0].remedy

    def test_a_short_clip_is_not_offered_a_remedy(self):
        """Nothing can be done about two seconds, so nothing is suggested."""
        result = _screen(2.0, "1K")
        assert result.findings[0].code is VpeReasonCode.FRAME_COUNT_TOO_LOW
        assert result.findings[0].remedy == ""

    def test_a_six_second_clip_survives(self):
        """Mid-window, and nothing on the row contradicts the capability."""
        assert _screen(6.0, "1K").screening is VpeScreening.NOT_RULED_OUT

    @pytest.mark.parametrize("duration", [4.0, 8.0])
    def test_the_exact_bounds_survive(self, duration: float):
        """96 and 192 frames are legal, so 4.0s and 8.0s must not be cut."""
        assert _screen(duration, "1K").screening is (VpeScreening.NOT_RULED_OUT)

    @pytest.mark.parametrize("duration", [3.99, 8.01])
    def test_a_rounding_hair_outside_still_survives(self, duration: float):
        """Half a frame of slack, so a float division cannot reject a clip.

        192/24 is exact here, but the stored value is a division and other
        frame rates do not divide cleanly. Rejecting a legal clip for a
        rounding artefact would be the worst kind of failure: invisible, and
        it removes a button.
        """
        assert _screen(duration, "1K").screening is (VpeScreening.NOT_RULED_OUT)

    def test_clearly_outside_the_slack_is_still_ruled_out(self):
        """The slack is half a frame, not a licence to skip the check."""
        assert _screen(8.5, "1K").screening is VpeScreening.RULED_OUT


class TestResolutionScreening:
    """Frame size is stored by name, so it can only reject."""

    def test_a_4k_source_is_ruled_out(self):
        """The upscaler's input is 720p or 1080p; 4K is not an input."""
        result = _screen(6.0, "4K")
        assert result.screening is VpeScreening.RULED_OUT
        assert result.findings[0].code is VpeReasonCode.FRAME_SIZE_UNSUPPORTED

    def test_an_off_spec_size_sharing_a_name_survives(self):
        """1600x900 names as 2K and is not a legal input, and still passes.

        This is the documented direction of error rather than a defect: the
        name is a range and preflight measures the pixels. The test exists so
        that anyone treating NOT_RULED_OUT as "eligible" trips over it.
        """
        assert _screen(6.0, "2K").screening is VpeScreening.NOT_RULED_OUT


class TestUnmeasuredRows:
    """Rows written before video metadata was measured."""

    def test_a_row_with_neither_field_is_unknown(self):
        """Nothing to judge, so judging it would be inventing an answer."""
        assert _screen(None, None).screening is VpeScreening.UNKNOWN

    def test_an_unknown_row_is_still_offered(self):
        """Hiding a capability on a row we cannot read loses the user work."""
        assert _screen(None, None).offer is True

    def test_one_field_is_enough_to_rule_out(self):
        """A missing resolution does not excuse a ten second duration."""
        assert _screen(10.0, None).screening is VpeScreening.RULED_OUT

    def test_one_usable_field_that_passes_is_not_unknown(self):
        """Partial metadata that contradicts nothing is not-ruled-out."""
        assert _screen(6.0, None).screening is VpeScreening.NOT_RULED_OUT


class TestCapabilitiesWithoutVideoInput:
    """Not every capability takes a video."""

    def test_a_still_only_capability_is_ruled_out(self):
        """Dialogue-driven animates a start frame; it takes no video in."""
        capability = get_capability(VpeCapabilityId.DIALOGUE_DRIVEN)
        result = screen_stored_video(
            capability,
            duration_seconds=6.0,
            resolution="1K",
        )
        assert result.screening is VpeScreening.RULED_OUT
        assert result.findings[0].code is VpeReasonCode.MEDIA_KIND_UNSUPPORTED
