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
"""Declarative registry of Veo Pro Experimental (VPE) capabilities.

Every VPE capability is reached through one shared publisher endpoint and is
selected by ``parameters.experiments.modelName``, so the differences between
them live entirely in the payload: which instance fields are legal, which
parameters sit at the top level versus inside ``experiments{}``, which mime
types and frame sizes are accepted, and what comes back.

Those differences are data here rather than branches elsewhere. Payload
builders, preflight, the DTO layer and the eventual capability endpoint all
read the same entries, so a doc correction is a one-line change in this file
instead of a hunt through if-statements.

Every value is transcribed from the VPE documentation (August 2026 revision);
where a doc contradicts itself or omits something the discrepancy is recorded
in the relevant ``notes`` rather than silently resolved. None of this has been
executed against the live API - the program is allowlist-gated and this
project is not on the allowlist.
"""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

# All capabilities POST to this single publisher model; the checkpoint is
# chosen by experiments.modelName, which is why model_name below is a payload
# field and never part of the URL.
VPE_ENDPOINT_MODEL_ID = "veo-experimental"

_DOC_BASE = (
    "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models"
    "/experimental"
)

# Wire mime types. Deliberately plain strings and deliberately not added to
# src.common.base_dto.MimeTypeEnum: that enum is validated on every gallery
# read, so a row carrying video/quicktime or image/x-exr would become
# unreadable through the repository. These describe VPE payloads only.
MIME_MP4 = "video/mp4"
MIME_QUICKTIME = "video/quicktime"  # ProRes .mov
MIME_MXF = "application/mxf"  # DNxHR .mxf
MIME_PNG = "image/png"  # also a 16-bit PNG frame sequence, via a glob gcsUri
MIME_JPEG = "image/jpeg"
MIME_EXR = "image/x-exr"
MIME_WAV = "audio/wav"
MIME_MP3 = "audio/mp3"  # non-standard, but what the doc lists
MIME_MPEG = "audio/mpeg"

# The API takes durations in seconds nowhere and frames everywhere that
# matters, and the frame count is what it actually validates: a nominal
# 10s 24fps clip probes as 10.005s, so a seconds-based bound rejects or
# accepts the wrong clips at the edges. Frames are authoritative throughout
# this module; seconds are derived for display only.
VPE_FPS = 24


class VpeCapabilityId(str, Enum):
    """Stable identifiers for the capabilities in the VPE program.

    Eight capabilities, seven checkpoints: seamless upscaling is the same
    ``veo3p1_upscale`` model as the plain upscaler driven with the
    ``experiments.seamless`` flags, but its inputs, its legal output
    resolutions and its failure modes differ enough that collapsing the two
    would mean re-deriving the difference at every call site.
    """

    UPSCALE = "upscale"
    UPSCALE_SEAMLESS = "upscale_seamless"
    OMNI_CINE = "omni_cine"
    VIDEO_TRANSFORM = "video_transform"
    PERF_ESTIMATION = "perf_estimation"
    PERF_GENERATION = "perf_generation"
    DIALOGUE_DRIVEN = "dialogue_driven"
    VIDEO_TEXTURES = "video_textures"


class VpeOrientation(str, Enum):
    """What the docs say about orientation, kept in three states.

    The third state is the honest one and the reason this is not a boolean.
    Only the upscaler documents portrait support, and only the upscaler
    exposes an ``aspectRatio`` parameter. Video transform and both
    performance models say "Landscape 16:9 orientation only" outright. The
    remaining three say nothing about orientation at all - they simply state
    a fixed 1280x720 frame and offer no way to ask for anything else.

    Recording that as LANDSCAPE_ONLY would invent a documented restriction
    that does not exist, and recording it as portrait-capable would invite a
    720x1280 input that gets pillarboxed into 1280x720 (leaving roughly
    405x720 of real subject pixels) without ever raising an error. Callers
    should treat FIXED_FRAME_UNSTATED as "landscape in practice, unconfirmed
    by the docs" and say so in the UI rather than pretending either way.
    """

    PORTRAIT_OK = "portrait_ok"
    LANDSCAPE_ONLY = "landscape_only"
    FIXED_FRAME_UNSTATED = "fixed_frame_unstated"


class VpeInstanceField(str, Enum):
    """Fields that may appear inside ``instances[0]``.

    Values are the wire names.
    """

    PROMPT = "prompt"
    VIDEO = "video"
    IMAGE = "image"
    LAST_FRAME = "lastFrame"
    REFERENCE_IMAGES = "referenceImages"
    REFERENCE_AUDIOS = "referenceAudios"


# Fields carrying a list of objects rather than a single one; only these may
# declare a max_items.
_ARRAY_INSTANCE_FIELDS = frozenset(
    {
        VpeInstanceField.REFERENCE_IMAGES,
        VpeInstanceField.REFERENCE_AUDIOS,
    },
)


class VpeParameterLocation(str, Enum):
    """Where a parameter sits in the request body.

    Values are dotted JSON paths so a payload builder can place a parameter
    from the registry alone. The nesting is the single easiest thing to get
    wrong: ``storageUri`` is top level, ``modelName`` is one level down in
    ``experiments``, and the seamless flags are two levels down.
    """

    PARAMETERS = "parameters"
    EXPERIMENTS = "parameters.experiments"
    EXPERIMENTS_SEAMLESS = "parameters.experiments.seamless"


class VpeFieldRequirement(str, Enum):
    """Whether a field must be sent.

    CONDITIONAL exists because several fields are documented "Required" in a
    table and then omitted from a working sample in the same page (video
    transform's ``video``) or are required only in one of the capability's
    modes (video textures' ``prompt``). Flattening those to required would
    make legal requests unbuildable; flattening them to optional would lose
    the constraint. The ``notes`` on the spec carry the actual rule.
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    CONDITIONAL = "conditional"


class VpeValueType(str, Enum):
    """JSON type of a parameter value."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"


class VpeCodec(str, Enum):
    """Accepted values of ``experiments.codec`` (output container)."""

    H264 = "h264"
    PRORES = "prores"
    DNXHR = "dnxhr"


class VpeCompressionQuality(str, Enum):
    """Accepted values of ``compressionQuality``.

    LOSSLESS_16BIT_PNG is upscaler-only and 4K-only.
    """

    OPTIMIZED = "optimized"
    LOSSLESS = "lossless"
    LOSSLESS_16BIT_PNG = "lossless_16bit_png"


class VpeUpscaleResolution(str, Enum):
    """Accepted values of the upscaler's ``resolution`` parameter."""

    HD_1080P = "1080p"
    UHD_4K = "4k"


class VpeOutputFormat(str, Enum):
    """Delivery formats a capability can write into the output folder."""

    H264_MP4 = "h264_mp4"
    PRORES_MOV = "prores_mov"
    DNXHR_MXF = "dnxhr_mxf"
    PNG16_SEQUENCE = "png16_sequence"
    EXR16_SEQUENCE = "exr16_sequence"


# Formats that arrive as a directory of frames rather than a single file.
_SEQUENCE_OUTPUT_FORMATS = frozenset(
    {
        VpeOutputFormat.PNG16_SEQUENCE,
        VpeOutputFormat.EXR16_SEQUENCE,
    },
)


