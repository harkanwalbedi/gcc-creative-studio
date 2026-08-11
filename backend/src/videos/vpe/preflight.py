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
"""Local preflight for Veo Pro Experimental (VPE) media.

VPE's own input validation cannot be trusted to explain itself. The user
guide's known-issues table records that a wrong input fps came back as a
"high load" message, and the high-load failure is itself intermittent and
real, so a user shown that text has no way to tell a broken clip from a busy
service. Every constraint the capability registry records is therefore
checked here first, against the actual file, and reported with the measured
value next to the required one.

Two entry points, deliberately separate so one measurement can be tested
against several capabilities without re-probing:

* ``probe_media`` measures a file with ffprobe.
* ``validate`` compares one measurement against one capability.

Two measurement decisions are load-bearing:

* Frame rate is an exact rational. 23.976 fps is 24000/1001, which is 23.976
  in float and "about 24" to a human, and the API rejects it outright
  ("Video fps mismatch. Expected: 24 got: 60."). Comparing floats here would
  wave through the single most common off-spec clip there is.
* Frame count, not container duration, decides duration questions. The real
  24 fps clips in this repo carry exactly 240 frames in a container that
  reports 10.005 seconds; a seconds-based bound rejects in-spec clips at the
  edges and the registry states every bound in frames for that reason.

Nothing here touches the network, Cloud Storage or config, so it is the one
part of the VPE integration that can be verified end to end today, against
real files, without being on the program allowlist.
"""

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from src.videos.vpe.capabilities import (
    FRAME_1080P,
    FRAME_1080P_PORTRAIT,
    MIME_EXR,
    MIME_JPEG,
    MIME_MP3,
    MIME_MP4,
    MIME_MXF,
    MIME_PNG,
    MIME_QUICKTIME,
    MIME_WAV,
    VPE_FPS,
    FrameSize,
    VpeAudioConstraints,
    VpeCapability,
    VpeCapabilityId,
    VpeImageConstraints,
    VpeInstanceField,
    VpeKnownIssueId,
    VpeOrientation,
    VpeVideoConstraints,
    get_capability,
)

logger = logging.getLogger(__name__)

# Every capability in the program is 24 fps exactly, in and out.
VPE_REQUIRED_FPS = Fraction(VPE_FPS, 1)

# An audio track whose length differs from the documented one by less than a
# single video frame cannot shift the lip-sync by a frame, so it is not worth
# a warning: probed durations of a nominally 8s file land on 7.99 or 8.01
# depending on how the encoder padded the last packet.
_AUDIO_SECONDS_TOLERANCE = 1.0 / VPE_FPS

# Documented as "resized and padded internally if not exact" - an off-spec
# still degrades the result instead of failing the job. Only these two say
# so; every other capability's stills must already be the right size, and
# the difference has to live somewhere because the registry records it as
# prose in ``input_image.notes`` rather than as a flag.
_STILLS_RESIZED_INTERNALLY = frozenset(
    {
        VpeCapabilityId.DIALOGUE_DRIVEN,
        VpeCapabilityId.VIDEO_TEXTURES,
    },
)

# ffprobe describes a still as a one-frame video stream, so the codec is what
# separates a JPEG from a movie. Frame sequences are handled the same way:
# probing one PNG of a sequence reports that PNG, not the sequence.
_STILL_IMAGE_CODECS = frozenset(
    {
        "bmp",
        "exr",
        "jpeg",
        "mjpeg",
        "png",
        "tiff",
        "webp",
    },
)


class VpeProbeError(RuntimeError):
    """Raised when a file cannot be measured at all."""


class VpeMediaKind(str, Enum):
    """What a probed file turned out to be."""

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class VpeFrameOrientation(str, Enum):
    """Orientation of a measured frame.

    Two states, matching ``FrameSize``: a square frame counts as landscape
    because it is not the pillarbox hazard portrait is.
    """

    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"


