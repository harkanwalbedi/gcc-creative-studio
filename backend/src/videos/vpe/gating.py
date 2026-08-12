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
"""Decides from a gallery row alone whether to offer a VPE capability.

``preflight`` answers the same question properly, but it answers it by
running ffprobe over the file, which means downloading it. A gallery of a
few hundred clips cannot pay that to decide which buttons to draw, so this
module answers a deliberately weaker question from the columns already on
the row.

The weakness is the point, and it is asymmetric. Of the upscaler's three
input rules, the row can speak to exactly one:

* **Duration** is stored, and stored well. ``duration_seconds`` is measured
  as frames over frame rate rather than read off the container, so a
  240-frame clip is 10.0 and not the 10.005 its container declares. A
  duration outside the window is therefore a sound rejection.
* **Frame size** is stored only as a name - ``resolution`` holds "1K", "2K"
  or "4K", resolved from the long edge. "2K" means the long edge is between
  1281 and 1920 pixels; it does not mean 1920x1080. So the name can rule a
  clip out and can never rule one in.
* **Frame rate** is not stored at all. The upscaler needs exactly 24 fps and
  nothing on the row knows the fps.

So this returns ``RULED_OUT`` or ``NOT_RULED_OUT``, never "eligible".
Anything that survives here still has to clear ``preflight.validate`` on the
real file before a paid job is submitted. Naming the states this way is not
pedantry: a UI that reads ``NOT_RULED_OUT`` as "this will work" and skips
preflight would submit off-spec clips to a 315-second billable job and
surface the failure five minutes later.

There is a third state. Rows written before video metadata was measured
carry no ``resolution`` and a ``duration_seconds`` copied off the request
rather than the result, and an extension asks for seven seconds whatever
was requested. Screening those on their stored values would grey out
buttons on the strength of numbers that were never true. They return
``UNKNOWN``, which the UI should treat as offer-and-let-preflight-decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.videos.vpe.capabilities import VpeCapability
from src.videos.vpe.preflight import VpeFinding, VpeReasonCode, VpeSeverity

# Resolution names as stored on the row, in ascending order of long edge.
# Mirrors veo_service.MEASURED_RESOLUTION_BY_LONG_EDGE. Kept as a local
# ordering rather than an import because this module needs the ceilings, not
# the naming function, and importing veo_service here would pull the whole
# generation service into a screening call.
_LONG_EDGE_CEILINGS: tuple[tuple[int, str], ...] = (
    (1280, "1K"),
    (1920, "2K"),
)
_LARGEST_NAME = "4K"


class VpeScreening(str, Enum):
    """How much a stored row can say about a capability's input rules."""

    RULED_OUT = "ruled_out"
    NOT_RULED_OUT = "not_ruled_out"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class VpeScreeningResult:
    """The outcome of screening one row against one capability.

    Attributes:
        capability_id: The capability the row was screened against.
        screening: Whether the row rules the capability out, fails to rule it
            out, or lacks the metadata to say.
        findings: Why, in the same vocabulary ``preflight`` uses, so a UI can
            render a screening reason and a preflight reason identically.
    """

    capability_id: str
    screening: VpeScreening
    findings: tuple[VpeFinding, ...] = ()

    @property
    def offer(self) -> bool:
        """Returns True if the capability should be offered on this row.

        Both ``NOT_RULED_OUT`` and ``UNKNOWN`` are offered. An unknown row is
        one this module cannot judge, and refusing to offer it would hide a
        capability that may well work.
        """
        return self.screening is not VpeScreening.RULED_OUT


def _name_long_edge(long_edge: int) -> str:
    """Names a long edge in the vocabulary stored on the row.

    Args:
        long_edge: The longer of a frame's two dimensions, in pixels.

    Returns:
        One of "1K", "2K" or "4K".
    """
    for ceiling, name in _LONG_EDGE_CEILINGS:
        if long_edge <= ceiling:
            return name
    return _LARGEST_NAME


def accepted_resolution_names(capability: VpeCapability) -> frozenset[str]:
    """Names every stored resolution a capability could conceivably accept.

    Derived from the registry's exact frame sizes rather than hardcoded, so
    a capability whose accepted sizes change does not need editing here.

    Note the widening: two different exact sizes can share a name, and a name
    covers a range of long edges. 1280x720 and 1920x1080 name as "1K" and
    "2K", so a 1600x900 clip also names as "2K" and survives screening
    despite being an illegal input. That is the intended direction of error.

    Args:
        capability: The capability whose video constraints to read.

    Returns:
        The stored resolution names that do not rule the capability out.
        Empty if the capability takes no video at all.
    """
    constraints = capability.input_video
    if constraints is None:
        return frozenset()
    return frozenset(
        _name_long_edge(max(size.width, size.height))
        for size in constraints.frame_sizes
    )


