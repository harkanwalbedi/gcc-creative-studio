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
"""Builds the ``:predictLongRunning`` request body for a VPE capability.

Every VPE capability posts to the same publisher endpoint with the same
outer shape - ``{"instances": [ {...} ], "parameters": {...}}`` - and then
disagrees about where each individual value goes. ``storageUri`` is a top
level parameter, ``modelName`` is one level down in ``experiments``, the
seamless flags are two levels down, the upscaler's ``task`` /
``resolution`` / ``aspectRatio`` / ``sharpness`` are top level while video
transform's ``videoTransformStrength`` and ``numDiffusionSteps`` - which
the docs list under *Instances* - are sent inside ``experiments``. Nesting
is the failure mode this module exists to prevent, so each capability gets
its own builder transcribed from its documented curl sample rather than one
clever generic serializer.

What is shared is the checking, and it is deliberately doubled. Before
building, the request is compared against the registry so a value the
capability cannot express is refused instead of being quietly dropped
(a ``seed`` on omni-cine, a seamless flag on the plain upscaler). After
building, the emitted JSON is walked and every key is looked up in the
registry, which catches the other direction: a builder that misspells a
wire name or nests it one level off. Neither check can be replaced by the
other.

This module does no media inspection - fps, frame count and frame size are
preflight's job - and no prompt trimming. The perf-generation limit is
documented as "content beyond this limit may not have a noticeable effect",
not as an error, so rejecting a long prompt there would refuse a request
the API accepts. The dialogue-driven page does state a flat "Maximum 1024
characters", but the registry records both numbers in one
``max_prompt_chars`` with no soft/hard distinction, so neither is enforced
here. Trimming belongs to the DTO layer, which is talking to someone who
can shorten the text.

None of it has been executed against the live API; the program is
allowlist-gated. The doc samples transcribed in
``tests/videos/vpe/test_payloads.py`` are the only available verification,
so a disagreement between a builder and a sample means the builder is
wrong.
"""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from src.videos.vpe.capabilities import (
    MIME_EXR,
    MIME_MP3,
    MIME_MP4,
    MIME_MPEG,
    MIME_MXF,
    MIME_PNG,
    MIME_QUICKTIME,
    MIME_WAV,
    VpeCapability,
    VpeCapabilityId,
    VpeCodec,
    VpeCompressionQuality,
    VpeInstanceField,
    VpeInstanceFieldSpec,
    VpeParameterLocation,
    VpeParameterSpec,
    VpeSuperResMode,
    VpeUpscaleResolution,
    VpeValueType,
    get_capability,
)

# Wire keys that are not registry-driven because they are not parameters or
# instance fields: they are the shape of an object inside one.
_KEY_GCS_URI = "gcsUri"
_KEY_INLINE_BYTES = "bytesBase64Encoded"
_KEY_MIME_TYPE = "mimeType"
_KEY_IMAGE = "image"
_KEY_AUDIO = "audio"
_KEY_REFERENCE_TYPE = "referenceType"
_KEY_FRAME_NUM = "frameNum"

_GCS_SCHEME = "gs://"

# conditioningFrames insertion points, from the video transform doc: "(8, 16,
# 24, 32, ..., 184)". Frame 0 is instances[].image, which is why the run does
# not start at zero.
_CONDITIONING_FRAME_STEP = 8
_CONDITIONING_FRAME_MIN = 8
_CONDITIONING_FRAME_MAX = 184

# Reverse index of the nesting levels, so the post-build walk knows which
# dicts are further parameter blocks and which are opaque values.
_LOCATIONS_BY_PATH: Mapping[str, VpeParameterLocation] = MappingProxyType(
    {location.value: location for location in VpeParameterLocation},
)


class VpePayloadError(ValueError):
    """A request that cannot be expressed as a legal VPE payload."""


def _enum_value(value: Any) -> Any:
    """Unwraps a registry enum so the payload carries plain JSON types.

    Args:
        value: A scalar, possibly one of the registry's str enums.

    Returns:
        The enum's value, or the argument unchanged.
    """
    return value.value if isinstance(value, Enum) else value


@dataclass(frozen=True, slots=True)
class VpeMediaRef:
    """One media input: a Cloud Storage object, or inline bytes.

    The same shape serves videos, stills and audio because the API uses the
    same object for all three; only the key it hangs off differs.
    """

    mime_type: str
    gcs_uri: str | None = None
    bytes_base64: str | None = None

    def __post_init__(self) -> None:
        """Rejects a reference that names no readable source."""
        if not self.mime_type.strip():
            raise VpePayloadError("A media reference needs a mimeType")
        if bool(self.gcs_uri) == bool(self.bytes_base64):
            raise VpePayloadError(
                "Provide exactly one of gcs_uri or bytes_base64",
            )
        if self.gcs_uri is not None and not self.gcs_uri.startswith(
            _GCS_SCHEME,
        ):
            raise VpePayloadError(
                f"VPE reads from Cloud Storage only: {self.gcs_uri!r}",
            )

    @property
    def is_sequence(self) -> bool:
        """Returns True if the URI is a frame-sequence glob.

        A PNG or EXR frame sequence is submitted as a wildcard gcsUri
        (``gs://bucket/frames/*.png``) with the frame's mime type, not the
        container's; the service lists and sorts the objects itself.
        """
        return self.gcs_uri is not None and "*" in self.gcs_uri


@dataclass(frozen=True, slots=True)
class VpeConditioningFrame:
    """A keyframe pinned to a frame index inside the generated clip."""

    image: VpeMediaRef
    frame_num: int

    def __post_init__(self) -> None:
        """Rejects an insertion index the model cannot condition on."""
        if self.frame_num % _CONDITIONING_FRAME_STEP:
            raise VpePayloadError(
                f"frameNum must be a multiple of {_CONDITIONING_FRAME_STEP},"
                f" got {self.frame_num}",
            )
        if not (
            _CONDITIONING_FRAME_MIN <= self.frame_num <= _CONDITIONING_FRAME_MAX
        ):
            raise VpePayloadError(
                f"frameNum must be within {_CONDITIONING_FRAME_MIN} to"
                f" {_CONDITIONING_FRAME_MAX}, got {self.frame_num}",
            )


@dataclass(frozen=True, slots=True)
class VpeSeamlessFlags:
    """The three ``experiments.seamless`` booleans.

    Grouped rather than loose so that an upscale can replay exactly what a
    generation used: the upscaler applies a wrapping signature derived from
    these, and a mismatch puts visible seams in the 4K output.
    """

    loop: bool = False
    tessellate_horizontal: bool = False
    tessellate_vertical: bool = False