class VpeKnownIssueId(str, Enum):
    """Documented defects the calling code has to design around."""

    UPSCALE_4K_PNG16_NO_OPERATION_ID = "upscale_4k_png16_no_operation_id"
    UPSCALE_1080P_INPUT_NEEDS_ASPECT_RATIO = (
        "upscale_1080p_input_needs_aspect_ratio"
    )
    UPSCALE_SEAMLESS_FLAGS_MUST_MATCH_SOURCE = (
        "upscale_seamless_flags_must_match_source"
    )
    UPSCALE_SEAMLESS_NO_1080P = "upscale_seamless_no_1080p"
    OMNI_CINE_NO_AUDIO_INPUT = "omni_cine_no_audio_input"
    OMNI_CINE_NO_VIDEO_REFERENCES = "omni_cine_no_video_references"
    OMNI_CINE_NO_SEED = "omni_cine_no_seed"
    OMNI_CINE_EXR_HEADERS_DROPPED = "omni_cine_exr_headers_dropped"
    OMNI_CINE_EXR_OUTPUT_NEEDS_EXR_INPUT = (
        "omni_cine_exr_output_needs_exr_input"
    )
    VIDEO_TRANSFORM_MASK_EDGE_PRECISION = "video_transform_mask_edge_precision"
    TEXTURES_LOOP_WITH_IMAGE_UNSUPPORTED = (
        "textures_loop_with_image_unsupported"
    )
    TEXTURES_LAST_FRAME_ANCHORED_EARLY = "textures_last_frame_anchored_early"
    TEXTURES_NEEDS_TESSELLATED_CONDITIONING = (
        "textures_needs_tessellated_conditioning"
    )
    PROGRAM_HIGH_LOAD_FAILURES = "program_high_load_failures"
    PROGRAM_INACCURATE_ERRORS = "program_inaccurate_errors"


@dataclass(frozen=True, slots=True)
class FrameSize:
    """A pixel frame size, e.g. 1280x720."""

    width: int
    height: int

    def __post_init__(self) -> None:
        """Rejects non-positive dimensions."""
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"Invalid frame size: {self.width}x{self.height}")

    @property
    def is_portrait(self) -> bool:
        """Returns True when the frame is taller than it is wide."""
        return self.height > self.width

    @property
    def is_landscape(self) -> bool:
        """Returns True when the frame is at least as wide as it is tall."""
        return not self.is_portrait

    def __str__(self) -> str:
        """Returns the conventional WIDTHxHEIGHT label."""
        return f"{self.width}x{self.height}"


FRAME_720P = FrameSize(1280, 720)
FRAME_720P_PORTRAIT = FrameSize(720, 1280)
FRAME_1080P = FrameSize(1920, 1080)
FRAME_1080P_PORTRAIT = FrameSize(1080, 1920)
FRAME_4K = FrameSize(3840, 2160)
FRAME_4K_PORTRAIT = FrameSize(2160, 3840)

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class VpeInstanceFieldSpec:
    """One field of ``instances[0]`` and everything it accepts."""

    field: VpeInstanceField
    requirement: VpeFieldRequirement
    mime_types: tuple[str, ...] = ()
    max_items: int | None = None
    accepts_inline_bytes: bool = False
    reference_type: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        """Rejects field shapes the API has no representation for."""
        is_array = self.field in _ARRAY_INSTANCE_FIELDS
        if self.field is VpeInstanceField.PROMPT:
            if self.mime_types or self.max_items is not None:
                raise ValueError(
                    "prompt is text: it has no mime types and no max_items",
                )
        elif not self.mime_types:
            raise ValueError(
                f"{self.field.value} must declare at least one mime type",
            )
        if is_array and (self.max_items is None or self.max_items < 1):
            raise ValueError(
                f"{self.field.value} is a list and needs a positive"
                " max_items",
            )
        if not is_array and self.max_items is not None:
            raise ValueError(
                f"{self.field.value} holds a single object, not a list",
            )
        if (
            self.reference_type is not None
            and self.field is not VpeInstanceField.REFERENCE_IMAGES
        ):
            raise ValueError(
                "referenceType only applies to referenceImages entries",
            )

    @property
    def is_required(self) -> bool:
        """Returns True only for unconditionally required fields."""
        return self.requirement is VpeFieldRequirement.REQUIRED

    def accepts(self, mime_type: str) -> bool:
        """Returns True if the field accepts the given mime type.

        Args:
            mime_type: The mime type to check, e.g. ``video/mp4``.

        Returns:
            True if the mime type is documented as accepted for this field.
        """
        return mime_type.lower() in self.mime_types