def screen_stored_video(
    capability: VpeCapability,
    *,
    duration_seconds: float | None,
    resolution: str | None,
    max_segments: int = 1,
) -> VpeScreeningResult:
    """Screens a stored video row against a capability's input rules.

    Args:
        capability: The capability to screen against.
        duration_seconds: The row's measured duration, or None if unmeasured.
        resolution: The row's stored resolution name, or None if unmeasured.
        max_segments: How many pieces a caller can split this clip into
            before running it, widening the duration ceiling by that many
            multiples of the capability's own maximum. Left at 1 - no
            splitting credit - for any capability that has no split-and-
            rejoin worker behind it, which is every capability but the
            upscaler today. A short clip gets no such credit either way:
            no number of pieces makes a clip longer.

    Returns:
        The screening outcome and the findings behind it.
    """
    capability_id = str(capability.capability_id)
    constraints = capability.input_video
    if constraints is None:
        return VpeScreeningResult(
            capability_id=capability_id,
            screening=VpeScreening.RULED_OUT,
            findings=(
                VpeFinding(
                    code=VpeReasonCode.MEDIA_KIND_UNSUPPORTED,
                    severity=VpeSeverity.BLOCK,
                    message=(
                        f"{capability.label} does not take a video as input."
                    ),
                    measured="video",
                ),
            ),
        )

    if duration_seconds is None and resolution is None:
        return VpeScreeningResult(
            capability_id=capability_id,
            screening=VpeScreening.UNKNOWN,
        )

    findings: list[VpeFinding] = []

    if duration_seconds is not None:
        # Half a frame of slack on each end. The bounds are frame counts and
        # the stored value is a float division, so a clip sitting exactly on
        # 192 frames can land a hair under 8.0 and must not be rejected for
        # a rounding artefact. Anything within half a frame of legal is left
        # for preflight to count exactly.
        slack = 0.5 / constraints.fps
        max_span = constraints.max_seconds * max_segments
        window = (
            f"{constraints.min_seconds:g}-{constraints.max_seconds:g}s"
            if max_segments <= 1
            else f"{constraints.min_seconds:g}-{max_span:g}s"
            f" split across up to {max_segments} segments"
        )
        too_short = duration_seconds < constraints.min_seconds - slack
        too_long = duration_seconds > max_span + slack
        if too_short or too_long:
            findings.append(
                VpeFinding(
                    code=(
                        VpeReasonCode.FRAME_COUNT_TOO_LOW
                        if too_short
                        else VpeReasonCode.FRAME_COUNT_TOO_HIGH
                    ),
                    severity=VpeSeverity.BLOCK,
                    message=(
                        f"{capability.label} accepts {window},"
                        f" and this one is {duration_seconds:g} seconds."
                    ),
                    # A long clip has a way out and a short one does not, so
                    # they get different advice rather than one hedged line -
                    # unless splitting is already priced into the window
                    # above and still is not enough, in which case there is
                    # nothing left to suggest.
                    remedy=(
                        "Split the clip into segments inside the window."
                        if too_long and max_segments <= 1
                        else ""
                    ),
                    measured=f"{duration_seconds:g}s",
                    required=window,
                ),
            )

    if resolution is not None:
        accepted = accepted_resolution_names(capability)
        if resolution not in accepted:
            findings.append(
                VpeFinding(
                    code=VpeReasonCode.FRAME_SIZE_UNSUPPORTED,
                    severity=VpeSeverity.BLOCK,
                    message=(
                        f"{capability.label} accepts"
                        f" {', '.join(sorted(accepted))} sources,"
                        f" and this one is {resolution}."
                    ),
                    measured=resolution,
                    required=", ".join(sorted(accepted)),
                ),
            )

    if findings:
        return VpeScreeningResult(
            capability_id=capability_id,
            screening=VpeScreening.RULED_OUT,
            findings=tuple(findings),
        )

    # Survived every check the row can answer. That is not the same as
    # passing: frame rate was never examined, and the frame size was checked
    # by name rather than by pixels.
    return VpeScreeningResult(
        capability_id=capability_id,
        screening=VpeScreening.NOT_RULED_OUT,
    )