_TARGET_AUDIO_MIME_TYPES = frozenset(
    {
        MIME_WAV,
        MIME_MP3,
        MIME_MPEG,
        "audio/aac",
        "audio/mp4",
        MIME_MP4,
        MIME_QUICKTIME,
    }
)

_OMNI_TRANSFORM_VIDEO_MIME_TYPES = frozenset(
    {
        MIME_MP4,
        MIME_QUICKTIME,
        MIME_MXF,
        MIME_PNG,
        MIME_EXR,
    }
)


@dataclass(frozen=True, slots=True)
class VpeAudioControl:
    """Audio steering inside ``experiments.customParameters.audioControl``.

    ``target_audio`` and ``use_target_audio_from_video`` are mutually exclusive.
    """

    target_audio: VpeMediaRef | None = None
    use_target_audio_from_video: bool | None = None

    def __post_init__(self) -> None:
        """Enforces mutual exclusion and Cloud Storage media rules."""
        if (
            self.target_audio is not None
            and self.use_target_audio_from_video is not None
        ):
            raise VpePayloadError(
                "audioControl.targetAudio and"
                " audioControl.useTargetAudioFromVideo are mutually exclusive",
            )
        if (
            self.target_audio is None
            and self.use_target_audio_from_video is None
        ):
            raise VpePayloadError(
                "audioControl requires either targetAudio or"
                " useTargetAudioFromVideo",
            )
        if self.target_audio is not None:
            if self.target_audio.bytes_base64 is not None:
                raise VpePayloadError(
                    "audioControl.targetAudio must be a Cloud Storage URI",
                )
            if (
                self.target_audio.mime_type.lower()
                not in _TARGET_AUDIO_MIME_TYPES
            ):
                raise VpePayloadError(
                    "audioControl.targetAudio has unsupported mimeType"
                    f" {self.target_audio.mime_type!r}",
                )


@dataclass(frozen=True, slots=True)
class VpeOmniVideoTransform:
    """Omni-Cine ``experiments.customParameters.videoTransform`` block."""

    initialization_video: VpeMediaRef
    noise_strength: float | None = None
    mask: VpeMediaRef | None = None

    def __post_init__(self) -> None:
        """Validates initialization video, noiseStrength range, and mask."""
        if self.initialization_video.bytes_base64 is not None:
            raise VpePayloadError(
                "videoTransform.initializationVideo must be a Cloud Storage"
                " URI",
            )
        if (
            self.initialization_video.mime_type.lower()
            not in _OMNI_TRANSFORM_VIDEO_MIME_TYPES
        ):
            raise VpePayloadError(
                "videoTransform.initializationVideo has unsupported mimeType"
                f" {self.initialization_video.mime_type!r}",
            )
        if self.noise_strength is not None and (
            isinstance(self.noise_strength, bool)
            or not 0.0 <= self.noise_strength <= 1.0
        ):
            raise VpePayloadError(
                "videoTransform.noiseStrength must be between 0.0 and 1.0,"
                f" got {self.noise_strength!r}",
            )
        if self.mask is not None:
            if self.mask.bytes_base64 is not None:
                raise VpePayloadError(
                    "videoTransform.mask must be a Cloud Storage URI",
                )
            if (
                self.mask.mime_type.lower()
                not in _OMNI_TRANSFORM_VIDEO_MIME_TYPES
            ):
                raise VpePayloadError(
                    "videoTransform.mask has unsupported mimeType"
                    f" {self.mask.mime_type!r}",
                )


@dataclass(frozen=True, slots=True)
class VpeRequest:
    """Everything any VPE / GMP capability can be asked for.

    One flat shape covering every capability. Anything the chosen capability
    does not accept is refused by name at build time, so flatness cannot turn
    into a silently ignored field.
    """

    capability_id: VpeCapabilityId
    storage_uri: str
    prompt: str | None = None
    video: VpeMediaRef | None = None
    image: VpeMediaRef | None = None
    last_frame: VpeMediaRef | None = None
    reference_images: tuple[VpeMediaRef, ...] = ()
    reference_audios: tuple[VpeMediaRef, ...] = ()
    seed: int | None = None
    codec: str | None = None
    compression_quality: str | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None
    sharpness: int | None = None
    fps: int | None = None
    video_transform_strength: float | None = None
    num_diffusion_steps: int | None = None
    video_transform_mask_gcs_uri: str | None = None
    conditioning_frames: tuple[VpeConditioningFrame, ...] = ()
    perf_mesh_gcs_uri: str | None = None
    seamless: VpeSeamlessFlags | None = None
    super_res_mode: str | None = None
    super_res_steps: int | None = None
    exr_color_space_override: str | None = None
    audio_control: VpeAudioControl | None = None
    omni_video_transform: VpeOmniVideoTransform | None = None

    def __post_init__(self) -> None:
        """Rejects an output destination VPE could not write to."""
        if not self.storage_uri.startswith(_GCS_SCHEME):
            raise VpePayloadError(
                "storageUri must be a Cloud Storage URI, got"
                f" {self.storage_uri!r}",
            )


# request attribute -> the instances[0] key it becomes.
_INSTANCE_SLOTS: Mapping[str, VpeInstanceField] = MappingProxyType(
    {
        "prompt": VpeInstanceField.PROMPT,
        "video": VpeInstanceField.VIDEO,
        "image": VpeInstanceField.IMAGE,
        "last_frame": VpeInstanceField.LAST_FRAME,
        "reference_images": VpeInstanceField.REFERENCE_IMAGES,
        "reference_audios": VpeInstanceField.REFERENCE_AUDIOS,
    },
)


@dataclass(frozen=True, slots=True)
class _ScalarSlot:
    """A request attribute that becomes one scalar parameter."""

    attribute: str
    name: str
    location: VpeParameterLocation