@dataclass(frozen=True, slots=True)
class VpeParameterSpec:
    """One entry of ``parameters{}``, wherever it nests."""

    name: str
    location: VpeParameterLocation
    requirement: VpeFieldRequirement
    value_type: VpeValueType
    allowed_values: tuple[str, ...] = ()
    default: object | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        """Rejects specs that could not describe a real parameter."""
        if not self.name or "." in self.name:
            raise ValueError(
                "Parameter name must be a bare key; nesting comes from"
                " location",
            )
        if (
            self.allowed_values
            and isinstance(self.default, str)
            and self.default not in self.allowed_values
        ):
            raise ValueError(
                f"Default {self.default!r} is not in allowed_values for"
                f" {self.name}",
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(f"Inverted range on {self.name}")
        if self.exclusive_minimum and self.minimum is None:
            raise ValueError(
                f"exclusive_minimum on {self.name} needs a minimum",
            )

    @property
    def is_required(self) -> bool:
        """Returns True only for unconditionally required parameters."""
        return self.requirement is VpeFieldRequirement.REQUIRED

    @property
    def json_path(self) -> str:
        """Returns the dotted path of the parameter in the request body."""
        return f"{self.location.value}.{self.name}"


@dataclass(frozen=True, slots=True)
class VpeVideoConstraints:
    """Limits on an input video (or frame sequence).

    Bounds are in frames because that is what the API validates; see the
    module-level note on VPE_FPS.
    """

    min_frames: int
    max_frames: int
    frame_sizes: tuple[FrameSize, ...]
    fps: int = VPE_FPS
    max_bytes_per_file: int | None = None
    max_bytes_per_frame: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        """Rejects impossible frame bounds or an empty size list."""
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        if self.min_frames < 1 or self.max_frames < self.min_frames:
            raise ValueError(
                f"Invalid frame bounds: {self.min_frames}..{self.max_frames}",
            )
        if not self.frame_sizes:
            raise ValueError("At least one accepted frame size is required")

    @property
    def min_seconds(self) -> float:
        """Returns the shortest accepted duration, for display only."""
        return self.min_frames / self.fps

    @property
    def max_seconds(self) -> float:
        """Returns the longest accepted duration, for display only."""
        return self.max_frames / self.fps

    def accepts_frame_count(self, frames: int) -> bool:
        """Returns True if a clip of this many frames is in range.

        Args:
            frames: Measured frame count of the candidate clip.

        Returns:
            True if the count is within the documented bounds.
        """
        return self.min_frames <= frames <= self.max_frames

    def accepts_frame_size(self, size: FrameSize) -> bool:
        """Returns True if the exact frame size is accepted.

        Args:
            size: Measured frame size of the candidate clip.

        Returns:
            True if the size is one of the documented input resolutions.
        """
        return size in self.frame_sizes


@dataclass(frozen=True, slots=True)
class VpeImageConstraints:
    """Limits on input stills (first frame, last frame, references)."""

    frame_sizes: tuple[FrameSize, ...] = ()
    any_resolution: bool = False
    max_bytes: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        """Forces the size rule to be stated exactly once."""
        if bool(self.frame_sizes) == self.any_resolution:
            raise ValueError(
                "Declare either explicit frame_sizes or any_resolution,"
                " not both and not neither",
            )


@dataclass(frozen=True, slots=True)
class VpeAudioConstraints:
    """Limits on an input audio track."""

    exact_seconds: float | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        """Rejects a non-positive documented length."""
        if self.exact_seconds is not None and self.exact_seconds <= 0:
            raise ValueError("exact_seconds must be positive")


@dataclass(frozen=True, slots=True)
class VpeOutputSpec:
    """What lands in the output folder.

    Every VPE job writes a directory, never a single file: a preview video,
    optionally a frame sequence under ``sample_0/``, optionally
    ``sample_0_audio.wav``, plus ``request.json`` and ``prompt.txt``.
    """

    frame_sizes: tuple[FrameSize, ...]
    formats: tuple[VpeOutputFormat, ...]
    max_frames: int
    fps: int = VPE_FPS
    produces_audio: bool = False
    has_audio_stem: bool = False
    emits_frame_sequence: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        """Rejects output descriptions that contradict themselves."""
        if not self.frame_sizes:
            raise ValueError("At least one output frame size is required")
        if not self.formats:
            raise ValueError("At least one output format is required")
        if self.max_frames < 1:
            raise ValueError("max_frames must be positive")
        if self.fps <= 0:
            raise ValueError("fps must be positive")
        if self.has_audio_stem and not self.produces_audio:
            raise ValueError(
                "A separate audio stem implies the capability makes audio",
            )
        sequences = set(self.formats) & _SEQUENCE_OUTPUT_FORMATS
        if self.emits_frame_sequence != bool(sequences):
            raise ValueError(
                "emits_frame_sequence must agree with the declared formats",
            )

    @property
    def max_seconds(self) -> float:
        """Returns the longest possible output, for display only."""
        return self.max_frames / self.fps


@dataclass(frozen=True, slots=True)
class VpeKnownIssue:
    """A documented defect, with the workaround the code should apply."""

    issue_id: VpeKnownIssueId
    summary: str
    workaround: str = ""
    resolved: bool = False


# Program-wide, from the user guide's known-issues table. Not attached to any
# one capability: the high-load failure is the reason a VPE job needs retry
# and a truthful "the service is busy" message rather than a hard failure,
# and the (now resolved) error-accuracy issue is the reason client-side
# preflight exists at all - a wrong fps used to surface as a capacity error.
VPE_PROGRAM_KNOWN_ISSUES: tuple[VpeKnownIssue, ...] = (
    VpeKnownIssue(
        issue_id=VpeKnownIssueId.PROGRAM_HIGH_LOAD_FAILURES,
        summary=(
            "A high percentage of jobs fail with a code 8 high-load error;"
            " intermittent but entirely blocking while it occurs."
        ),
        workaround="Retry after a few minutes; surface the operation id.",
    ),
    VpeKnownIssue(
        issue_id=VpeKnownIssueId.PROGRAM_INACCURATE_ERRORS,
        summary=(
            "Input violations were reported inaccurately - a wrong input fps"
            " came back as a high-load message."
        ),
        workaround=(
            "Validate fps, frame count and frame size locally before"
            " submitting, so the user sees the real reason."
        ),
        resolved=True,
    ),
)


@dataclass(frozen=True, slots=True)
class VpeCapability:
    """One VPE capability: its payload contract and its media limits."""

    capability_id: VpeCapabilityId
    model_name: str
    label: str
    description: str
    orientation: VpeOrientation
    instance_fields: tuple[VpeInstanceFieldSpec, ...]
    parameters: tuple[VpeParameterSpec, ...]
    output: VpeOutputSpec
    doc_url: str = ""
    input_video: VpeVideoConstraints | None = None
    input_image: VpeImageConstraints | None = None
    input_audio: VpeAudioConstraints | None = None
    max_prompt_chars: int | None = None
    known_issues: tuple[VpeKnownIssue, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        """Rejects entries that could not describe a real capability."""
        if not self.model_name.strip():
            raise ValueError("model_name is required")
        if not self.label.strip() or not self.description.strip():
            raise ValueError(f"{self.model_name} needs a label and summary")
        if not self.instance_fields:
            raise ValueError(f"{self.model_name} declares no instance fields")

        fields = [spec.field for spec in self.instance_fields]
        if len(set(fields)) != len(fields):
            raise ValueError(f"Duplicate instance field on {self.model_name}")

        paths = [spec.json_path for spec in self.parameters]
        if len(set(paths)) != len(paths):
            raise ValueError(f"Duplicate parameter on {self.model_name}")

        model_param = self.parameter(
            "modelName",
            VpeParameterLocation.EXPERIMENTS,
        )
        if model_param is None or model_param.allowed_values != (
            self.model_name,
        ):
            raise ValueError(
                f"{self.capability_id.value} must declare"
                " experiments.modelName pinned to its model name",
            )
        storage = self.parameter("storageUri", VpeParameterLocation.PARAMETERS)
        if storage is None or not storage.is_required:
            raise ValueError(
                f"{self.capability_id.value} must require storageUri:"
                " VPE only ever writes to Cloud Storage",
            )

        if self.supports(VpeInstanceField.VIDEO) and self.input_video is None:
            raise ValueError(
                f"{self.capability_id.value} takes a video but declares no"
                " video constraints",
            )
        if self.input_image is None and (
            self.supports(VpeInstanceField.IMAGE)
            or self.supports(VpeInstanceField.LAST_FRAME)
            or self.supports(VpeInstanceField.REFERENCE_IMAGES)
        ):
            raise ValueError(
                f"{self.capability_id.value} takes stills but declares no"
                " image constraints",
            )
        if (
            self.supports(VpeInstanceField.REFERENCE_AUDIOS)
            and self.input_audio is None
        ):
            raise ValueError(
                f"{self.capability_id.value} takes audio but declares no"
                " audio constraints",
            )

        aspect_ratio = self.parameter(
            "aspectRatio",
            VpeParameterLocation.PARAMETERS,
        )
        if (
            self.orientation is VpeOrientation.PORTRAIT_OK
            and aspect_ratio is None
        ):
            raise ValueError(
                f"{self.capability_id.value} claims portrait support with no"
                " aspectRatio parameter to request it",
            )
        if self.orientation is not VpeOrientation.PORTRAIT_OK:
            portrait = [size for size in self.frame_sizes if size.is_portrait]
            if portrait:
                raise ValueError(
                    f"{self.capability_id.value} is not documented as"
                    f" portrait-capable but lists {portrait[0]}",
                )

        if self.max_prompt_chars is not None and (
            self.max_prompt_chars < 1
            or not self.supports(VpeInstanceField.PROMPT)
        ):
            raise ValueError(
                f"{self.capability_id.value} caps a prompt it cannot take",
            )

        issue_ids = [issue.issue_id for issue in self.known_issues]
        if len(set(issue_ids)) != len(issue_ids):
            raise ValueError(f"Duplicate known issue on {self.model_name}")

    @property
    def frame_sizes(self) -> tuple[FrameSize, ...]:
        """Returns every input and output frame size the entry declares."""
        sizes: list[FrameSize] = list(self.output.frame_sizes)
        if self.input_video is not None:
            sizes.extend(self.input_video.frame_sizes)
        if self.input_image is not None:
            sizes.extend(self.input_image.frame_sizes)
        return tuple(sizes)

    @property
    def max_reference_images(self) -> int:
        """Returns the reference-image cap, or 0 if none are accepted."""
        spec = self.instance_field(VpeInstanceField.REFERENCE_IMAGES)
        return spec.max_items or 0 if spec is not None else 0

    def instance_field(
        self,
        field: VpeInstanceField,
    ) -> VpeInstanceFieldSpec | None:
        """Returns the spec for an instance field, if it is accepted.

        Args:
            field: The instance field to look up.

        Returns:
            The matching spec, or None if the capability ignores the field.
        """
        for spec in self.instance_fields:
            if spec.field is field:
                return spec
        return None

    def supports(self, field: VpeInstanceField) -> bool:
        """Returns True if the capability accepts the given instance field.

        Args:
            field: The instance field to check.

        Returns:
            True if the field appears in this capability's contract.
        """
        return self.instance_field(field) is not None

    def parameter(
        self,
        name: str,
        location: VpeParameterLocation | None = None,
    ) -> VpeParameterSpec | None:
        """Returns a parameter spec by name.

        Args:
            name: Bare parameter key, e.g. ``storageUri``.
            location: Optional nesting level to disambiguate.

        Returns:
            The matching spec, or None if the parameter is not accepted.
        """
        for spec in self.parameters:
            if spec.name == name and (
                location is None or spec.location is location
            ):
                return spec
        return None

    def has_known_issue(self, issue_id: VpeKnownIssueId) -> bool:
        """Returns True if the capability carries the given known issue.

        Args:
            issue_id: The documented issue to check for.

        Returns:
            True if the issue is recorded against this capability.
        """
        return any(issue.issue_id is issue_id for issue in self.known_issues)


def _model_name_parameter(model_name: str) -> VpeParameterSpec:
    """Builds the experiments.modelName spec pinned to one checkpoint.

    Args:
        model_name: The checkpoint the endpoint should route to.

    Returns:
        A required parameter spec whose only legal value is that name.
    """
    return VpeParameterSpec(
        name="modelName",
        location=VpeParameterLocation.EXPERIMENTS,
        requirement=VpeFieldRequirement.REQUIRED,
        value_type=VpeValueType.STRING,
        allowed_values=(model_name,),
        notes="Routes the shared veo-experimental endpoint to a checkpoint.",
    )


_STORAGE_URI = VpeParameterSpec(
    name="storageUri",
    location=VpeParameterLocation.PARAMETERS,
    requirement=VpeFieldRequirement.REQUIRED,
    value_type=VpeValueType.STRING,
    notes=(
        "gs://BUCKET/PATH/ output folder. Must live in the allowlisted"
        " project; results land in a random subdirectory of it."
    ),
)

_SEED = VpeParameterSpec(
    name="seed",
    location=VpeParameterLocation.PARAMETERS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.INTEGER,
    minimum=0,
    maximum=4294967295,
    notes="Identical inputs plus an identical seed return the same output.",
)

_CODEC = VpeParameterSpec(
    name="codec",
    location=VpeParameterLocation.EXPERIMENTS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.STRING,
    allowed_values=tuple(codec.value for codec in VpeCodec),
    default=VpeCodec.H264.value,
    notes=(
        "Output preview container only; input formats are detected from the"
        " container or the declared mimeType."
    ),
)

_COMPRESSION_QUALITY = VpeParameterSpec(
    name="compressionQuality",
    location=VpeParameterLocation.PARAMETERS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.STRING,
    allowed_values=(
        VpeCompressionQuality.OPTIMIZED.value,
        VpeCompressionQuality.LOSSLESS.value,
    ),
    default=VpeCompressionQuality.OPTIMIZED.value,
    notes=(
        "lossless selects ProRes 4444 / DNxHR 444 (4:4:4); optimized selects"
        " ProRes Standard / DNxHR HQ (4:2:2)."
    ),
)

_SEAMLESS_LOOP = VpeParameterSpec(
    name="loop",
    location=VpeParameterLocation.EXPERIMENTS_SEAMLESS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.BOOLEAN,
    default=False,
    notes="Seamless temporal loop: the last frame flows into the first.",
)

_SEAMLESS_TESSELLATE_HORIZONTAL = VpeParameterSpec(
    name="tessellateHorizontal",
    location=VpeParameterLocation.EXPERIMENTS_SEAMLESS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.BOOLEAN,
    default=False,
    notes="Seamless spatial tiling along the horizontal axis.",
)

_SEAMLESS_TESSELLATE_VERTICAL = VpeParameterSpec(
    name="tessellateVertical",
    location=VpeParameterLocation.EXPERIMENTS_SEAMLESS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.BOOLEAN,
    default=False,
    notes="Seamless spatial tiling along the vertical axis.",
)

_SEAMLESS_PARAMETERS = (
    _SEAMLESS_LOOP,
    _SEAMLESS_TESSELLATE_HORIZONTAL,
    _SEAMLESS_TESSELLATE_VERTICAL,
)

# task is absent from the upscaler's parameter table yet present in every
# documented upscaler sample, so it is sent rather than trusted to default.
_UPSCALE_TASK = VpeParameterSpec(
    name="task",
    location=VpeParameterLocation.PARAMETERS,
    requirement=VpeFieldRequirement.REQUIRED,
    value_type=VpeValueType.STRING,
    allowed_values=("upscale",),
    default="upscale",
    notes="Undocumented in the parameter table; in every doc sample.",
)

_UPSCALE_SHARPNESS = VpeParameterSpec(
    name="sharpness",
    location=VpeParameterLocation.PARAMETERS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.INTEGER,
    minimum=0,
    maximum=4,
    default=1,
    notes="Higher is sharper.",
)

_UPSCALE_COMPRESSION_QUALITY = VpeParameterSpec(
    name="compressionQuality",
    location=VpeParameterLocation.PARAMETERS,
    requirement=VpeFieldRequirement.OPTIONAL,
    value_type=VpeValueType.STRING,
    allowed_values=tuple(q.value for q in VpeCompressionQuality),
    default=VpeCompressionQuality.OPTIMIZED.value,
    notes=(
        "optimized sets crf=18, lossless sets crf=0. lossless_16bit_png is"
        " 4K-only, and see the operation-id known issue before using it."
    ),
)


VPE_CAPABILITY_LIST: tuple[VpeCapability, ...] = (
    VpeCapability(
        capability_id=VpeCapabilityId.UPSCALE,
        model_name="veo3p1_upscale",
        label="Upscaler",
        description=(
            "Standalone upsampler taking 720p or 1080p source to 1080p or"
            " 4K for editing, colour grading and VFX pipelines."
        ),
        # The only capability the docs state portrait support for, and the
        # only one exposing aspectRatio to ask for it.
        orientation=VpeOrientation.PORTRAIT_OK,
        doc_url=f"{_DOC_BASE}/upscaler",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.VIDEO,
                requirement=VpeFieldRequirement.REQUIRED,
                mime_types=(MIME_MP4, MIME_QUICKTIME, MIME_MXF, MIME_PNG),
                notes=(
                    "The instances table lists video/mp4 alone, but the input"
                    " format table and the professional-formats page both"
                    " accept ProRes, DNxHR and a 16-bit PNG sequence"
                    " (gcsUri as a glob such as gs://bucket/in/*.png with"
                    " mimeType image/png)."
                ),
            ),
        ),
        parameters=(
            _model_name_parameter("veo3p1_upscale"),
            _STORAGE_URI,
            _UPSCALE_TASK,
            _CODEC,
            VpeParameterSpec(
                name="resolution",
                location=VpeParameterLocation.PARAMETERS,
                requirement=VpeFieldRequirement.OPTIONAL,
                value_type=VpeValueType.STRING,
                allowed_values=tuple(res.value for res in VpeUpscaleResolution),
                notes=(
                    "Documented default is 720p, which upscales nothing;"
                    " always send 1080p or 4k explicitly."
                ),
            ),
            VpeParameterSpec(
                name="aspectRatio",
                location=VpeParameterLocation.PARAMETERS,
                requirement=VpeFieldRequirement.CONDITIONAL,
                value_type=VpeValueType.STRING,
                allowed_values=("16:9", "9:16"),
                default="16:9",
                notes=(
                    "Documented optional, but a 1080p input errors without"
                    " it, and a declared ratio that disagrees with the pixels"
                    " is rejected. Always send the measured one."
                ),
            ),
            _UPSCALE_COMPRESSION_QUALITY,
            _UPSCALE_SHARPNESS,
        ),
        input_video=VpeVideoConstraints(
            min_frames=96,
            max_frames=192,
            frame_sizes=(
                FRAME_720P,
                FRAME_720P_PORTRAIT,
                FRAME_1080P,
                FRAME_1080P_PORTRAIT,
            ),
            notes=(
                "4 to 8 seconds at exactly 24 fps. Anything below 720p is"
                " rejected outright ('Unsupported video width 854')."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(
                FRAME_1080P,
                FRAME_1080P_PORTRAIT,
                FRAME_4K,
                FRAME_4K_PORTRAIT,
            ),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
            ),
            max_frames=192,
            emits_frame_sequence=True,
            notes=(
                "1080p output is H.264 only; ProRes, DNxHR and 16-bit PNG"
                " frames are 4K-only. The docs never say whether a source"
                " audio track survives, so do not assume it does."
            ),
        ),
        known_issues=(
            VpeKnownIssue(
                issue_id=(
                    VpeKnownIssueId.UPSCALE_1080P_INPUT_NEEDS_ASPECT_RATIO
                ),
                summary=(
                    "Upscaling a 1080p asset without aspectRatio returns an"
                    " error even though the field is documented optional."
                ),
                workaround="Always send the measured aspectRatio.",
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.UPSCALE_4K_PNG16_NO_OPERATION_ID,
                summary=(
                    "4K with lossless_16bit_png may return neither an"
                    " operation id nor the output path, so the caller cannot"
                    " find the result."
                ),
                workaround=(
                    "Treat 4K + lossless_16bit_png as unsupported, or poll"
                    " the output prefix in Cloud Storage instead."
                ),
            ),
        ),
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
        model_name="veo3p1_upscale",
        label="Upscaler (seamless tessellation and looping)",
        description=(
            "The same upscaler driven with the seamless flags, taking a"
            " tessellated or looping 720p clip to 4K without seams."
        ),
        # Same checkpoint as the plain upscaler, so aspectRatio exists, but
        # this doc pins the input to the 1280x720 that video textures emits
        # and never mentions orientation: there is no documented route to a
        # portrait tessellated source.
        orientation=VpeOrientation.FIXED_FRAME_UNSTATED,
        doc_url=f"{_DOC_BASE}/upscaling-tessellation",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.VIDEO,
                requirement=VpeFieldRequirement.REQUIRED,
                mime_types=(MIME_MP4, MIME_QUICKTIME, MIME_MXF, MIME_PNG),
                notes=(
                    "The 720p video or 16-bit PNG sequence produced by"
                    " veo-exp-video-textures."
                ),
            ),
        ),
        parameters=(
            _model_name_parameter("veo3p1_upscale"),
            _STORAGE_URI,
            _UPSCALE_TASK,
            _CODEC,
            VpeParameterSpec(
                name="resolution",
                location=VpeParameterLocation.PARAMETERS,
                requirement=VpeFieldRequirement.REQUIRED,
                value_type=VpeValueType.STRING,
                allowed_values=(VpeUpscaleResolution.UHD_4K.value,),
                default=VpeUpscaleResolution.UHD_4K.value,
                notes="1080p is not supported for seamless sources.",
            ),
            VpeParameterSpec(
                name="aspectRatio",
                location=VpeParameterLocation.PARAMETERS,
                requirement=VpeFieldRequirement.OPTIONAL,
                value_type=VpeValueType.STRING,
                allowed_values=("16:9",),
                default="16:9",
                notes=(
                    "The sample sends 16:9; the only documented source is a"
                    " 1280x720 video-textures output."
                ),
            ),
            _UPSCALE_COMPRESSION_QUALITY,
            _UPSCALE_SHARPNESS,
            *_SEAMLESS_PARAMETERS,
        ),
        input_video=VpeVideoConstraints(
            min_frames=96,
            max_frames=192,
            frame_sizes=(FRAME_720P,),
            notes="4 to 8 seconds at exactly 24 fps.",
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_4K,),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
            ),
            max_frames=192,
            emits_frame_sequence=True,
            notes="4K only; the codec comes from experiments.codec.",
        ),
        known_issues=(
            VpeKnownIssue(
                issue_id=(
                    VpeKnownIssueId.UPSCALE_SEAMLESS_FLAGS_MUST_MATCH_SOURCE
                ),
                summary=(
                    "Omitted or mismatched seamless flags make the upscaler"
                    " apply the wrong wrapping signature, so seams appear in"
                    " the 4K output."
                ),
                workaround=(
                    "Persist the loop / tessellateHorizontal /"
                    " tessellateVertical flags used for the 720p generation"
                    " and replay them here verbatim."
                ),
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.UPSCALE_SEAMLESS_NO_1080P,
                summary=(
                    "1080p upscaling is not supported for tessellated or"
                    " looping videos."
                ),
                workaround="Offer 4K only for seamless sources.",
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.UPSCALE_4K_PNG16_NO_OPERATION_ID,
                summary=(
                    "4K with lossless_16bit_png may return neither an"
                    " operation id nor the output path."
                ),
                workaround=(
                    "Treat 4K + lossless_16bit_png as unsupported, or poll"
                    " the output prefix in Cloud Storage instead."
                ),
            ),
        ),
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.OMNI_CINE,
        model_name="omni-cine",
        label="Omni-Cine",
        description=(
            "The Omni model on professional plumbing: EXR and 16-bit PNG"
            " sequences in and out, ProRes/DNxHR previews, up to 240 frames."
        ),
        # States a 1280x720 output and exposes no aspectRatio; the docs never
        # discuss orientation for this model.
        orientation=VpeOrientation.FIXED_FRAME_UNSTATED,
        doc_url=f"{_DOC_BASE}/omni-cine",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.PROMPT,
                requirement=VpeFieldRequirement.REQUIRED,
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.VIDEO,
                requirement=VpeFieldRequirement.OPTIONAL,
                mime_types=(
                    MIME_MP4,
                    MIME_QUICKTIME,
                    MIME_MXF,
                    MIME_PNG,
                    MIME_EXR,
                ),
                notes=(
                    "Omit for text-to-video. Frame sequences are passed as a"
                    " glob gcsUri (gs://bucket/path/*.exr)."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.REFERENCE_IMAGES,
                requirement=VpeFieldRequirement.OPTIONAL,
                mime_types=(MIME_PNG, MIME_JPEG, MIME_EXR),
                max_items=7,
                reference_type="asset",
                notes=(
                    "referenceType is lowercase 'asset' here and uppercase"
                    " 'ASSET' for performance generation. EXR references"
                    " require an EXR input video."
                ),
            ),
        ),
        parameters=(
            _model_name_parameter("omni-cine"),
            _STORAGE_URI,
            _COMPRESSION_QUALITY,
            _CODEC,
        ),
        input_video=VpeVideoConstraints(
            min_frames=1,
            max_frames=240,
            frame_sizes=(FRAME_720P,),
            max_bytes_per_file=50 * _MIB,
            max_bytes_per_frame=30 * _MIB,
            notes=(
                "Exactly 24 fps; image sequences are interpreted at 24 fps."
                " EXR inputs must be scene-linear ACES2065-1 (AP0) or ACEScg"
                " (AP1), consistent across every frame."
            ),
        ),
        input_image=VpeImageConstraints(
            any_resolution=True,
            max_bytes=30 * _MIB,
            notes=(
                "Reference images may be any resolution, but a 1280x720 crop"
                " around the region of interest gives the best detail."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_720P,),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
                VpeOutputFormat.EXR16_SEQUENCE,
            ),
            max_frames=240,
            produces_audio=True,
            has_audio_stem=True,
            emits_frame_sequence=True,
            notes=(
                "Preview videos carry generated audio and the folder also"
                " holds sample_0_audio.wav. Output length matches the input"
                " video; text- or image-only requests emit 240 frames. EXR"
                " output keeps the input resolution and colour space, so it"
                " is the one case where output is not 1280x720."
            ),
        ),
        known_issues=(
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.OMNI_CINE_NO_AUDIO_INPUT,
                summary="Audio input is not supported.",
                workaround="Do not offer the audio-reference slot here.",
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.OMNI_CINE_NO_VIDEO_REFERENCES,
                summary=(
                    "Video references are not supported; only a single input"
                    " video plus still references."
                ),
                workaround=(
                    "The app's existing Omni edit flow sends video plus"
                    " images, so it cannot be re-pointed at omni-cine."
                ),
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.OMNI_CINE_NO_SEED,
                summary="Seed for reproducibility is not supported.",
                workaround="Do not send seed; do not promise repeatability.",
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.OMNI_CINE_EXR_HEADERS_DROPPED,
                summary="EXR header attributes on inputs are not preserved.",
                workaround=(
                    "Re-apply the source headers downstream if the finishing"
                    " pipeline depends on them."
                ),
            ),
            VpeKnownIssue(
                issue_id=(VpeKnownIssueId.OMNI_CINE_EXR_OUTPUT_NEEDS_EXR_INPUT),
                summary=(
                    "EXR output is only produced when an EXR input video is"
                    " provided; every other input yields 16-bit PNG frames."
                ),
                workaround=(
                    "Do not advertise EXR delivery unless the source is EXR."
                ),
            ),
        ),
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
        model_name="veo-exp-video-transform",
        label="Video transform",
        description=(
            "Whole-frame restyle, grayscale-masked local edits, and"
            " multi-keyframe interpolation, anchored to the source motion."
        ),
        orientation=VpeOrientation.LANDSCAPE_ONLY,
        doc_url=f"{_DOC_BASE}/video-transform",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.PROMPT,
                requirement=VpeFieldRequirement.REQUIRED,
                notes=(
                    "Describe the scene you want rather than the edit to"
                    " perform; instruction-style prompts do worse."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.VIDEO,
                requirement=VpeFieldRequirement.CONDITIONAL,
                mime_types=(MIME_MP4, MIME_QUICKTIME, MIME_MXF, MIME_PNG),
                notes=(
                    "Documented Required, yet the multi-keyframe I2V sample"
                    " sends only image plus conditioningFrames. Required for"
                    " V2V and masked V2V; omitted for keyframe I2V."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.IMAGE,
                requirement=VpeFieldRequirement.OPTIONAL,
                mime_types=(MIME_PNG, MIME_JPEG),
                notes="First-frame conditioning image.",
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.LAST_FRAME,
                requirement=VpeFieldRequirement.OPTIONAL,
                mime_types=(MIME_PNG, MIME_JPEG),
                notes="Treated as the final target frame of the output.",
            ),
        ),
        parameters=(
            _model_name_parameter("veo-exp-video-transform"),
            _STORAGE_URI,
            _SEED,
            _COMPRESSION_QUALITY,
            _CODEC,
            VpeParameterSpec(
                name="videoTransformStrength",
                location=VpeParameterLocation.EXPERIMENTS,
                requirement=VpeFieldRequirement.REQUIRED,
                value_type=VpeValueType.NUMBER,
                minimum=0.0,
                maximum=1.0,
                exclusive_minimum=True,
                notes=(
                    "0.10-0.15 is the lowest practical range; 0.1-0.4 keeps"
                    " structure, 0.4-1.0 allows geometry changes. Documented"
                    " under Instances but sent inside experiments in every"
                    " sample."
                ),
            ),
            VpeParameterSpec(
                name="numDiffusionSteps",
                location=VpeParameterLocation.EXPERIMENTS,
                requirement=VpeFieldRequirement.REQUIRED,
                value_type=VpeValueType.INTEGER,
                minimum=1,
                maximum=250,
                default=20,
                notes=(
                    "Documented Required with a default of 20; higher is"
                    " sharper and slower."
                ),
            ),
            VpeParameterSpec(
                name="videoTransformMaskGcsUri",
                location=VpeParameterLocation.EXPERIMENTS,
                requirement=VpeFieldRequirement.OPTIONAL,
                value_type=VpeValueType.STRING,
                notes=(
                    "Grayscale ProRes, DNxHR or H264 mask matching the input"
                    " resolution and duration. Each pixel scales strength."
                ),
            ),
            VpeParameterSpec(
                name="conditioningFrames",
                location=VpeParameterLocation.EXPERIMENTS,
                requirement=VpeFieldRequirement.OPTIONAL,
                value_type=VpeValueType.ARRAY,
                notes=(
                    "Items are {image: {gcsUri, mimeType}, frameNum}, with"
                    " frameNum a multiple of 8 from 8 to 184."
                ),
            ),
        ),
        input_video=VpeVideoConstraints(
            min_frames=1,
            max_frames=192,
            frame_sizes=(FRAME_720P,),
            notes=(
                "Exactly 24 fps, 1 to 192 frames. A 16-bit PNG sequence is"
                " passed as a glob gcsUri with mimeType image/png."
            ),
        ),
        input_image=VpeImageConstraints(
            frame_sizes=(FRAME_720P,),
            notes=(
                "16:9 only; 16-bit PNG conditioning images must be exactly"
                " 1280x720."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_720P,),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
            ),
            max_frames=192,
            emits_frame_sequence=True,
            notes="The docs do not state whether audio is generated.",
        ),
        known_issues=(
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.VIDEO_TRANSFORM_MASK_EDGE_PRECISION,
                summary=(
                    "Mask boundaries are coarse, and even zero-valued pixels"
                    " can pick up compression artifacts."
                ),
                workaround=(
                    "Dilate or erode the mask, and composite the output back"
                    " over the source to keep untouched pixels pristine."
                ),
            ),
        ),
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.PERF_ESTIMATION,
        model_name="veo-exp-perf-estimation",
        label="Performance estimation (blue mesh)",
        description=(
            "Step 1 of performance control: extracts an actor's movement and"
            " expression from a video as a blue-mesh clip."
        ),
        orientation=VpeOrientation.LANDSCAPE_ONLY,
        doc_url=f"{_DOC_BASE}/performance-controls",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.VIDEO,
                requirement=VpeFieldRequirement.REQUIRED,
                mime_types=(MIME_MP4,),
                notes="H264 only, unlike most other capabilities.",
            ),
        ),
        parameters=(
            _model_name_parameter("veo-exp-perf-estimation"),
            _STORAGE_URI,
            _SEED,
        ),
        input_video=VpeVideoConstraints(
            min_frames=192,
            max_frames=192,
            frame_sizes=(FRAME_720P,),
            notes=(
                "Exactly 192 frames (8s) at exactly 24 fps; the source is not"
                " rescaled, so 1280x720 landscape is the only input."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_720P,),
            formats=(VpeOutputFormat.H264_MP4,),
            max_frames=192,
            notes=(
                "The blue-mesh clip. Feed its gcsUri to performance"
                " generation as experiments.perfMeshGcsUri."
            ),
        ),
        notes=(
            "prompt is not used by this model, so a UI must not collect one."
        ),
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.PERF_GENERATION,
        model_name="veo-exp-perf-generation",
        label="Performance generation",
        description=(
            "Step 2 of performance control: drives a generated character with"
            " a blue-mesh clip plus reference images and a prompt."
        ),
        orientation=VpeOrientation.LANDSCAPE_ONLY,
        doc_url=f"{_DOC_BASE}/performance-controls",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.PROMPT,
                requirement=VpeFieldRequirement.REQUIRED,
                notes=(
                    "The model anchors on the mesh and the references far"
                    " more strongly than on the prompt."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.REFERENCE_IMAGES,
                requirement=VpeFieldRequirement.REQUIRED,
                mime_types=(MIME_PNG, MIME_JPEG),
                max_items=5,
                reference_type="ASSET",
                notes=(
                    "Uppercase ASSET here, lowercase 'asset' for omni-cine."
                    " Character-sheet images fit several views into one of"
                    " the five slots."
                ),
            ),
        ),
        parameters=(
            _model_name_parameter("veo-exp-perf-generation"),
            _STORAGE_URI,
            _SEED,
            _COMPRESSION_QUALITY,
            _CODEC,
            VpeParameterSpec(
                name="perfMeshGcsUri",
                location=VpeParameterLocation.EXPERIMENTS,
                requirement=VpeFieldRequirement.REQUIRED,
                value_type=VpeValueType.STRING,
                notes=(
                    "The H264 blue-mesh video from performance estimation, or"
                    " a custom mesh. It arrives as a parameter, not as"
                    " instances[].video."
                ),
            ),
        ),
        input_video=VpeVideoConstraints(
            min_frames=192,
            max_frames=192,
            frame_sizes=(FRAME_720P,),
            notes=(
                "Constrains the blue mesh supplied through"
                " experiments.perfMeshGcsUri: exactly 192 frames at 24 fps,"
                " 1280x720 H264. 16-bit V2V input is not enabled."
            ),
        ),
        input_image=VpeImageConstraints(
            frame_sizes=(FRAME_720P,),
            notes=(
                "16:9 only; 16-bit PNG references must be exactly 1280x720."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_720P,),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
            ),
            max_frames=192,
            emits_frame_sequence=True,
            notes="The docs do not state whether audio is generated.",
        ),
        max_prompt_chars=1000,
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
        model_name="veo-exp-a2v-generation",
        label="Dialogue-driven generation",
        description=(
            "Animates a character start frame so the performance and lip-sync"
            " match a supplied speech recording exactly."
        ),
        # 1280x720 output, no aspectRatio parameter, and the doc's "resized
        # and padded internally" means a portrait still is pillarboxed rather
        # than rejected - a silent quality loss, not a portrait path.
        orientation=VpeOrientation.FIXED_FRAME_UNSTATED,
        doc_url=f"{_DOC_BASE}/dialogue-driven-generation",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.PROMPT,
                requirement=VpeFieldRequirement.REQUIRED,
                notes=(
                    "Must describe the character in the image; misalignment"
                    " degrades lip-sync. Prompt for a static camera."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.IMAGE,
                requirement=VpeFieldRequirement.REQUIRED,
                mime_types=(MIME_PNG, MIME_JPEG),
                accepts_inline_bytes=True,
                notes=(
                    "First frame, supplying the visual identity. Either"
                    " gcsUri or bytesBase64Encoded."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.REFERENCE_AUDIOS,
                requirement=VpeFieldRequirement.REQUIRED,
                mime_types=(MIME_WAV, MIME_MP3, MIME_MPEG),
                max_items=1,
                accepts_inline_bytes=True,
                notes=(
                    "Only the first array element is used. WAV, MP3, M4A and"
                    " AAC files are accepted, but the doc lists mime types"
                    " for the first three only."
                ),
            ),
        ),
        parameters=(
            _model_name_parameter("veo-exp-a2v-generation"),
            _STORAGE_URI,
            _COMPRESSION_QUALITY,
            _CODEC,
            VpeParameterSpec(
                name="seed",
                location=VpeParameterLocation.PARAMETERS,
                requirement=VpeFieldRequirement.OPTIONAL,
                value_type=VpeValueType.INTEGER,
                minimum=0,
                maximum=4294967295,
                notes=(
                    "Absent from this capability's parameter table but sent"
                    " by both professional-formats samples for it."
                ),
            ),
        ),
        input_image=VpeImageConstraints(
            frame_sizes=(FRAME_720P,),
            notes=(
                "1280x720 required. Off-spec stills are resized and padded"
                " internally rather than rejected, so a portrait frame comes"
                " back pillarboxed; crop to 16:9 first."
            ),
        ),
        input_audio=VpeAudioConstraints(
            exact_seconds=8.0,
            notes=(
                "Truncated if longer, padded with silence if shorter, and"
                " converted internally to 2-channel 48 kHz."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_720P,),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
            ),
            max_frames=192,
            emits_frame_sequence=True,
            notes=(
                "No new audio is generated, and the docs never say whether"
                " the supplied track is muxed into the preview. Treat the"
                " input recording as the authoritative audio and check the"
                " delivered file before publishing it."
            ),
        ),
        max_prompt_chars=1024,
        notes=(
            "The input table also lists a video format row, but this model"
            " has no video instance field and the professional-formats table"
            " lists only image and referenceAudios as its inputs."
        ),
    ),
    VpeCapability(
        capability_id=VpeCapabilityId.VIDEO_TEXTURES,
        model_name="veo-exp-video-textures",
        label="Video textures",
        description=(
            "Generates video that tiles seamlessly in space, loops seamlessly"
            " in time, or both."
        ),
        # 1280x720 output with no aspectRatio parameter; orientation is never
        # discussed.
        orientation=VpeOrientation.FIXED_FRAME_UNSTATED,
        doc_url=f"{_DOC_BASE}/video-textures",
        instance_fields=(
            VpeInstanceFieldSpec(
                field=VpeInstanceField.PROMPT,
                requirement=VpeFieldRequirement.CONDITIONAL,
                notes=(
                    "Required for text-to-video; optional when a conditioning"
                    " image is supplied, though still strongly recommended."
                    " Tessellation wants continuous patterns, not discrete"
                    " objects that look wrong when tiled."
                ),
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.IMAGE,
                requirement=VpeFieldRequirement.OPTIONAL,
                mime_types=(MIME_PNG, MIME_JPEG),
                notes="First-frame conditioning image.",
            ),
            VpeInstanceFieldSpec(
                field=VpeInstanceField.LAST_FRAME,
                requirement=VpeFieldRequirement.OPTIONAL,
                mime_types=(MIME_PNG, MIME_JPEG),
                notes=(
                    "Anchored to the 8th-to-last output frame, not the final"
                    " one."
                ),
            ),
        ),
        parameters=(
            _model_name_parameter("veo-exp-video-textures"),
            _STORAGE_URI,
            _SEED,
            _COMPRESSION_QUALITY,
            _CODEC,
            *_SEAMLESS_PARAMETERS,
        ),
        input_image=VpeImageConstraints(
            frame_sizes=(FRAME_720P,),
            notes=(
                "1280x720 required; off-spec stills are resized and padded"
                " internally rather than rejected."
            ),
        ),
        output=VpeOutputSpec(
            frame_sizes=(FRAME_720P,),
            formats=(
                VpeOutputFormat.H264_MP4,
                VpeOutputFormat.PRORES_MOV,
                VpeOutputFormat.DNXHR_MXF,
                VpeOutputFormat.PNG16_SEQUENCE,
            ),
            max_frames=192,
            emits_frame_sequence=True,
            notes=(
                "Upscale seamless output with the UPSCALE_SEAMLESS"
                " capability, replaying the same seamless flags."
            ),
        ),
        known_issues=(
            VpeKnownIssue(
                issue_id=(VpeKnownIssueId.TEXTURES_LOOP_WITH_IMAGE_UNSUPPORTED),
                summary=(
                    "Looping combined with image conditioning is unsupported"
                    " and gives unpredictable results."
                ),
                workaround=(
                    "Send the same still as image and lastFrame with loop"
                    " false; the clip then starts and ends on that frame."
                ),
            ),
            VpeKnownIssue(
                issue_id=VpeKnownIssueId.TEXTURES_LAST_FRAME_ANCHORED_EARLY,
                summary=(
                    "lastFrame conditions the 8th-to-last frame rather than"
                    " the final frame."
                ),
                workaround=(
                    "Do not describe it in the UI as an exact end frame."
                ),
            ),
            VpeKnownIssue(
                issue_id=(
                    VpeKnownIssueId.TEXTURES_NEEDS_TESSELLATED_CONDITIONING
                ),
                summary=(
                    "Conditioning a tessellated request on a non-tessellated"
                    " image may produce non-tessellating boundaries."
                ),
                workaround=(
                    "Condition on a frame taken from a previous tessellated"
                    " output."
                ),
            ),
        ),
    ),
)


