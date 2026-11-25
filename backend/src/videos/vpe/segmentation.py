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
"""Divides a clip into pieces a capability will accept.

The upscaler takes 4 to 8 seconds. Production shots are routinely 9 or 10,
so most of a real library is outside the window and the only answer that
returns a full-length master is to split the shot, upscale the pieces
independently and rejoin them.

That the rejoin is invisible is measured rather than assumed - see
``scripts/vpe_seam_check.py`` and section 6g of the integration plan. This
module is only concerned with choosing the cut points, and it is shared by
the measurement script and the service so the thing that was verified is the
thing that runs.

Bounds come from the capability registry rather than constants here. The
upscaler's window is expressed in frames because that is what the API
validates, and frames are what this module divides.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.videos.vpe.capabilities import VpeVideoConstraints


class VpeSegmentationError(ValueError):
    """Raised when a clip cannot be divided into acceptable segments."""


@dataclass(frozen=True, slots=True)
class VpeSegment:
    """One piece of a source clip, as a half-open frame range.

    Attributes:
        index: Position in the sequence, from zero.
        first_frame: Index of the first frame kept, zero based.
        frames: How many frames this segment holds.
    """

    index: int
    first_frame: int
    frames: int

    @property
    def last_frame(self) -> int:
        """Returns the index of the final frame kept, inclusive."""
        return self.first_frame + self.frames - 1

    def seconds(self, fps: int) -> float:
        """Returns the segment's length in seconds.

        Args:
            fps: Frame rate to divide by.

        Returns:
            The length in seconds.
        """
        return self.frames / fps


def plan_segments(
    frames: int,
    constraints: VpeVideoConstraints,
    *,
    preferred_frames: int | None = None,
) -> tuple[VpeSegment, ...]:
    """Divides a frame count into segments the capability will accept.

    Segments are balanced rather than greedy. Filling each piece to the
    maximum and leaving a remainder is the obvious approach and it is wrong
    twice over: 240 frames at a flat 192 leaves a 48 frame tail, which is
    below the 96 frame minimum and so illegal, and even where a remainder is
    legal a much shorter final piece is a piece whose grain has every reason
    to differ from its neighbours. Equal halves avoid both.

    Args:
        frames: Total frames in the source clip.
        constraints: The capability's input video constraints.
        preferred_frames: Segment length to aim for, defaulting to the
            longest the capability accepts. Only a preference - it is
            overridden wherever it would produce an illegal division.

    Returns:
        Segments covering every frame exactly once, in order. A clip already
        inside the window returns as a single segment.

    Raises:
        VpeSegmentationError: If the clip is shorter than the minimum, or no
            division into legal segments exists.
    """
    if frames < 1:
        raise VpeSegmentationError(f"A clip must have frames, got {frames}.")

    if frames < constraints.min_frames:
        # Padding a short clip up to the minimum would invent content and
        # hand it back as if it were the source, so this is refused rather
        # than worked around.
        raise VpeSegmentationError(
            f"{frames} frames is below the {constraints.min_frames} frame"
            f" minimum ({constraints.min_seconds:g}s), and a clip cannot be"
            " lengthened to fit.",
        )

    if frames <= constraints.max_frames:
        return (VpeSegment(index=0, first_frame=0, frames=frames),)

    # How many segments are possible at all, before any preference is
    # considered. Too few and some segment must exceed the maximum; too many
    # and some segment must fall below the minimum.
    fewest = -(-frames // constraints.max_frames)
    most = frames // constraints.min_frames
    if fewest > most:
        raise VpeSegmentationError(
            f"{frames} frames cannot be divided into segments of between"
            f" {constraints.min_frames} and {constraints.max_frames}"
            " frames: no segment count satisfies both bounds.",
        )

    wanted = preferred_frames or constraints.max_frames
    wanted = max(constraints.min_frames, min(wanted, constraints.max_frames))
    # The preference only chooses among counts that already work. Honouring
    # it outside that range would produce illegal segments, so it is a
    # preference in the literal sense rather than an instruction.
    count = min(most, max(fewest, -(-frames // wanted)))

    base, extra = divmod(frames, count)
    segments: list[VpeSegment] = []
    start = 0
    for index in range(count):
        # The remainder is spread one frame at a time across the leading
        # segments, so lengths differ by at most a single frame.
        length = base + (1 if index < extra else 0)
        segments.append(
            VpeSegment(index=index, first_frame=start, frames=length),
        )
        start += length
    return tuple(segments)