# Every scalar parameter, with the nesting level it belongs at. This table is
# what makes "wrong level" a build error rather than a silent 400 from the
# API two seconds later.
_SCALAR_SLOTS: tuple[_ScalarSlot, ...] = (
    _ScalarSlot("seed", "seed", VpeParameterLocation.PARAMETERS),
    _ScalarSlot(
        "compression_quality",
        "compressionQuality",
        VpeParameterLocation.PARAMETERS,
    ),
    _ScalarSlot("resolution", "resolution", VpeParameterLocation.PARAMETERS),
    _ScalarSlot("aspect_ratio", "aspectRatio", VpeParameterLocation.PARAMETERS),
    _ScalarSlot("sharpness", "sharpness", VpeParameterLocation.PARAMETERS),
    _ScalarSlot("fps", "fps", VpeParameterLocation.PARAMETERS),
    _ScalarSlot("codec", "codec", VpeParameterLocation.EXPERIMENTS),
    _ScalarSlot(
        "video_transform_strength",
        "videoTransformStrength",
        VpeParameterLocation.EXPERIMENTS,
    ),
    _ScalarSlot(
        "num_diffusion_steps",
        "numDiffusionSteps",
        VpeParameterLocation.EXPERIMENTS,
    ),
    _ScalarSlot(
        "video_transform_mask_gcs_uri",
        "videoTransformMaskGcsUri",
        VpeParameterLocation.EXPERIMENTS,
    ),
    _ScalarSlot(
        "perf_mesh_gcs_uri",
        "perfMeshGcsUri",
        VpeParameterLocation.EXPERIMENTS,
    ),
    _ScalarSlot(
        "super_res_mode",
        "mode",
        VpeParameterLocation.EXPERIMENTS_CUSTOM_SUPER_RES,
    ),
    _ScalarSlot(
        "super_res_steps",
        "steps",
        VpeParameterLocation.EXPERIMENTS_CUSTOM_SUPER_RES,
    ),
    _ScalarSlot(
        "exr_color_space_override",
        "exrColorSpaceOverride",
        VpeParameterLocation.EXPERIMENTS_CUSTOM,
    ),
)


# Scalar parameters whose value is a Cloud Storage URI in its own right,
# rather than a VpeMediaRef that validates its own. VPE reads inputs from
# Cloud Storage only - the user guide states direct upload is unsupported -
# so these get the same check the media references get.
_GCS_URI_PARAMETERS = frozenset({"videoTransformMaskGcsUri", "perfMeshGcsUri"})


def _is_absent(value: Any) -> bool:
    """Returns True when a request field asks for nothing at all.

    Whitespace is the case a plain truthiness test gets wrong: a prompt of
    spaces guides no generation, but it is documented Required for five of
    the eight capabilities and the API would take it at face value.

    Args:
        value: A request attribute: a string, a media reference, or a tuple.

    Returns:
        True if the caller supplied no usable value.
    """
    if isinstance(value, str):
        return not value.strip()
    return not value


def _check_instance_slots(
    capability: VpeCapability,
    request: VpeRequest,
) -> None:
    """Checks the media and prompt inputs against the capability.

    Args:
        capability: The registry entry being built for.
        request: The caller's request.

    Raises:
        VpePayloadError: If an input is not accepted by this capability, an
            unconditionally required input is missing, or an array input
            exceeds its documented item limit.
    """
    for attribute, field in _INSTANCE_SLOTS.items():
        value = getattr(request, attribute)
        absent = _is_absent(value)
        spec = capability.instance_field(field)
        if not absent and spec is None:
            raise VpePayloadError(
                f"{capability.capability_id.value} does not accept"
                f" instances[].{field.value}",
            )
        if spec is None:
            continue
        if spec.is_required and absent:
            raise VpePayloadError(
                f"{capability.capability_id.value} requires"
                f" instances[].{field.value}",
            )
        if spec.max_items is not None and len(value) > spec.max_items:
            raise VpePayloadError(
                f"{field.value} accepts at most {spec.max_items} items,"
                f" got {len(value)}",
            )


def _check_scalar_slots(
    capability: VpeCapability,
    request: VpeRequest,
) -> None:
    """Checks every supplied scalar parameter against the registry.

    Args:
        capability: The registry entry being built for.
        request: The caller's request.

    Raises:
        VpePayloadError: If a parameter is not accepted by this capability
            or its value is outside the documented range or value set.
    """
    for slot in _SCALAR_SLOTS:
        value = _enum_value(getattr(request, slot.attribute))
        if value is None:
            continue
        spec = capability.parameter(slot.name, slot.location)
        if spec is None:
            raise VpePayloadError(
                f"{capability.capability_id.value} does not accept"
                f" {slot.location.value}.{slot.name}",
            )
        if slot.name in _GCS_URI_PARAMETERS and not str(value).startswith(
            _GCS_SCHEME,
        ):
            raise VpePayloadError(
                f"{spec.json_path} must be a Cloud Storage URI, got"
                f" {value!r}",
            )
        _check_scalar_value(capability, spec, value)