class VpeSeverity(str, Enum):
    """How badly a finding should stop a submission."""

    OK = "ok"
    WARN = "warn"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        """Returns a sortable severity, highest being the most severe."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    VpeSeverity.OK: 0,
    VpeSeverity.WARN: 1,
    VpeSeverity.BLOCK: 2,
}


class VpeReasonCode(str, Enum):
    """Machine-readable reason for a finding.

    A UI branches on these; the message beside them is for the person.
    """

    MEDIA_KIND_UNSUPPORTED = "media_kind_unsupported"
    MIME_TYPE_UNSUPPORTED = "mime_type_unsupported"
    MIME_TYPE_UNDOCUMENTED = "mime_type_undocumented"
    FPS_MISMATCH = "fps_mismatch"
    VARIABLE_FRAME_RATE = "variable_frame_rate"
    FRAME_COUNT_TOO_LOW = "frame_count_too_low"
    FRAME_COUNT_TOO_HIGH = "frame_count_too_high"
    FRAME_COUNT_ESTIMATED = "frame_count_estimated"
    FRAME_SIZE_UNSUPPORTED = "frame_size_unsupported"
    PORTRAIT_NOT_SUPPORTED = "portrait_not_supported"
    IMAGE_RESIZED_INTERNALLY = "image_resized_internally"
    FILE_TOO_LARGE = "file_too_large"
    AUDIO_LENGTH_MISMATCH = "audio_length_mismatch"
    AUDIO_INPUT_UNSUPPORTED = "audio_input_unsupported"
    SOURCE_AUDIO_NOT_PRESERVED = "source_audio_not_preserved"
    UPSCALE_1080P_NEEDS_ASPECT_RATIO = "upscale_1080p_needs_aspect_ratio"


@dataclass(frozen=True, slots=True)
class VpeMediaProbe:
    """One measured file, in the terms VPE validates.

    Attributes:
        path: Local path that was measured.
        kind: Whether the file is a video, a still or an audio track.
        container: ffprobe's format name, e.g. ``mov,mp4,m4a,3gp,3g2,mj2``.
        size_bytes: File size on disk.
        width: Frame width in pixels, None for audio.
        height: Frame height in pixels, None for audio.
        fps: Average frame rate as an exact rational, None for audio.
        nominal_fps: Declared base frame rate; differs from fps only for
            variable-frame-rate material.
        frame_count: Video frames, or 1 for a still.
        frame_count_exact: False when the count had to be derived from the
            duration because the container did not carry one.
        container_seconds: Duration the container claims, kept for display
            and never used for a bound check.
        has_audio: True if the file carries an audio stream.
        video_codec: ffprobe codec name of the video stream, if any.
        audio_codec: ffprobe codec name of the audio stream, if any.
        mime_type: The VPE wire mime type this file should be declared as,
            or None if the format has no documented mime type.
    """

    path: str
    kind: VpeMediaKind
    container: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    fps: Fraction | None = None
    nominal_fps: Fraction | None = None
    frame_count: int | None = None
    frame_count_exact: bool = True
    container_seconds: float | None = None
    has_audio: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None
    mime_type: str | None = None

    @property
    def frame_size(self) -> FrameSize | None:
        """Returns the measured frame size, or None for audio."""
        if not self.width or not self.height:
            return None
        return FrameSize(self.width, self.height)

    @property
    def orientation(self) -> VpeFrameOrientation | None:
        """Returns the frame orientation, or None for audio."""
        size = self.frame_size
        if size is None:
            return None
        if size.is_portrait:
            return VpeFrameOrientation.PORTRAIT
        return VpeFrameOrientation.LANDSCAPE

    @property
    def is_portrait(self) -> bool:
        """Returns True only for a frame taller than it is wide."""
        return self.orientation is VpeFrameOrientation.PORTRAIT

    @property
    def frame_seconds(self) -> float | None:
        """Returns the duration implied by the frame count and the fps.

        This is the number to quote at a user: it is what the API measures,
        and it disagrees with ``container_seconds`` on real files.
        """
        if not self.frame_count or not self.fps:
            return None
        return self.frame_count / float(self.fps)


@dataclass(frozen=True, slots=True)
class VpeFinding:
    """One reason a file does or does not fit a capability.

    Attributes:
        code: Machine-readable reason.
        severity: OK is never used here; a finding is a WARN or a BLOCK.
        message: Sentence naming the measured value and the required one.
        remedy: What to do about it, empty when there is nothing to suggest.
        measured: The measured value alone, for a UI that wants to lay the
            two out itself rather than parse the message.
        required: The required value alone.
    """

    code: VpeReasonCode
    severity: VpeSeverity
    message: str
    remedy: str = ""
    measured: str = ""
    required: str = ""


@dataclass(frozen=True, slots=True)
class VpeVerdict:
    """The answer to "can this file be sent to this capability"."""

    capability: VpeCapability
    probe: VpeMediaProbe
    findings: tuple[VpeFinding, ...] = ()

    @property
    def capability_id(self) -> VpeCapabilityId:
        """Returns the capability the file was checked against."""
        return self.capability.capability_id

    @property
    def severity(self) -> VpeSeverity:
        """Returns the worst severity across every finding."""
        if not self.findings:
            return VpeSeverity.OK
        return max(
            (finding.severity for finding in self.findings),
            key=lambda severity: severity.rank,
        )

    @property
    def is_blocked(self) -> bool:
        """Returns True if the file must not be submitted as it is."""
        return self.severity is VpeSeverity.BLOCK

    @property
    def blocking(self) -> tuple[VpeFinding, ...]:
        """Returns only the findings that prevent submission."""
        return tuple(
            finding
            for finding in self.findings
            if finding.severity is VpeSeverity.BLOCK
        )

    @property
    def warnings(self) -> tuple[VpeFinding, ...]:
        """Returns only the findings that degrade rather than prevent."""
        return tuple(
            finding
            for finding in self.findings
            if finding.severity is VpeSeverity.WARN
        )

    @property
    def codes(self) -> tuple[VpeReasonCode, ...]:
        """Returns every reason code, in reported order."""
        return tuple(finding.code for finding in self.findings)

    def has(self, code: VpeReasonCode) -> bool:
        """Returns True if the verdict carries the given reason code.

        Args:
            code: The reason code to look for.

        Returns:
            True if any finding reports that code.
        """
        return code in self.codes

    def summary(self) -> str:
        """Returns a one-line summary suitable for a log or a toast."""
        name = os.path.basename(self.probe.path)
        label = self.capability.label
        if not self.findings:
            return f"{name} is within spec for {label}."
        head = self.findings[0].message
        extra = len(self.findings) - 1
        if extra:
            return f"{head} (+{extra} more)"
        return head


def probe_media(path: str) -> VpeMediaProbe:
    """Measures a media file with ffprobe.

    Args:
        path: Local path to a video, still image or audio file.

    Returns:
        The measurement, with the frame rate as an exact rational.

    Raises:
        VpeProbeError: If the file is missing, ffprobe is unavailable, or the
            file carries neither a video nor an audio stream.
    """
    if not os.path.isfile(path):
        raise VpeProbeError(f"No such media file: {path}")

    data = _run_ffprobe(path, ("-show_streams", "-show_format"))
    streams = data.get("streams") or []
    video = _first_stream(streams, "video")
    audio = _first_stream(streams, "audio")
    if video is None and audio is None:
        raise VpeProbeError(
            f"{path} carries neither a video nor an audio stream",
        )

    fmt = data.get("format") or {}
    container = str(fmt.get("format_name") or "")
    container_seconds = _float(fmt.get("duration"))
    size_bytes = os.path.getsize(path)

    if video is None:
        return VpeMediaProbe(
            path=path,
            kind=VpeMediaKind.AUDIO,
            container=container,
            size_bytes=size_bytes,
            container_seconds=container_seconds
            or _float(audio.get("duration")),
            has_audio=True,
            audio_codec=_text(audio.get("codec_name")),
            mime_type=_audio_mime(_text(audio.get("codec_name")), container),
        )

    video_codec = _text(video.get("codec_name"))
    width = _int(video.get("width"))
    height = _int(video.get("height"))
    kind = _classify_video_stream(video, video_codec)

    if kind is VpeMediaKind.IMAGE:
        # A still has no meaningful frame rate: ffprobe invents 25/1 for a
        # PNG, which would fail an fps check that has no business running.
        return VpeMediaProbe(
            path=path,
            kind=kind,
            container=container,
            size_bytes=size_bytes,
            width=width,
            height=height,
            frame_count=1,
            has_audio=audio is not None,
            video_codec=video_codec,
            audio_codec=_text(audio.get("codec_name")) if audio else None,
            mime_type=_image_mime(video_codec),
        )

    nominal_fps = _fraction(video.get("r_frame_rate"))
    fps = _fraction(video.get("avg_frame_rate")) or nominal_fps
    stream_seconds = _float(video.get("duration"))
    frame_count, exact = _frame_count(
        path,
        video,
        fps,
        container_seconds or stream_seconds,
    )

    return VpeMediaProbe(
        path=path,
        kind=VpeMediaKind.VIDEO,
        container=container,
        size_bytes=size_bytes,
        width=width,
        height=height,
        fps=fps,
        nominal_fps=nominal_fps,
        frame_count=frame_count,
        frame_count_exact=exact,
        container_seconds=container_seconds or stream_seconds,
        has_audio=audio is not None,
        video_codec=video_codec,
        audio_codec=_text(audio.get("codec_name")) if audio else None,
        mime_type=_video_mime(video_codec, container),
    )


def validate(
    probe: VpeMediaProbe,
    capability: VpeCapability | VpeCapabilityId | str,
) -> VpeVerdict:
    """Checks one measured file against one capability.

    The slot the file would occupy is inferred from what the file is: a
    video is checked against the capability's video constraints, a still
    against its image constraints, an audio track against its audio
    constraints. That holds for every capability but one, because no
    capability accepts two different videos or two different audio tracks.

    Omni-cine is the exception, and a still sent to it is under-checked. It
    takes stills in two different roles: a reference image, which may be any
    resolution, and a frame of a PNG or EXR input sequence, which the
    documented input table requires to be 1280x720. Both arrive here as
    ``VpeMediaKind.IMAGE`` and both are checked against the reference-image
    constraints, so an off-spec sequence frame passes clean. The byte cap is
    the same 30 MiB either way, so only the resolution goes unchecked.
    Resolving it properly means telling ``validate`` which slot the caller
    means, rather than guessing from the file, and that is worth doing when
    omni-cine input sequences are actually wired up.

    Args:
        probe: A measurement from ``probe_media``.
        capability: A capability entry, its id, or the id's string value.

    Returns:
        A verdict listing every violation, blocking findings first.

    Raises:
        ValueError: If the capability id is unknown.
    """
    entry = (
        capability
        if isinstance(capability, VpeCapability)
        else get_capability(capability)
    )

    if probe.kind is VpeMediaKind.VIDEO:
        findings = _check_video(probe, entry)
    elif probe.kind is VpeMediaKind.IMAGE:
        findings = _check_image(probe, entry)
    else:
        findings = _check_audio(probe, entry)

    # Blocking findings first: a caller that shows only the first line should
    # show the reason the job cannot run, not an aside about the audio track.
    ordered = sorted(findings, key=lambda item: -item.severity.rank)
    return VpeVerdict(
        capability=entry,
        probe=probe,
        findings=tuple(ordered),
    )


def preflight_media(
    path: str,
    capability: VpeCapability | VpeCapabilityId | str,
) -> VpeVerdict:
    """Measures a file and checks it against a capability in one call.

    Args:
        path: Local path to the media file.
        capability: A capability entry, its id, or the id's string value.

    Returns:
        The verdict for that file and capability.

    Raises:
        VpeProbeError: If the file cannot be measured.
        ValueError: If the capability id is unknown.
    """
    return validate(probe_media(path), capability)


def _check_video(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Checks a measured video against a capability's video contract.

    Args:
        probe: A video measurement.
        capability: The capability the clip would be sent to.

    Returns:
        Every violation found, in discovery order.
    """
    constraints = capability.input_video
    if constraints is None:
        return [_kind_unsupported(probe, capability)]

    findings: list[VpeFinding] = []
    subject = _video_subject(capability)
    findings.extend(_check_video_mime(probe, capability))
    findings.extend(_check_fps(probe, capability))
    findings.extend(_check_frame_count(probe, capability, constraints, subject))
    findings.extend(
        _check_frame_size(probe, capability, constraints.frame_sizes, subject),
    )
    findings.extend(_check_aspect_ratio_issue(probe, capability))
    findings.extend(
        _check_file_size(probe, capability, constraints.max_bytes_per_file),
    )
    findings.extend(_check_incoming_audio_track(probe, capability))
    return findings