def _build_registry(
    capabilities: tuple[VpeCapability, ...],
) -> Mapping[VpeCapabilityId, VpeCapability]:
    """Indexes the capability list by id, rejecting duplicates.

    Args:
        capabilities: Every capability entry, in presentation order.

    Returns:
        A read-only mapping from capability id to entry.

    Raises:
        ValueError: If two entries share an id, or an id is missing an entry.
    """
    registry: dict[VpeCapabilityId, VpeCapability] = {}
    for capability in capabilities:
        if capability.capability_id in registry:
            raise ValueError(
                f"Duplicate capability id {capability.capability_id.value}",
            )
        registry[capability.capability_id] = capability
    missing = set(VpeCapabilityId) - set(registry)
    if missing:
        raise ValueError(
            "Capability ids without an entry: "
            + ", ".join(sorted(item.value for item in missing)),
        )
    return MappingProxyType(registry)


# Read-only so a caller cannot mutate the shared contract at runtime.
VPE_CAPABILITIES: Mapping[VpeCapabilityId, VpeCapability] = _build_registry(
    VPE_CAPABILITY_LIST,
)


def get_capability(
    capability_id: VpeCapabilityId | str,
) -> VpeCapability:
    """Looks up a capability by id.

    Args:
        capability_id: A VpeCapabilityId, or its string value.

    Returns:
        The matching capability entry.

    Raises:
        ValueError: If the id is not a known VPE capability.
    """
    try:
        resolved = VpeCapabilityId(capability_id)
    except ValueError as error:
        known = ", ".join(item.value for item in VpeCapabilityId)
        raise ValueError(
            f"Unknown VPE capability '{capability_id}'. Known: {known}",
        ) from error
    return VPE_CAPABILITIES[resolved]


def capabilities_for_model(model_name: str) -> tuple[VpeCapability, ...]:
    """Returns every capability served by a checkpoint.

    More than one entry can share a model name - veo3p1_upscale backs both
    the plain and the seamless upscaler - so this returns a tuple rather than
    a single entry.

    Args:
        model_name: An experiments.modelName value.

    Returns:
        The matching entries, empty if the model name is unknown.
    """
    return tuple(
        capability
        for capability in VPE_CAPABILITY_LIST
        if capability.model_name == model_name
    )