def _check_scalar_value(
    capability: VpeCapability,
    spec: VpeParameterSpec,
    value: Any,
) -> None:
    """Checks one scalar against its documented domain.

    Args:
        capability: The registry entry being built for, for the message.
        spec: The parameter's registry entry.
        value: The caller's value, already unwrapped from any enum.

    Raises:
        VpePayloadError: If the value is not one the docs allow.
    """
    label = f"{capability.capability_id.value} {spec.json_path}"
    if spec.value_type is VpeValueType.INTEGER and (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        raise VpePayloadError(f"{label} must be an integer, got {value!r}")
    if spec.allowed_values and value not in spec.allowed_values:
        raise VpePayloadError(
            f"{label} must be one of {list(spec.allowed_values)},"
            f" got {value!r}",
        )
    if spec.minimum is not None:
        if spec.exclusive_minimum:
            too_low = value <= spec.minimum
            bound = "greater than"
        else:
            too_low = value < spec.minimum
            bound = "at least"
        if too_low:
            raise VpePayloadError(
                f"{label} must be {bound} {spec.minimum}, got {value!r}",
            )
    if spec.maximum is not None and value > spec.maximum:
        raise VpePayloadError(
            f"{label} must be at most {spec.maximum}, got {value!r}",
        )


def _check_group_slots(
    capability: VpeCapability,
    request: VpeRequest,
) -> None:
    """Checks structured group parameters against the capability.

    Args:
        capability: The registry entry being built for.
        request: The caller's request.

    Raises:
        VpePayloadError: If the capability has no such parameter.
    """
    has_seamless = capability.parameter(
        "loop",
        VpeParameterLocation.EXPERIMENTS_SEAMLESS,
    )
    if request.seamless is not None and has_seamless is None:
        raise VpePayloadError(
            f"{capability.capability_id.value} has no seamless flags",
        )
    has_keyframes = capability.parameter(
        "conditioningFrames",
        VpeParameterLocation.EXPERIMENTS,
    )
    if request.conditioning_frames and has_keyframes is None:
        raise VpePayloadError(
            f"{capability.capability_id.value} does not accept"
            " conditioningFrames",
        )
    has_audio_control = capability.parameter(
        "targetAudio",
        VpeParameterLocation.EXPERIMENTS_CUSTOM_AUDIO_CONTROL,
    )
    if request.audio_control is not None and has_audio_control is None:
        raise VpePayloadError(
            f"{capability.capability_id.value} does not accept"
            " customParameters.audioControl",
        )
    has_omni_transform = capability.parameter(
        "initializationVideo",
        VpeParameterLocation.EXPERIMENTS_CUSTOM_VIDEO_TRANSFORM,
    )
    if request.omni_video_transform is not None and has_omni_transform is None:
        raise VpePayloadError(
            f"{capability.capability_id.value} does not accept"
            " customParameters.videoTransform",
        )


def _media(
    capability: VpeCapability,
    field: VpeInstanceField,
    ref: VpeMediaRef,
) -> dict[str, Any]:
    """Renders one media reference, checked against its field's contract.

    Args:
        capability: The registry entry being built for.
        field: The instance field the reference will hang off.
        ref: The caller's media reference.

    Returns:
        The ``{gcsUri | bytesBase64Encoded, mimeType}`` object.

    Raises:
        VpePayloadError: If the mime type is not documented for this field,
            or inline bytes are used where only Cloud Storage is accepted.
    """
    spec = capability.instance_field(field)
    if spec is None:
        raise VpePayloadError(
            f"{capability.capability_id.value} does not accept"
            f" instances[].{field.value}",
        )
    return _media_for_spec(capability, spec, ref)


def _media_for_spec(
    capability: VpeCapability,
    spec: VpeInstanceFieldSpec,
    ref: VpeMediaRef,
) -> dict[str, Any]:
    """Renders one media reference against an already-resolved field spec.

    Args:
        capability: The registry entry being built for, for the message.
        spec: The field contract the reference must satisfy.
        ref: The caller's media reference.

    Returns:
        The ``{gcsUri | bytesBase64Encoded, mimeType}`` object.

    Raises:
        VpePayloadError: If the mime type is not documented for this field,
            or inline bytes are used where only Cloud Storage is accepted.
    """
    if not spec.accepts(ref.mime_type):
        raise VpePayloadError(
            f"{capability.capability_id.value} {spec.field.value} accepts"
            f" {list(spec.mime_types)}, got {ref.mime_type!r}",
        )
    if ref.bytes_base64 is not None and not spec.accepts_inline_bytes:
        raise VpePayloadError(
            f"{capability.capability_id.value} {spec.field.value} must be a"
            " Cloud Storage URI; inline bytes are not documented for it",
        )
    # The field check accepts any casing, so a caller may hand in
    # "Video/MP4"; the docs spell every mime type exactly once and in lower
    # case, and that is the spelling the service matches on. Accepting one
    # form and sending another is how a reference passes every local check
    # and is then rejected by the API.
    mime_type = ref.mime_type.lower()
    if ref.gcs_uri is not None:
        return {_KEY_GCS_URI: ref.gcs_uri, _KEY_MIME_TYPE: mime_type}
    return {
        _KEY_INLINE_BYTES: ref.bytes_base64,
        _KEY_MIME_TYPE: mime_type,
    }


def _reference_images(
    capability: VpeCapability,
    refs: tuple[VpeMediaRef, ...],
) -> list[dict[str, Any]]:
    """Renders the referenceImages array with its per-model tag casing.

    ``referenceType`` is lowercase ``asset`` in the omni-cine sample and
    uppercase ``ASSET`` in the performance-generation sample. Both are
    transcribed in the registry, so the casing is read from there rather
    than guessed at once for both.

    Args:
        capability: The registry entry being built for.
        refs: The caller's reference images, in order.

    Returns:
        A list of ``{image, referenceType}`` objects.

    Raises:
        VpePayloadError: If the capability takes no reference images.
    """
    spec = capability.instance_field(VpeInstanceField.REFERENCE_IMAGES)
    if spec is None:
        raise VpePayloadError(
            f"{capability.capability_id.value} does not accept"
            " instances[].referenceImages",
        )
    return [
        {
            _KEY_IMAGE: _media_for_spec(capability, spec, ref),
            _KEY_REFERENCE_TYPE: spec.reference_type,
        }
        for ref in refs
    ]


def _reference_audios(
    capability: VpeCapability,
    refs: tuple[VpeMediaRef, ...],
) -> list[dict[str, Any]]:
    """Renders the referenceAudios array.

    Unlike referenceImages the entries carry no ``referenceType``; the doc
    sample is a bare ``{"audio": {...}}``.

    Args:
        capability: The registry entry being built for.
        refs: The caller's audio references, in order.

    Returns:
        A list of ``{audio}`` objects.
    """
    return [
        {
            _KEY_AUDIO: _media(
                capability,
                VpeInstanceField.REFERENCE_AUDIOS,
                ref,
            ),
        }
        for ref in refs
    ]


def _conditioning_frames(
    capability: VpeCapability,
    frames: tuple[VpeConditioningFrame, ...],
) -> list[dict[str, Any]]:
    """Renders experiments.conditioningFrames.

    The stills are validated against the ``image`` field's mime types: the
    conditioning frames are the same kind of input as the first frame, and
    the doc gives them no separate format table.

    Args:
        capability: The registry entry being built for.
        frames: The caller's keyframes, in order.

    Returns:
        A list of ``{image, frameNum}`` objects.
    """
    return [
        {
            _KEY_IMAGE: _media(
                capability,
                VpeInstanceField.IMAGE,
                frame.image,
            ),
            _KEY_FRAME_NUM: frame.frame_num,
        }
        for frame in frames
    ]


def _seamless(request: VpeRequest) -> dict[str, bool]:
    """Renders experiments.seamless with all three flags always present.

    Every documented seamless sample sends all three booleans, including
    the false ones, and the upscaler's known issue is precisely that an
    omitted flag makes it apply the wrong wrapping signature. Sending the
    full triple costs nothing and removes the class of bug where a default
    on one side of a two-call workflow disagrees with the other.

    Args:
        request: The caller's request; a missing group means all false.

    Returns:
        The ``{loop, tessellateHorizontal, tessellateVertical}`` object.
    """
    flags = request.seamless or VpeSeamlessFlags()
    return {
        "loop": flags.loop,
        "tessellateHorizontal": flags.tessellate_horizontal,
        "tessellateVertical": flags.tessellate_vertical,
    }


def _set_if(target: dict[str, Any], key: str, value: Any) -> None:
    """Writes a parameter only when the caller asked for it.

    An unsent optional parameter and one sent at its documented default are
    not the same request: ``resolution`` defaults to ``720p``, which
    upscales nothing, and ``compressionQuality`` changes the delivered
    codec profile. Nothing is defaulted in on the caller's behalf.

    Args:
        target: The parameter block to write into.
        key: The wire name.
        value: The value, skipped entirely when None.
    """
    if value is not None:
        target[key] = _enum_value(value)


def _start(
    capability: VpeCapability,
    request: VpeRequest,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Seeds the three blocks every capability shares.

    Args:
        capability: The registry entry being built for.
        request: The caller's request.

    Returns:
        The empty instance, the parameters block carrying ``storageUri``,
        and the experiments block carrying ``modelName``.
    """
    experiments: dict[str, Any] = {"modelName": capability.model_name}
    parameters: dict[str, Any] = {
        "storageUri": request.storage_uri,
        "experiments": experiments,
    }
    return {}, parameters, experiments


def _finish(
    instance: dict[str, Any],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Wraps an instance and its parameters in the request envelope.

    Args:
        instance: The single ``instances[0]`` object.
        parameters: The fully populated parameters block.

    Returns:
        The complete ``:predictLongRunning`` body.
    """
    return {"instances": [instance], "parameters": parameters}


def _upscale_task(capability: VpeCapability) -> Any:
    """Returns the upscaler's ``task`` value from the registry.

    ``task`` is absent from the upscaler's parameter table but present in
    every documented sample, so it is sent rather than left to default.

    Args:
        capability: One of the two upscaler entries.

    Returns:
        The documented task string.
    """
    spec = capability.parameter("task", VpeParameterLocation.PARAMETERS)
    if spec is None:  # pragma: no cover - registry invariant
        raise VpePayloadError("The upscaler entry lost its task parameter")
    return spec.default


def _upscale_resolution(capability: VpeCapability, request: VpeRequest) -> Any:
    """Returns the resolution the request will actually carry.

    The plain upscaler leaves the parameter out when the caller named no
    resolution: its documented default is 720p, which upscales nothing, so
    filling one in would silently change the job. The seamless entry marks
    the parameter required with 4k as its only legal value - 1080p is not
    supported for tessellated or looping sources - so that one is completed
    from the registry.

    Args:
        capability: One of the two upscaler entries.
        request: The caller's request.

    Returns:
        The resolution to send, or None to omit the parameter.
    """
    if request.resolution is not None:
        return _enum_value(request.resolution)
    spec = capability.parameter("resolution", VpeParameterLocation.PARAMETERS)
    if spec is None:  # pragma: no cover - registry invariant
        raise VpePayloadError("The upscale entry lost its resolution parameter")
    return spec.default if spec.is_required else None


def _check_png16_is_4k(request: VpeRequest, resolution: Any) -> None:
    """Rejects 16-bit PNG delivery at any resolution but 4K.

    "lossless_16bit_png is only available at 4k resolution" is a rule about
    two parameters at once, so neither the per-value domain check nor
    preflight - which measures one file at a time and never sees the
    parameters - can catch it. An omitted resolution counts as a violation:
    the documented default is 720p.

    Args:
        request: The caller's request.
        resolution: The resolution the payload will carry, or None.

    Raises:
        VpePayloadError: If 16-bit PNG output was asked for below 4K.
    """
    quality = _enum_value(request.compression_quality)
    if quality != VpeCompressionQuality.LOSSLESS_16BIT_PNG.value:
        return
    if resolution != VpeUpscaleResolution.UHD_4K.value:
        asked = resolution or "no resolution (the default is 720p)"
        raise VpePayloadError(
            f"compressionQuality {quality} is only available at"
            f" {VpeUpscaleResolution.UHD_4K.value}, got {asked}",
        )


def _check_codec_is_4k(request: VpeRequest, resolution: Any) -> None:
    """Rejects a professional codec at any upscale resolution but 4K.

    The upscaler's two output tables disagree by resolution rather than by
    codec: the 720p-to-4K row lists "ProRes, DNxHR, H264 (video/mp4), or
    16-bit PNG frames", while the 720p-to-1080p row lists H264 alone. Asking
    for ProRes at 1080p is therefore requesting a format that path cannot
    emit, and like the 16-bit PNG rule it pairs two parameters, so no
    per-value check can see it.

    Args:
        request: The caller's request.
        resolution: The resolution the payload will carry, or None.

    Raises:
        VpePayloadError: If a non-H.264 codec was asked for below 4K.
    """
    codec = _enum_value(request.codec)
    if codec is None or codec == VpeCodec.H264.value:
        return
    if resolution != VpeUpscaleResolution.UHD_4K.value:
        asked = resolution or "no resolution (the default is 720p)"
        raise VpePayloadError(
            f"codec {codec} is only available at"
            f" {VpeUpscaleResolution.UHD_4K.value}; {asked} outputs H.264"
            " only",
        )


def _build_upscale(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the plain upscaler body.

    Everything except ``modelName`` and ``codec`` sits at the top level of
    ``parameters``, which is the opposite of what most of the other
    capabilities do with their own knobs.

    ``experiments.codec`` is sent even though neither upscaler parameter
    table lists it and no upscaler sample carries one. Three separate doc
    statements make it reachable - the tessellation page says outright that
    "the video codec is specified by parameters.experiments.codec", and both
    the upscaler and professional-formats pages list ProRes and DNxHR among
    the 4K outputs. Refusing it would make a documented output format
    unrequestable, which is the worse of the two readings.

    Args:
        capability: The upscale registry entry.
        request: The caller's request.

    Returns:
        The request body.
    """
    instance, parameters, experiments = _start(capability, request)
    instance[VpeInstanceField.VIDEO.value] = _media(
        capability,
        VpeInstanceField.VIDEO,
        request.video,
    )
    resolution = _upscale_resolution(capability, request)
    _check_png16_is_4k(request, resolution)
    _check_codec_is_4k(request, resolution)
    _set_if(experiments, "codec", request.codec)
    parameters["task"] = _upscale_task(capability)
    _set_if(parameters, "resolution", resolution)
    _set_if(parameters, "aspectRatio", request.aspect_ratio)
    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(parameters, "sharpness", request.sharpness)
    return _finish(instance, parameters)


def _check_seamless_delivery(request: VpeRequest) -> None:
    """Rejects a seamless upscale that no delivery format can carry.

    Stated nowhere, and found only by running it. The service answers:
    "Seamless tiling is only supported when the `compressionQuality`
    parameter is set to `lossless_16bit_png`, or when using ProRes or DNxHR
    codecs." No prose in the tessellation or upscaler pages mentions the
    rule; the tessellation sample happens to satisfy it by sending
    lossless_16bit_png, so copying that sample works and varying from it
    fails with nothing to warn you.

    Checked here rather than left to the API because the rejection costs a
    round trip and reads as a payload defect.

    Args:
        request: The caller's request.

    Raises:
        VpePayloadError: If neither condition is met.
    """
    quality = _enum_value(request.compression_quality)
    codec = _enum_value(request.codec)
    if quality == VpeCompressionQuality.LOSSLESS_16BIT_PNG.value:
        return
    if codec in (VpeCodec.PRORES.value, VpeCodec.DNXHR.value):
        return
    raise VpePayloadError(
        "seamless output needs compressionQuality"
        f" {VpeCompressionQuality.LOSSLESS_16BIT_PNG.value}, or codec"
        f" {VpeCodec.PRORES.value} or {VpeCodec.DNXHR.value}; got"
        f" compressionQuality={quality!r} codec={codec!r}",
    )


def _build_upscale_seamless(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the seamless (tessellated or looping) upscaler body.

    Same checkpoint and same top-level parameters as the plain upscaler -
    including the 4k resolution its registry entry completes - plus
    ``experiments.seamless``.

    Args:
        capability: The seamless upscale registry entry.
        request: The caller's request.

    Returns:
        The request body.

    Raises:
        VpePayloadError: If seamless output was asked for in a delivery
            format that cannot carry it.
    """
    _check_seamless_delivery(request)
    payload = _build_upscale(capability, request)
    payload["parameters"]["experiments"]["seamless"] = _seamless(request)
    return payload


def _audio_control_block(control: VpeAudioControl) -> dict[str, Any]:
    """Renders ``experiments.customParameters.audioControl``."""
    if control.target_audio is not None:
        return {
            "targetAudio": {
                _KEY_GCS_URI: control.target_audio.gcs_uri,
                _KEY_MIME_TYPE: control.target_audio.mime_type.lower(),
            }
        }
    return {
        "useTargetAudioFromVideo": bool(control.use_target_audio_from_video)
    }


def _omni_video_transform_block(
    transform: VpeOmniVideoTransform,
) -> dict[str, Any]:
    """Renders ``experiments.customParameters.videoTransform``."""
    block: dict[str, Any] = {
        "initializationVideo": {
            _KEY_GCS_URI: transform.initialization_video.gcs_uri,
            _KEY_MIME_TYPE: transform.initialization_video.mime_type.lower(),
        }
    }
    if transform.noise_strength is not None:
        block["noiseStrength"] = float(transform.noise_strength)
    if transform.mask is not None:
        block["mask"] = {
            _KEY_GCS_URI: transform.mask.gcs_uri,
            _KEY_MIME_TYPE: transform.mask.mime_type.lower(),
        }
    return block


def _build_super_res(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the ``veo-exp-super-res`` request body.

    Enforces GMP Super Resolution parameter rules:
    - ``customParameters.superResConfig.mode`` is required.
    - ``fps`` is accepted only for PNG/EXR image sequences, omitted for video
      containers.
    - ``codec`` must be omitted when ``compressionQuality`` is
      ``lossless_16bit_png`` or the input is EXR.
    - ``exrColorSpaceOverride`` is valid only for EXR inputs.
    - At ``MODE_8K``, ``lossless_16bit_png`` and ``dnxhr`` + ``lossless`` are
      disallowed.
    """
    if request.super_res_mode is None:
        raise VpePayloadError(
            "super_res requires"
            " parameters.experiments.customParameters.superResConfig.mode",
        )
    mode = _enum_value(request.super_res_mode)
    quality = _enum_value(request.compression_quality)
    codec = _enum_value(request.codec)
    video_mime = request.video.mime_type.lower()
    is_exr = video_mime == MIME_EXR
    is_sequence = (
        video_mime in (MIME_PNG, MIME_EXR) or request.video.is_sequence
    )

    if request.fps is not None and not is_sequence:
        raise VpePayloadError(
            "super_res parameters.fps is only supported for image sequences;"
            " omit fps for video containers",
        )
    if codec is not None and (
        quality == VpeCompressionQuality.LOSSLESS_16BIT_PNG.value or is_exr
    ):
        raise VpePayloadError(
            "super_res experiments.codec must be omitted for"
            " lossless_16bit_png or EXR",
        )
    if request.exr_color_space_override is not None and not is_exr:
        raise VpePayloadError(
            "super_res customParameters.exrColorSpaceOverride is only"
            " supported for EXR inputs",
        )
    if mode == VpeSuperResMode.MODE_8K.value:
        if quality == VpeCompressionQuality.LOSSLESS_16BIT_PNG.value:
            raise VpePayloadError(
                "super_res MODE_8K does not support lossless_16bit_png",
            )
        if (
            codec == VpeCodec.DNXHR.value
            and quality == VpeCompressionQuality.LOSSLESS.value
        ):
            raise VpePayloadError(
                "super_res MODE_8K does not support lossless DNxHR",
            )

    instance, parameters, experiments = _start(capability, request)
    instance[VpeInstanceField.VIDEO.value] = _media(
        capability,
        VpeInstanceField.VIDEO,
        request.video,
    )
    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(parameters, "sharpness", request.sharpness)
    _set_if(parameters, "fps", request.fps)
    _set_if(parameters, "seed", request.seed)
    _set_if(experiments, "codec", request.codec)

    super_res_config: dict[str, Any] = {"mode": mode}
    _set_if(super_res_config, "steps", request.super_res_steps)
    custom_params: dict[str, Any] = {"superResConfig": super_res_config}
    _set_if(
        custom_params,
        "exrColorSpaceOverride",
        request.exr_color_space_override,
    )
    experiments["customParameters"] = custom_params
    return _finish(instance, parameters)


def _check_exr_references_have_an_exr_video(request: VpeRequest) -> None:
    """Rejects EXR reference images without an EXR input video."""
    has_exr_reference = any(
        ref.mime_type.lower() == MIME_EXR for ref in request.reference_images
    )
    if not has_exr_reference:
        return
    has_exr_video = (
        request.video is not None
        and request.video.mime_type.lower() == MIME_EXR
    ) or (
        request.omni_video_transform is not None
        and request.omni_video_transform.initialization_video.mime_type.lower()
        == MIME_EXR
    )
    if not has_exr_video:
        raise VpePayloadError(
            "omni-cine EXR reference images require an EXR input video",
        )


def _build_omni_cine(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the Omni-Cine body."""
    _check_exr_references_have_an_exr_video(request)
    if request.last_frame is not None and request.image is None:
        raise VpePayloadError(
            "omni-cine instances[].lastFrame requires instances[].image",
        )
    if request.reference_audios and request.audio_control is not None:
        raise VpePayloadError(
            "omni-cine instances[].referenceAudios and"
            " customParameters.audioControl are mutually exclusive",
        )

    instance, parameters, experiments = _start(capability, request)
    instance[VpeInstanceField.PROMPT.value] = request.prompt
    if request.video is not None:
        instance[VpeInstanceField.VIDEO.value] = _media(
            capability,
            VpeInstanceField.VIDEO,
            request.video,
        )
    if request.image is not None:
        instance[VpeInstanceField.IMAGE.value] = _media(
            capability,
            VpeInstanceField.IMAGE,
            request.image,
        )
    if request.last_frame is not None:
        instance[VpeInstanceField.LAST_FRAME.value] = _media(
            capability,
            VpeInstanceField.LAST_FRAME,
            request.last_frame,
        )
    if request.reference_images:
        instance[VpeInstanceField.REFERENCE_IMAGES.value] = _reference_images(
            capability,
            request.reference_images,
        )
    if request.reference_audios:
        instance[VpeInstanceField.REFERENCE_AUDIOS.value] = _reference_audios(
            capability,
            request.reference_audios,
        )

    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(experiments, "codec", request.codec)

    custom_params: dict[str, Any] = {}
    _set_if(
        custom_params,
        "exrColorSpaceOverride",
        request.exr_color_space_override,
    )
    if request.omni_video_transform is not None:
        custom_params["videoTransform"] = _omni_video_transform_block(
            request.omni_video_transform
        )
    if request.audio_control is not None:
        custom_params["audioControl"] = _audio_control_block(
            request.audio_control
        )
    if custom_params:
        experiments["customParameters"] = custom_params

    return _finish(instance, parameters)


def _build_omni_perf(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the Omni-Perf body."""
    instance, parameters, experiments = _start(capability, request)
    instance[VpeInstanceField.PROMPT.value] = request.prompt
    instance[VpeInstanceField.VIDEO.value] = _media(
        capability,
        VpeInstanceField.VIDEO,
        request.video,
    )
    instance[VpeInstanceField.REFERENCE_IMAGES.value] = _reference_images(
        capability,
        request.reference_images,
    )
    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(experiments, "codec", request.codec)
    if request.audio_control is not None:
        experiments["customParameters"] = {
            "audioControl": _audio_control_block(request.audio_control)
        }
    return _finish(instance, parameters)


def _build_video_transform(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the video transform body, in all three of its modes.

    The strength and step count are documented under *Instances* but every
    sample sends them inside ``experiments``, alongside the mask URI and
    the keyframe array; only ``seed``, ``storageUri`` and
    ``compressionQuality`` stay at the top level. Neither strength nor
    steps is forced in: the keyframe-interpolation sample omits both and
    the professional-formats samples omit the steps, so requiring them
    would make documented requests unbuildable.

    Args:
        capability: The video transform registry entry.
        request: The caller's request.

    Returns:
        The request body.

    Raises:
        VpePayloadError: If the request conditions on nothing but a prompt,
            or masks an input video it does not supply.
    """
    # video is documented Required, and the single sample that leaves it out
    # conditions on instances[].image instead. A prompt on its own is
    # text-to-video, which this checkpoint does not offer, so it would be
    # accepted here and then rejected by the API.
    if request.video is None and request.image is None:
        raise VpePayloadError(
            "Video transform needs an input video or a first-frame image",
        )
    # Masked transform is listed as a V2V mode and the mask is documented as
    # "a grayscale video matching the input video's resolution and duration".
    # Sent alongside a first frame and no video it masks nothing, so it would
    # be ignored and the caller would get an unmasked whole-frame edit.
    if request.video_transform_mask_gcs_uri and request.video is None:
        raise VpePayloadError(
            "videoTransformMaskGcsUri masks an input video, so"
            " instances[].video is required with it",
        )
    instance, parameters, experiments = _start(capability, request)
    instance[VpeInstanceField.PROMPT.value] = request.prompt
    if request.image is not None:
        instance[VpeInstanceField.IMAGE.value] = _media(
            capability,
            VpeInstanceField.IMAGE,
            request.image,
        )
    if request.video is not None:
        instance[VpeInstanceField.VIDEO.value] = _media(
            capability,
            VpeInstanceField.VIDEO,
            request.video,
        )
    if request.last_frame is not None:
        instance[VpeInstanceField.LAST_FRAME.value] = _media(
            capability,
            VpeInstanceField.LAST_FRAME,
            request.last_frame,
        )
    _set_if(parameters, "seed", request.seed)
    _set_if(parameters, "compressionQuality", request.compression_quality)
    if request.video is not None:
        _set_if(
            experiments,
            "videoTransformStrength",
            request.video_transform_strength,
        )
    _set_if(experiments, "numDiffusionSteps", request.num_diffusion_steps)
    _set_if(experiments, "codec", request.codec)
    _set_if(
        experiments,
        "videoTransformMaskGcsUri",
        request.video_transform_mask_gcs_uri,
    )
    if request.conditioning_frames:
        experiments["conditioningFrames"] = _conditioning_frames(
            capability,
            request.conditioning_frames,
        )
    return _finish(instance, parameters)


def _build_perf_estimation(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the performance estimation (blue mesh) body.

    Args:
        capability: The perf-estimation registry entry.
        request: The caller's request.

    Returns:
        The request body.
    """
    instance, parameters, _ = _start(capability, request)
    instance[VpeInstanceField.VIDEO.value] = _media(
        capability,
        VpeInstanceField.VIDEO,
        request.video,
    )
    _set_if(parameters, "seed", request.seed)
    return _finish(instance, parameters)


def _build_perf_generation(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the performance generation body.

    The blue mesh arrives as ``experiments.perfMeshGcsUri``, not as
    ``instances[].video`` - this model has no video instance field at all.

    Args:
        capability: The perf-generation registry entry.
        request: The caller's request.

    Returns:
        The request body.

    Raises:
        VpePayloadError: If no blue mesh was supplied.
    """
    instance, parameters, experiments = _start(capability, request)
    if not request.perf_mesh_gcs_uri:
        raise VpePayloadError(
            "Performance generation requires experiments.perfMeshGcsUri",
        )
    instance[VpeInstanceField.PROMPT.value] = request.prompt
    instance[VpeInstanceField.REFERENCE_IMAGES.value] = _reference_images(
        capability,
        request.reference_images,
    )
    experiments["perfMeshGcsUri"] = request.perf_mesh_gcs_uri
    _set_if(parameters, "seed", request.seed)
    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(experiments, "codec", request.codec)
    return _finish(instance, parameters)


def _build_dialogue_driven(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the dialogue-driven (audio-to-video) body.

    This is the one capability whose stills and audio may be sent inline as
    base64 instead of from Cloud Storage.

    Args:
        capability: The dialogue-driven registry entry.
        request: The caller's request.

    Returns:
        The request body.
    """
    instance, parameters, experiments = _start(capability, request)
    instance[VpeInstanceField.PROMPT.value] = request.prompt
    instance[VpeInstanceField.IMAGE.value] = _media(
        capability,
        VpeInstanceField.IMAGE,
        request.image,
    )
    instance[VpeInstanceField.REFERENCE_AUDIOS.value] = _reference_audios(
        capability,
        request.reference_audios,
    )
    _set_if(parameters, "seed", request.seed)
    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(experiments, "codec", request.codec)
    return _finish(instance, parameters)


def _build_video_textures(
    capability: VpeCapability,
    request: VpeRequest,
) -> dict[str, Any]:
    """Builds the video textures body.

    Args:
        capability: The video textures registry entry.
        request: The caller's request.

    Returns:
        The request body.
    """
    instance, parameters, experiments = _start(capability, request)
    if not _is_absent(request.prompt):
        instance[VpeInstanceField.PROMPT.value] = request.prompt
    if request.image is not None:
        instance[VpeInstanceField.IMAGE.value] = _media(
            capability,
            VpeInstanceField.IMAGE,
            request.image,
        )
    if request.last_frame is not None:
        instance[VpeInstanceField.LAST_FRAME.value] = _media(
            capability,
            VpeInstanceField.LAST_FRAME,
            request.last_frame,
        )
    if not instance:
        raise VpePayloadError(
            "Video textures needs a prompt, a conditioning image, or both",
        )
    experiments["seamless"] = _seamless(request)
    _set_if(parameters, "seed", request.seed)
    _set_if(parameters, "compressionQuality", request.compression_quality)
    _set_if(experiments, "codec", request.codec)
    return _finish(instance, parameters)


_Builder = Callable[[VpeCapability, VpeRequest], dict[str, Any]]


def _build_builders() -> Mapping[VpeCapabilityId, _Builder]:
    """Indexes the builders by capability id.

    Returns:
        A read-only mapping covering every capability id.

    Raises:
        ValueError: If a capability has no builder.
    """
    builders: dict[VpeCapabilityId, _Builder] = {
        VpeCapabilityId.UPSCALE: _build_upscale,
        VpeCapabilityId.UPSCALE_SEAMLESS: _build_upscale_seamless,
        VpeCapabilityId.SUPER_RES: _build_super_res,
        VpeCapabilityId.OMNI_CINE: _build_omni_cine,
        VpeCapabilityId.VIDEO_TRANSFORM: _build_video_transform,
        VpeCapabilityId.PERF_ESTIMATION: _build_perf_estimation,
        VpeCapabilityId.OMNI_PERF: _build_omni_perf,
        VpeCapabilityId.PERF_GENERATION: _build_perf_generation,
        VpeCapabilityId.DIALOGUE_DRIVEN: _build_dialogue_driven,
        VpeCapabilityId.VIDEO_TEXTURES: _build_video_textures,
    }
    missing = set(VpeCapabilityId) - set(builders)
    if missing:
        raise ValueError(
            "Capability ids without a payload builder: "
            + ", ".join(sorted(item.value for item in missing)),
        )
    return MappingProxyType(builders)


_BUILDERS: Mapping[VpeCapabilityId, _Builder] = _build_builders()


def _check_instances(
    capability: VpeCapability,
    instances: Any,
) -> None:
    """Checks the built instances array against the registry.

    Args:
        capability: The registry entry that was built for.
        instances: The emitted instances array.

    Raises:
        VpePayloadError: If the array is not a single object, or a key is
            not an instance field this capability declares.
    """
    if not isinstance(instances, list) or len(instances) != 1:
        raise VpePayloadError(
            "A VPE request carries exactly one instance",
        )
    for key in instances[0]:
        try:
            field = VpeInstanceField(key)
        except ValueError as error:
            raise VpePayloadError(
                f"{capability.capability_id.value} emitted unknown instance"
                f" field {key!r}",
            ) from error
        if not capability.supports(field):
            raise VpePayloadError(
                f"{capability.capability_id.value} emitted"
                f" instances[].{key}, which it does not declare",
            )


def _check_parameters(
    capability: VpeCapability,
    block: Any,
    location: VpeParameterLocation,
) -> None:
    """Checks one parameter block, descending into nested blocks.

    Args:
        capability: The registry entry that was built for.
        block: The emitted mapping at this nesting level.
        location: The level ``block`` sits at.

    Raises:
        VpePayloadError: If a key is not declared at this level. That
            includes a parameter emitted at the wrong depth, since the
            lookup is by name *and* location.
    """
    if not isinstance(block, dict):
        raise VpePayloadError(f"{location.value} must be an object")
    for key, value in block.items():
        nested = _LOCATIONS_BY_PATH.get(f"{location.value}.{key}")
        if nested is not None and isinstance(value, dict):
            _check_parameters(capability, value, nested)
            continue
        if capability.parameter(key, location) is None:
            raise VpePayloadError(
                f"{capability.capability_id.value} emitted"
                f" {location.value}.{key}, which it does not declare",
            )


def build_payload(request: VpeRequest) -> dict[str, Any]:
    """Builds the ``:predictLongRunning`` body for one VPE request.

    Args:
        request: A request naming the capability and its inputs. Media is
            expected to have already been probed and conformed by
            preflight; only the payload contract is checked here.

    Returns:
        The JSON-serializable request body, ready to POST to
        ``.../publishers/google/models/veo-experimental:predictLongRunning``.

    Raises:
        VpePayloadError: If the request names inputs or parameters the
            capability does not accept, omits a required input, or carries
            a value outside its documented range.
        ValueError: If the capability id is not a known VPE capability.
    """
    capability = get_capability(request.capability_id)
    _check_instance_slots(capability, request)
    _check_scalar_slots(capability, request)
    _check_group_slots(capability, request)

    payload = _BUILDERS[capability.capability_id](capability, request)

    # The checks above cover what the caller asked for; this one covers what
    # the builder actually wrote, which is where a misspelled or misplaced
    # wire name would otherwise survive all the way to the API.
    _check_instances(capability, payload["instances"])
    _check_parameters(
        capability,
        payload["parameters"],
        VpeParameterLocation.PARAMETERS,
    )
    return payload