def _check_image(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Checks a measured still against a capability's image contract.

    Args:
        probe: A still-image measurement.
        capability: The capability the still would be sent to.

    Returns:
        Every violation found, in discovery order.
    """
    constraints = capability.input_image
    if constraints is None:
        return [_kind_unsupported(probe, capability)]

    findings: list[VpeFinding] = []
    findings.extend(_check_image_mime(probe, capability))
    if not constraints.any_resolution:
        findings.extend(
            _check_still_frame_size(probe, capability, constraints),
        )
    findings.extend(_check_file_size(probe, capability, constraints.max_bytes))
    return findings


def _check_audio(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Checks a measured audio track against a capability's audio contract.

    Args:
        probe: An audio measurement.
        capability: The capability the track would be sent to.

    Returns:
        Every violation found, in discovery order.
    """
    constraints = capability.input_audio
    spec = capability.instance_field(VpeInstanceField.REFERENCE_AUDIOS)
    if constraints is None or spec is None:
        return [_kind_unsupported(probe, capability)]

    findings: list[VpeFinding] = []
    if probe.mime_type is None:
        # AAC and M4A are named as accepted in the input table, yet the
        # instances table lists only wav, mp3 and mpeg mime types, so there
        # is no legal value to put in audio.mimeType for them.
        if probe.audio_codec in ("aac", "alac"):
            findings.append(
                VpeFinding(
                    code=VpeReasonCode.MIME_TYPE_UNDOCUMENTED,
                    severity=VpeSeverity.WARN,
                    message=(
                        f"This track is {probe.audio_codec.upper()}. The"
                        " input table names AAC and M4A as accepted, but the"
                        " only documented mimeType values are"
                        f" {_join(spec.mime_types)}, so the request has no"
                        " correct value to declare."
                    ),
                    remedy="Transcode to 16-bit WAV and send audio/wav.",
                    measured=str(probe.audio_codec),
                    required=_join(spec.mime_types),
                ),
            )
        else:
            findings.append(_mime_unsupported(probe, spec.mime_types))
    elif not spec.accepts(probe.mime_type):
        findings.append(_mime_unsupported(probe, spec.mime_types))

    findings.extend(_check_audio_length(probe, capability, constraints))
    return findings


def _check_video_mime(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Checks the clip's format against the accepted video mime types.

    Args:
        probe: A video measurement.
        capability: The capability the clip would be sent to.

    Returns:
        A single mime finding, or nothing.
    """
    spec = capability.instance_field(VpeInstanceField.VIDEO)
    # Performance generation has video constraints but no video instance
    # field: its clip arrives as experiments.perfMeshGcsUri, documented as
    # H264 (video/mp4).
    accepted = spec.mime_types if spec is not None else (MIME_MP4,)
    if probe.mime_type is not None and probe.mime_type in accepted:
        return []
    return [_mime_unsupported(probe, accepted)]


def _check_image_mime(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Checks the still's format against every still slot's mime types.

    Args:
        probe: A still-image measurement.
        capability: The capability the still would be sent to.

    Returns:
        A single mime finding, or nothing.
    """
    accepted: list[str] = []
    for field in (
        VpeInstanceField.IMAGE,
        VpeInstanceField.LAST_FRAME,
        VpeInstanceField.REFERENCE_IMAGES,
    ):
        spec = capability.instance_field(field)
        if spec is not None:
            accepted.extend(
                mime for mime in spec.mime_types if mime not in accepted
            )
    if probe.mime_type is not None and probe.mime_type in accepted:
        return []
    return [_mime_unsupported(probe, tuple(accepted))]


def _check_fps(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Checks the frame rate, exactly.

    Args:
        probe: A video measurement.
        capability: The capability the clip would be sent to.

    Returns:
        The fps finding, or a variable-frame-rate warning, or nothing.
    """
    if probe.fps is None:
        return [
            VpeFinding(
                code=VpeReasonCode.FPS_MISMATCH,
                severity=VpeSeverity.BLOCK,
                message=(
                    "This clip's frame rate could not be measured;"
                    f" {capability.label} requires exactly"
                    f" {VPE_FPS} fps."
                ),
                remedy=f"Re-encode at a constant {VPE_FPS} fps.",
                measured="unknown",
                required=f"{VPE_FPS} fps",
            ),
        ]
    if probe.fps != VPE_REQUIRED_FPS:
        # A clip whose declared base rate disagrees with its measured
        # average is variable-rate, and the tools a user is likely to check
        # with report the flattering number, so name both.
        spacing = ""
        if probe.nominal_fps is not None and probe.nominal_fps != probe.fps:
            spacing = (
                f" Its declared base rate is"
                f" {_fps_label(probe.nominal_fps)}, so the frames are not"
                " evenly spaced."
            )
        return [
            VpeFinding(
                code=VpeReasonCode.FPS_MISMATCH,
                severity=VpeSeverity.BLOCK,
                message=(
                    f"This clip is {_fps_label(probe.fps)};"
                    f" {capability.label} requires exactly {VPE_FPS} fps."
                    " The API compares the rate exactly and reports a"
                    " mismatch as a load error, so it looks like an outage."
                    f"{spacing}"
                ),
                remedy=(
                    f"Transcode to a constant {VPE_FPS} fps"
                    " (ffmpeg -r 24, or retime if the motion matters)."
                ),
                measured=_fps_label(probe.fps),
                required=f"{VPE_FPS} fps",
            ),
        ]
    if probe.nominal_fps is not None and probe.nominal_fps != probe.fps:
        return [
            VpeFinding(
                code=VpeReasonCode.VARIABLE_FRAME_RATE,
                severity=VpeSeverity.WARN,
                message=(
                    f"This clip averages {VPE_FPS} fps but declares a base"
                    f" rate of {_fps_label(probe.nominal_fps)}, which means"
                    " variable frame timing; the average is what was"
                    " checked, and the API may still see individual frames"
                    " off the 24 fps grid."
                ),
                remedy=(
                    "Re-encode to constant frame rate"
                    " (ffmpeg -vsync cfr -r 24)."
                ),
                measured=_fps_label(probe.nominal_fps),
                required=f"constant {VPE_FPS} fps",
            ),
        ]
    return []


def _check_frame_count(
    probe: VpeMediaProbe,
    capability: VpeCapability,
    constraints: VpeVideoConstraints,
    subject: str,
) -> list[VpeFinding]:
    """Checks the clip length in frames.

    Args:
        probe: A video measurement.
        capability: The capability the clip would be sent to.
        constraints: That capability's video constraints.
        subject: What to call the clip in the message.

    Returns:
        Any length findings, plus a warning if the count was derived.
    """
    findings: list[VpeFinding] = []
    frames = probe.frame_count
    if frames is None:
        return findings

    if not probe.frame_count_exact:
        findings.append(
            VpeFinding(
                code=VpeReasonCode.FRAME_COUNT_ESTIMATED,
                severity=VpeSeverity.WARN,
                message=(
                    f"The container does not carry a frame count, so {frames}"
                    " frames was derived from the duration and the frame"
                    " rate; the length check below could be off by a frame."
                ),
                remedy="Re-wrap the clip (ffmpeg -c copy) to write a count.",
                measured=f"~{frames} frames",
                required=_range_label(constraints),
            ),
        )

    if constraints.accepts_frame_count(frames):
        return findings

    too_long = frames > constraints.max_frames
    code = (
        VpeReasonCode.FRAME_COUNT_TOO_HIGH
        if too_long
        else VpeReasonCode.FRAME_COUNT_TOO_LOW
    )
    remedy = (
        "Trim to at most "
        f"{constraints.max_frames} frames"
        f" ({_seconds_label(constraints.max_seconds)})."
        if too_long
        else (
            "Extend to at least "
            f"{constraints.min_frames} frames"
            f" ({_seconds_label(constraints.min_seconds)})."
        )
    )
    # Quote the clip's own running time, not the 24 fps reading of its
    # frame count: an off-spec frame rate is already blocked separately and
    # a user checking the timeline should recognise the number.
    measured_seconds = _seconds_label(
        probe.frame_seconds or frames / float(VPE_REQUIRED_FPS),
    )
    return findings + [
        VpeFinding(
            code=code,
            severity=VpeSeverity.BLOCK,
            message=(
                f"This {subject} is {measured_seconds} ({frames} frames);"
                f" {capability.label} accepts {_range_label(constraints)}."
            ),
            remedy=remedy,
            measured=f"{measured_seconds} ({frames} frames)",
            required=_range_label(constraints),
        ),
    ]


def _check_frame_size(
    probe: VpeMediaProbe,
    capability: VpeCapability,
    sizes: tuple[FrameSize, ...],
    subject: str,
) -> list[VpeFinding]:
    """Checks orientation first, then the exact frame size.

    Orientation is checked first and suppresses the size finding, because
    "720x1280 is not one of 1280x720" is the same fact told worse: it does
    not say that the job will succeed and quietly waste most of the frame.

    Args:
        probe: A measurement with a frame size.
        capability: The capability the media would be sent to.
        sizes: The accepted frame sizes for this slot.
        subject: What to call the media in the message.

    Returns:
        At most one finding.
    """
    size = probe.frame_size
    if size is None or not sizes:
        return []

    portrait_ok = capability.orientation is VpeOrientation.PORTRAIT_OK
    if size.is_portrait and not portrait_ok:
        return [_portrait_block(capability, size, sizes, subject)]

    if size in sizes:
        return []

    # Telling someone with a 640x360 source to crop is useless advice: the
    # problem there is that there are not enough pixels to begin with.
    target = sizes[0]
    if size.width < target.width or size.height < target.height:
        remedy = (
            f"This is smaller than {target}. Re-render or upscale the source"
            " first: VPE rejects input below 720p outright ('Unsupported"
            " video width 854')."
        )
    else:
        remedy = (
            f"Crop or rescale to {target}. Crop rather than pad: padding"
            " spends the frame on black bars."
        )
    return [
        VpeFinding(
            code=VpeReasonCode.FRAME_SIZE_UNSUPPORTED,
            severity=VpeSeverity.BLOCK,
            message=(
                f"This {subject} is {size}; {capability.label} accepts"
                f" {_join(str(item) for item in sizes)}."
            ),
            remedy=remedy,
            measured=str(size),
            required=_join(str(item) for item in sizes),
        ),
    ]


def _check_still_frame_size(
    probe: VpeMediaProbe,
    capability: VpeCapability,
    constraints: VpeImageConstraints,
) -> list[VpeFinding]:
    """Checks a still's size, allowing for models that resize internally.

    Args:
        probe: A still-image measurement.
        capability: The capability the still would be sent to.
        constraints: That capability's image constraints.

    Returns:
        At most one finding.
    """
    findings = _check_frame_size(
        probe,
        capability,
        constraints.frame_sizes,
        "image",
    )
    if not findings:
        return findings
    finding = findings[0]
    if (
        finding.code is not VpeReasonCode.FRAME_SIZE_UNSUPPORTED
        or capability.capability_id not in _STILLS_RESIZED_INTERNALLY
    ):
        return findings

    # Documented as resized and padded internally, so an off-spec landscape
    # still costs detail rather than the job. Portrait already returned a
    # block above and never reaches here.
    target = constraints.frame_sizes[0]
    return [
        VpeFinding(
            code=VpeReasonCode.IMAGE_RESIZED_INTERNALLY,
            severity=VpeSeverity.WARN,
            message=(
                f"This image is {probe.frame_size}; {capability.label} wants"
                f" {target} and resizes and pads anything else internally,"
                " so the result will not be a pixel-accurate use of this"
                " frame."
            ),
            remedy=f"Crop to {target} yourself for the best result.",
            measured=str(probe.frame_size),
            required=str(target),
        ),
    ]


def _check_aspect_ratio_issue(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Flags the upscaler's 1080p aspectRatio defect for a 1080p source.

    Args:
        probe: A video measurement.
        capability: The capability the clip would be sent to.

    Returns:
        A single warning, or nothing.
    """
    issue = VpeKnownIssueId.UPSCALE_1080P_INPUT_NEEDS_ASPECT_RATIO
    size = probe.frame_size
    if not capability.has_known_issue(issue) or size is None:
        return []
    if size not in (FRAME_1080P, FRAME_1080P_PORTRAIT):
        return []
    ratio = "9:16" if size.is_portrait else "16:9"
    return [
        VpeFinding(
            code=VpeReasonCode.UPSCALE_1080P_NEEDS_ASPECT_RATIO,
            severity=VpeSeverity.WARN,
            message=(
                f"This clip is {size}, and a 1080p source makes"
                f" {capability.label} error unless aspectRatio is sent, even"
                " though the parameter is documented as optional."
            ),
            remedy=f'Send aspectRatio "{ratio}" with this request.',
            measured=str(size),
            required=f'aspectRatio "{ratio}"',
        ),
    ]


def _check_file_size(
    probe: VpeMediaProbe,
    capability: VpeCapability,
    limit: int | None,
) -> list[VpeFinding]:
    """Checks the file against a documented byte cap.

    Args:
        probe: Any measurement.
        capability: The capability the file would be sent to.
        limit: The documented cap in bytes, or None if there is none.

    Returns:
        A single finding, or nothing.
    """
    if limit is None or probe.size_bytes <= limit:
        return []
    return [
        VpeFinding(
            code=VpeReasonCode.FILE_TOO_LARGE,
            severity=VpeSeverity.BLOCK,
            message=(
                f"This file is {_mib(probe.size_bytes)};"
                f" {capability.label} accepts at most {_mib(limit)}."
            ),
            remedy=(
                "Re-encode at a lower bitrate, or crop to the region being"
                " changed and composite the result back."
            ),
            measured=_mib(probe.size_bytes),
            required=f"at most {_mib(limit)}",
        ),
    ]


def _check_incoming_audio_track(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> list[VpeFinding]:
    """Warns that a source audio track will not come back.

    Args:
        probe: A video measurement.
        capability: The capability the clip would be sent to.

    Returns:
        A single warning, or nothing.
    """
    if not probe.has_audio:
        return []
    if capability.has_known_issue(VpeKnownIssueId.OMNI_CINE_NO_AUDIO_INPUT):
        return [
            VpeFinding(
                code=VpeReasonCode.AUDIO_INPUT_UNSUPPORTED,
                severity=VpeSeverity.WARN,
                message=(
                    f"This clip carries {probe.audio_codec} audio, and"
                    f" {capability.label} does not accept audio input: the"
                    " track is ignored and the output carries newly"
                    " generated audio instead."
                ),
                remedy=(
                    "Expect a different soundtrack, and re-mux the original"
                    " track afterwards if you need it."
                ),
                measured=f"{probe.audio_codec} track present",
                required="no audio input",
            ),
        ]
    return [
        VpeFinding(
            code=VpeReasonCode.SOURCE_AUDIO_NOT_PRESERVED,
            severity=VpeSeverity.WARN,
            message=(
                f"This clip carries {probe.audio_codec} audio."
                f" {capability.label} does not document preserving a source"
                " audio track, so treat the delivered file as silent until"
                " you have checked it."
            ),
            remedy="Re-mux the original audio onto the result.",
            measured=f"{probe.audio_codec} track present",
            required="not documented",
        ),
    ]


def _check_audio_length(
    probe: VpeMediaProbe,
    capability: VpeCapability,
    constraints: VpeAudioConstraints,
) -> list[VpeFinding]:
    """Checks an audio track against a documented exact length.

    Args:
        probe: An audio measurement.
        capability: The capability the track would be sent to.
        constraints: That capability's audio constraints.

    Returns:
        A single warning, or nothing.
    """
    expected = constraints.exact_seconds
    measured = probe.container_seconds
    if expected is None or measured is None:
        return []
    if abs(measured - expected) <= _AUDIO_SECONDS_TOLERANCE:
        return []

    longer = measured > expected
    tail = (
        "everything past that point is dropped"
        if longer
        else "the remainder is padded with silence"
    )
    return [
        VpeFinding(
            code=VpeReasonCode.AUDIO_LENGTH_MISMATCH,
            severity=VpeSeverity.WARN,
            message=(
                f"This track is {_seconds_label(measured)};"
                f" {capability.label} uses exactly"
                f" {_seconds_label(expected)}, so {tail}."
            ),
            remedy=(
                f"Cut the recording to {_seconds_label(expected)} around the"
                " line you want performed."
            ),
            measured=_seconds_label(measured),
            required=_seconds_label(expected),
        ),
    ]


def _kind_unsupported(
    probe: VpeMediaProbe,
    capability: VpeCapability,
) -> VpeFinding:
    """Builds the finding for media a capability has no slot for.

    Args:
        probe: Any measurement.
        capability: The capability that has no slot for it.

    Returns:
        A blocking finding naming the slots the capability does have.
    """
    accepted = _accepted_kinds(capability)
    message = (
        f"This file is {probe.kind.value};"
        f" {capability.label} takes {_join_phrases(accepted)}."
    )
    remedy = ""

    video_spec = capability.instance_field(VpeInstanceField.VIDEO)
    if (
        probe.kind is VpeMediaKind.IMAGE
        and video_spec is not None
        and probe.mime_type is not None
        and video_spec.accepts(probe.mime_type)
    ):
        # PNG and EXR are legal video input here, but only as a numbered
        # sequence addressed by a glob gcsUri, so a lone frame is not a
        # usable input and measuring one frame proves nothing about the run.
        message += (
            f" {probe.mime_type} frames are accepted only as a numbered"
            " sequence addressed by a glob gcsUri, not as a single still."
        )
        remedy = (
            "Validate the whole sequence against the video constraints"
            " instead of one frame."
        )
    elif probe.kind is VpeMediaKind.AUDIO and capability.has_known_issue(
        VpeKnownIssueId.OMNI_CINE_NO_AUDIO_INPUT,
    ):
        message += " Audio input is explicitly unsupported for this model."

    return VpeFinding(
        code=VpeReasonCode.MEDIA_KIND_UNSUPPORTED,
        severity=VpeSeverity.BLOCK,
        message=message,
        remedy=remedy,
        measured=probe.kind.value,
        required=_join_phrases(accepted),
    )


def _mime_unsupported(
    probe: VpeMediaProbe,
    accepted: tuple[str, ...],
) -> VpeFinding:
    """Builds the finding for a format with no legal mimeType value.

    Args:
        probe: Any measurement.
        accepted: The mime types the slot documents.

    Returns:
        A blocking finding naming the codec that was measured.
    """
    codec = probe.video_codec or probe.audio_codec or "unknown"
    measured = probe.mime_type or f"{codec} in {probe.container}"
    allowed = _join(accepted) or "none"
    return VpeFinding(
        code=VpeReasonCode.MIME_TYPE_UNSUPPORTED,
        severity=VpeSeverity.BLOCK,
        message=(
            f"This file is {measured}, which is not one of the accepted"
            f" formats: {allowed}."
        ),
        remedy=(
            "Transcode to H.264 in MP4 (video), PNG or JPEG (stills), or"
            " WAV (audio)."
        ),
        measured=measured,
        required=_join(accepted),
    )


def _portrait_block(
    capability: VpeCapability,
    size: FrameSize,
    accepted: tuple[FrameSize, ...],
    subject: str,
) -> VpeFinding:
    """Builds the portrait finding, with the pillarbox arithmetic spelled out.

    This is a block rather than a warning on purpose. The API does not reject
    a portrait frame for these models; it fits it inside the landscape output
    frame and bills for the whole job, so the failure is a paid delivery that
    is mostly black.

    Args:
        capability: The capability that is not portrait-capable.
        size: The measured portrait frame size.
        accepted: The frame sizes the slot accepts.
        subject: What to call the media in the message.

    Returns:
        A blocking finding.
    """
    target = _target_frame(capability, accepted)
    scale = min(target.width / size.width, target.height / size.height)
    fitted = FrameSize(
        max(1, round(size.width * scale)),
        max(1, round(size.height * scale)),
    )
    bars = target.width - fitted.width
    coverage = (fitted.width * fitted.height) / (target.width * target.height)
    stated = (
        "is documented landscape 16:9 only"
        if capability.orientation is VpeOrientation.LANDSCAPE_ONLY
        else f"only ever produces a {target} frame"
    )
    return VpeFinding(
        code=VpeReasonCode.PORTRAIT_NOT_SUPPORTED,
        severity=VpeSeverity.BLOCK,
        message=(
            f"This {subject} is {size} (portrait) and {capability.label}"
            f" {stated}. The API does not reject portrait input, it fits it"
            f" into the {target} frame: {size} becomes about {fitted} of"
            f" picture with {bars} px of black bars beside it, so only"
            f" {coverage:.0%} of the frame you pay for is your subject."
        ),
        remedy=(
            f"Crop to {target} rather than padding, or reframe the shot"
            " landscape."
        ),
        measured=f"{size} (portrait)",
        required=_join(str(item) for item in accepted) or str(target),
    )


def _target_frame(
    capability: VpeCapability,
    accepted: tuple[FrameSize, ...],
) -> FrameSize:
    """Returns the landscape frame a portrait input would be fitted into.

    Args:
        capability: The capability being checked.
        accepted: The frame sizes documented for the slot.

    Returns:
        The first landscape size the slot accepts, falling back to the
        capability's output frame.
    """
    for size in (*accepted, *capability.output.frame_sizes):
        if size.is_landscape:
            return size
    return capability.output.frame_sizes[0]


def _accepted_kinds(capability: VpeCapability) -> list[str]:
    """Lists the kinds of media a capability accepts, for a message.

    Args:
        capability: The capability to describe.

    Returns:
        Human-readable slot names, e.g. ``["a video", "still images"]``.
    """
    kinds: list[str] = []
    if capability.input_video is not None:
        kinds.append("a video")
    if capability.input_image is not None:
        kinds.append("still images")
    if capability.input_audio is not None:
        kinds.append("an audio track")
    if capability.supports(VpeInstanceField.PROMPT) and not kinds:
        kinds.append("a text prompt only")
    return kinds


def _video_subject(capability: VpeCapability) -> str:
    """Names the clip in a message.

    Args:
        capability: The capability being checked.

    Returns:
        "blue mesh" for performance generation, whose video constraints
        describe the mesh delivered through experiments.perfMeshGcsUri
        rather than an instances[] video, and "clip" everywhere else.
    """
    if capability.capability_id is VpeCapabilityId.PERF_GENERATION:
        return "blue mesh"
    return "clip"


def _run_ffprobe(path: str, extra_args: tuple[str, ...]) -> dict:
    """Runs ffprobe and parses its JSON output.

    Args:
        path: File to probe.
        extra_args: ffprobe arguments selecting what to report.

    Returns:
        The parsed ffprobe document.

    Raises:
        VpeProbeError: If ffprobe is missing, fails, or emits invalid JSON.
    """
    command = ["ffprobe", "-v", "error", *extra_args, "-of", "json", path]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as error:
        raise VpeProbeError(
            "ffprobe not found. Install ffmpeg to preflight VPE media.",
        ) from error
    except subprocess.CalledProcessError as error:
        raise VpeProbeError(
            f"ffprobe could not read {path}: {error.stderr.strip()}",
        ) from error
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise VpeProbeError(f"ffprobe returned no JSON for {path}") from error


def _frame_count(
    path: str,
    stream: dict,
    fps: Fraction | None,
    seconds: float | None,
) -> tuple[int | None, bool]:
    """Determines the video stream's frame count.

    Args:
        path: File being probed, for the counting fallback.
        stream: The ffprobe video stream object.
        fps: The measured frame rate, if any.
        seconds: The container duration, if any.

    Returns:
        The frame count and whether it is exact rather than derived.
    """
    frames = _int(stream.get("nb_frames"))
    if frames and frames > 0:
        return frames, True

    # Some containers, notably a raw or re-wrapped stream, carry no count.
    # Counting decodes the file, so it only happens when there is no
    # alternative - but a guessed length is exactly the kind of thing the
    # API would reject with a misleading error.
    logger.info("No frame count in %s; counting frames with ffprobe", path)
    try:
        counted = _run_ffprobe(
            path,
            (
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
            ),
        )
    except VpeProbeError:
        counted = {}
    streams = counted.get("streams") or [{}]
    frames = _int(streams[0].get("nb_read_frames"))
    if frames and frames > 0:
        return frames, True

    if fps and seconds:
        return max(1, round(seconds * float(fps))), False
    return None, False


def _classify_video_stream(stream: dict, codec: str | None) -> VpeMediaKind:
    """Decides whether a video stream is really a still image.

    Args:
        stream: The ffprobe video stream object.
        codec: Its codec name.

    Returns:
        IMAGE for a single-frame still-image codec, VIDEO otherwise.
    """
    frames = _int(stream.get("nb_frames"))
    if codec in _STILL_IMAGE_CODECS and (frames is None or frames <= 1):
        return VpeMediaKind.IMAGE
    return VpeMediaKind.VIDEO


def _video_mime(codec: str | None, container: str) -> str | None:
    """Maps a decoded video stream to the mime type VPE expects.

    The container alone cannot decide this: ffprobe reports both .mp4 and
    .mov as ``mov,mp4,m4a,3gp,3g2,mj2``, and the two carry different mime
    types in a VPE request.

    Args:
        codec: ffprobe codec name.
        container: ffprobe format name.

    Returns:
        The wire mime type, or None if the format has no documented one.
    """
    if codec == "h264":
        return MIME_MP4
    if codec == "prores":
        return MIME_QUICKTIME
    if codec == "dnxhd" and "mxf" in container:
        return MIME_MXF
    return None


def _image_mime(codec: str | None) -> str | None:
    """Maps a still-image codec to the mime type VPE expects.

    Args:
        codec: ffprobe codec name.

    Returns:
        The wire mime type, or None if the format has no documented one.
    """
    return {
        "png": MIME_PNG,
        "mjpeg": MIME_JPEG,
        "jpeg": MIME_JPEG,
        "exr": MIME_EXR,
    }.get(codec or "")


def _audio_mime(codec: str | None, container: str) -> str | None:
    """Maps an audio stream to the mime type VPE expects.

    Args:
        codec: ffprobe codec name.
        container: ffprobe format name.

    Returns:
        The wire mime type, or None if the format has no documented one.
        AAC and M4A land here deliberately: the docs name them as accepted
        formats but list no mime type for them.
    """
    if "wav" in container or (codec or "").startswith("pcm_"):
        return MIME_WAV
    if codec == "mp3" or container == "mp3":
        return MIME_MP3
    return None


def _first_stream(streams: list[dict], codec_type: str) -> dict | None:
    """Returns the first stream of a given type.

    Args:
        streams: ffprobe stream objects.
        codec_type: ``video`` or ``audio``.

    Returns:
        The first matching stream, or None.
    """
    for stream in streams:
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _fraction(value: object) -> Fraction | None:
    """Parses an ffprobe rate such as ``24/1`` into an exact Fraction.

    Args:
        value: The raw ffprobe field.

    Returns:
        The rate, or None when it is absent or the undefined ``0/0``.
    """
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _int(value: object) -> int | None:
    """Parses an ffprobe integer field.

    Args:
        value: The raw ffprobe field.

    Returns:
        The integer, or None if it is absent or unparseable.
    """
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    """Parses an ffprobe float field.

    Args:
        value: The raw ffprobe field.

    Returns:
        The float, or None if it is absent or unparseable.
    """
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str | None:
    """Returns a stripped string, or None for an absent field.

    Args:
        value: The raw ffprobe field.

    Returns:
        The trimmed text, or None.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _seconds_label(seconds: float) -> str:
    """Formats a duration the way the docs quote them.

    Args:
        seconds: The duration.

    Returns:
        A short label such as ``8s`` or ``9.5s``.
    """
    return f"{_seconds_number(seconds)}s"


def _seconds_number(seconds: float) -> str:
    """Formats a duration without its unit, for the low end of a range.

    Args:
        seconds: The duration.

    Returns:
        A bare number such as ``4`` or ``9.5``.
    """
    rounded = round(seconds, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


def _fps_label(fps: Fraction) -> str:
    """Formats a frame rate without hiding a non-integer denominator.

    Args:
        fps: The measured rate.

    Returns:
        ``24 fps``, or ``23.976 fps (24000/1001)`` for the rates that round
        to a plausible-looking integer.
    """
    if fps.denominator == 1:
        return f"{fps.numerator} fps"
    return f"{float(fps):.3f} fps ({fps.numerator}/{fps.denominator})"


def _range_label(constraints: VpeVideoConstraints) -> str:
    """Describes a frame-count range in both seconds and frames.

    Args:
        constraints: The video constraints to describe.

    Returns:
        ``exactly 8s (192 frames)`` or ``4-8s (96-192 frames)``.
    """
    if constraints.min_frames == constraints.max_frames:
        return (
            f"exactly {_seconds_label(constraints.max_seconds)}"
            f" ({constraints.max_frames} frames)"
        )
    return (
        f"{_seconds_number(constraints.min_seconds)}-"
        f"{_seconds_label(constraints.max_seconds)}"
        f" ({constraints.min_frames}-{constraints.max_frames} frames)"
    )


def _mib(size_bytes: int) -> str:
    """Formats a byte count in MiB, the unit the docs use.

    Args:
        size_bytes: The size in bytes.

    Returns:
        A label such as ``50 MiB``.
    """
    return f"{size_bytes / (1024 * 1024):.1f} MiB"


def _join(values) -> str:
    """Joins values into a comma-separated list.

    Args:
        values: Any iterable of strings.

    Returns:
        The joined text, empty when there is nothing to join.
    """
    return ", ".join(values)


def _join_phrases(values: list[str]) -> str:
    """Joins phrases into a sentence fragment ending in "and".

    Args:
        values: Phrases such as ``["a video", "still images"]``.

    Returns:
        ``a video and still images``, or an empty string.
    """
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return f"{_join(values[:-1])} and {values[-1]}"
