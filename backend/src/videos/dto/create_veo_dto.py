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


from typing import Annotated, Literal

from fastapi import Query
from pydantic import Field, field_validator, model_validator

from src.common.base_dto import (
    AspectRatioEnum,
    AssetReferenceDto,
    BaseDto,
    ColorAndToneEnum,
    CompositionEnum,
    GenerationModelEnum,
    LightingEnum,
    ReferenceImageTypeEnum,
    StyleEnum,
)
from src.common.schema.media_item_model import (
    AssetRoleEnum,
    SourceMediaItemLink,
)

# Models served by the Gemini Omni Interactions API. Their capabilities differ
# substantially from Veo's, so several limits below are resolved per-model
# rather than as static field constraints.
#
# GEMINI_OMNI ("gemini-omni-generate-preview") is intentionally absent: it is
# not a real model. Vertex rejects it with "Unsupported model interaction". The
# enum member survives only so historical rows deserialize.
OMNI_1_0_MODELS = frozenset({GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW})
OMNI_1_1_MODELS = frozenset(
    {
        GenerationModelEnum.GEMINI_OMNI_1_1_FLASH,
        GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
    }
)
OMNI_1_2_MODELS = frozenset({GenerationModelEnum.GEMINI_OMNI_1_2_FLASH})
OMNI_1_1_PLUS_MODELS = OMNI_1_1_MODELS | OMNI_1_2_MODELS
OMNI_MODELS = OMNI_1_0_MODELS | OMNI_1_1_PLUS_MODELS

# Widest bounds accepted by any supported model. The real, per-model limits are
# enforced in CreateVeoDto.validate_cross_fields.
MAX_REFERENCE_IMAGES_ANY_MODEL = 10
MAX_DURATION_SECONDS_ANY_MODEL = 15

# Omni 1.0/1.1 accepts 3-10s; Omni 1.2 accepts 3-15s for text/start-frame
# generation and 3-10s when references, extension, or end-frame interpolation
# are present.
OMNI_DURATION_RANGE = (3, 10)
OMNI_1_2_DURATION_RANGE = (3, 15)

# Veo offers a fixed set of lengths rather than a range, so 5s and 7s are not
# valid even though they fall between the bounds.
VEO_DURATION_CHOICES = frozenset({4, 6, 8})

# Reference image limits per model family.
OMNI_1_2_MAX_REFERENCE_IMAGES = 10
OMNI_MAX_REFERENCE_IMAGES = 7
VEO_MAX_REFERENCE_IMAGES = 3
OMNI_MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_VIDEOS_ANY_MODEL = 3


class ReferenceImageDto(BaseDto):
    asset_id: int = Field(
        description="The ID of the SourceAsset to use as a reference.",
    )
    reference_type: ReferenceImageTypeEnum = Field(
        default=ReferenceImageTypeEnum.ASSET
    )


class ConditioningFrameDto(BaseDto):
    """An intermediate conditioning keyframe for multi-keyframe video transform."""

    frame_number: int = Field(
        ge=8,
        le=184,
        description="Frame position for conditioning (must be a multiple of 8, e.g. 8, 16, 24...)",
    )
    image_asset_id: AssetReferenceDto = Field(
        description="Asset reference for the conditioning keyframe image.",
    )


class CreateVeoDto(BaseDto):
    """The refactored request model. Defaults are defined here to make the API
    contract explicit and self-documenting.
    """

    prompt: Annotated[str, Query(max_length=10000)] = Field(
        description="Prompt term to be passed to the model",
    )
    workspace_id: int = Field(
        ge=1,
        description="The ID of the workspace for this generation.",
    )
    generation_model: GenerationModelEnum = Field(
        default=GenerationModelEnum.VEO_3_1_GENERATE_001,
        description="Model used for image generation.",
    )
    aspect_ratio: AspectRatioEnum = Field(
        default=AspectRatioEnum.RATIO_16_9,
        description="Aspect ratio of the image.",
    )
    number_of_media: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Number of videos to generate (between 1 and 4).",
    )
    style: StyleEnum | None = Field(
        default=None, description="Style of the image."
    )
    negative_prompt: str = Field(
        default="",
        description="Negative prompt for the image.",
    )
    color_and_tone: ColorAndToneEnum | None = Field(
        default=None,
        description="The desired color and tone style for the image.",
    )
    lighting: LightingEnum | None = Field(
        default=None,
        description="The desired lighting style for the image.",
    )
    composition: CompositionEnum | None = Field(
        default=None,
        description="The desired lighting style for the image.",
    )
    generate_audio: bool = Field(
        default=False,
        description="Whether to add audio to the generated video.",
    )
    duration_seconds: int = Field(
        default=8,
        ge=1,
        le=MAX_DURATION_SECONDS_ANY_MODEL,
        description=(
            "Duration in seconds for the videos to generate. The accepted "
            "range depends on the model: Veo allows 4/6/8, Omni 1.0/1.1 "
            "allows 3-10, and Omni 1.2 allows 3-15 (3-10 with references)."
        ),
    )
    start_image_asset_id: AssetReferenceDto | None = Field(
        default=None,
        description="Object containing ID and type of asset to use as the starting image.",
    )
    end_image_asset_id: AssetReferenceDto | None = Field(
        default=None,
        description="Object containing ID and type of asset to use as the ending image.",
    )
    source_video_asset_id: AssetReferenceDto | None = Field(
        default=None,
        description="Object containing ID and type of asset to use as the source video.",
    )
    source_media_items: list[SourceMediaItemLink] | None = Field(
        default=None,
        description="A list of previously generated media items (from the gallery) to be used as inputs (e.g., start/end frames).",
    )
    use_brand_guidelines: bool = Field(
        default=False,
        description="Whether to prepend brand guidelines to the prompt.",
    )
    enhance_prompt: bool = Field(
        default=False,
        description="Whether to enhance the prompt using Gemini.",
    )
    reference_images: list[ReferenceImageDto] | None = Field(
        default=None,
        max_length=MAX_REFERENCE_IMAGES_ANY_MODEL,
        description=(
            "A list of reference images, each with an ID and a type (ASSET or "
            "STYLE). The maximum count depends on the model: Veo allows 3, "
            "Gemini Omni 1.0/1.1 allows 7, and Gemini Omni 1.2 allows 10."
        ),
    )
    reference_video: AssetReferenceDto | None = Field(
        default=None,
        description="Object containing ID and type of asset to use as a reference video.",
    )
    reference_videos: list[AssetReferenceDto] | None = Field(
        default=None,
        max_length=MAX_REFERENCE_VIDEOS_ANY_MODEL,
        description="List of reference videos to steer generation.",
    )
    reference_audio: AssetReferenceDto | None = Field(
        default=None,
        description="Object containing ID and type of asset to use as a reference audio.",
    )
    edit_source: AssetReferenceDto | None = Field(
        default=None,
        description=(
            "A video to modify. Unlike parent_media_item_id, which continues "
            "an existing conversation, this starts a fresh edit of any clip, "
            "including uploaded footage. Gemini Omni only."
        ),
    )
    strip_source_audio: bool = Field(
        default=True,
        description=(
            "Remove the audio track from the clip being edited before sending "
            "it. Omni refuses to edit a clip containing speech when reference "
            "images are also supplied, and its own output always has audio, so "
            "this defaults on. Turn it off to preserve the original audio when "
            "editing without references."
        ),
    )
    parent_media_item_id: int | None = Field(
        default=None,
        description="The ID of the parent media item for multi-turn conversation editing.",
    )
    parent_media_index: int = Field(
        default=0,
        ge=0,
        description=(
            "Which video within the parent media item to continue editing. A "
            "single job can produce several clips, each with its own "
            "interaction, so the index selects which conversation to resume."
        ),
    )
    resolution: Literal["1K", "2K", "4K", "360p", "720p", "1080p"] = Field(
        default="1K",
        description="Resolution of the generated videos.",
    )
    video_transform_strength: float = Field(
        default=0.5,
        ge=0.01,
        le=1.0,
        description="Restyle intensity for video transform (0.1 to 1.0).",
    )
    num_diffusion_steps: int = Field(
        default=20,
        ge=1,
        le=250,
        description="Diffusion step count for video transform.",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=4294967295,
        description="Random seed for deterministic generation (0 to 4294967295).",
    )
    video_transform_mask_asset_id: AssetReferenceDto | None = Field(
        default=None,
        description="Optional grayscale mask video asset for localized inpainting.",
    )
    conditioning_frames: list[ConditioningFrameDto] | None = Field(
        default=None,
        description="Optional intermediate conditioning keyframes (at multiples of 8 frames) for keyframe-guided video transform.",
    )
    output_codec: Literal["h264", "prores", "dnxhr"] = Field(
        default="h264",
        description="GMP output container/codec: h264, prores (.mov), or dnxhr (.mxf).",
    )
    compression_quality: Literal["optimized", "lossless"] = Field(
        default="optimized",
        description="GMP compression quality profile: optimized or lossless.",
    )
    audio_control_mode: (
        Literal[
            "generate",
            "preserve_video_audio",
            "target_audio",
            "reference_audio",
        ]
        | None
    ) = Field(
        default=None,
        description="Optional GMP audio steering mode for Omni-Perf and Omni-Cine.",
    )
    is_blue_mesh_input: bool = Field(
        default=False,
        description="True when the driving video for Omni-Perf is already a blue-mesh clip.",
    )
    seamless_loop: bool = Field(
        default=False,
        description="Enable temporal seamless looping for Video Textures.",
    )
    tessellate_horizontal: bool = Field(
        default=False,
        description="Enable horizontal spatial tiling for Video Textures.",
    )
    tessellate_vertical: bool = Field(
        default=False,
        description="Enable vertical spatial tiling for Video Textures.",
    )

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "CreateVeoDto":
        """Performs cross-field model, reference, resolution, and duration validations."""
        if (
            self.compression_quality == "lossless"
            and self.output_codec == "h264"
        ):
            raise ValueError(
                "Lossless compressionQuality requires output_codec 'prores' or"
                " 'dnxhr'."
            )

        if self.reference_images:
            seen_imgs: set[tuple[int, str]] = set()
            deduped_imgs: list[ReferenceImageDto] = []
            for img in self.reference_images:
                key = (
                    img.asset_id,
                    getattr(
                        img.reference_type, "value", str(img.reference_type)
                    ),
                )
                if key not in seen_imgs:
                    seen_imgs.add(key)
                    deduped_imgs.append(img)
            self.reference_images = deduped_imgs

        if self.reference_videos:
            seen_vids: set[tuple[str, int, int]] = set()
            deduped_vids: list[AssetReferenceDto] = []
            for vid in self.reference_videos:
                key = (vid.type, vid.id, vid.index or 0)
                if key not in seen_vids:
                    seen_vids.add(key)
                    deduped_vids.append(vid)
            self.reference_videos = deduped_vids

        conflicting_roles_present = False
        start_frame_role_present = False
        end_frame_role_present = False
        reference_roles_present = False
        image_reference_role_count = 0
        video_reference_role_count = 0
        audio_reference_role_present = False
        video_role_present = False
        model = self.generation_model

        if self.source_media_items:
            non_reference_roles = {
                AssetRoleEnum.START_FRAME,
                AssetRoleEnum.END_FRAME,
                AssetRoleEnum.VIDEO_EXTENSION_SOURCE,
                AssetRoleEnum.EDIT_SOURCE,
            }
            reference_roles = {
                AssetRoleEnum.IMAGE_REFERENCE_ASSET,
                AssetRoleEnum.IMAGE_REFERENCE_STYLE,
                AssetRoleEnum.VIDEO_REFERENCE,
                AssetRoleEnum.AUDIO_REFERENCE,
            }
            valid_roles = non_reference_roles.union(reference_roles)

            for item in self.source_media_items:
                if item.role not in valid_roles:
                    raise ValueError(
                        f"Invalid role '{item.role}' for source_media_item.",
                    )
                if item.role == AssetRoleEnum.START_FRAME:
                    start_frame_role_present = True
                elif item.role in {
                    AssetRoleEnum.END_FRAME,
                    AssetRoleEnum.VIDEO_EXTENSION_SOURCE,
                }:
                    conflicting_roles_present = True
                if item.role == AssetRoleEnum.END_FRAME:
                    end_frame_role_present = True
                if item.role in reference_roles:
                    reference_roles_present = True
                if item.role in {
                    AssetRoleEnum.IMAGE_REFERENCE_ASSET,
                    AssetRoleEnum.IMAGE_REFERENCE_STYLE,
                }:
                    image_reference_role_count += 1
                if item.role == AssetRoleEnum.VIDEO_REFERENCE:
                    video_reference_role_count += 1
                if item.role == AssetRoleEnum.AUDIO_REFERENCE:
                    audio_reference_role_present = True
                if item.role in {
                    AssetRoleEnum.VIDEO_EXTENSION_SOURCE,
                    AssetRoleEnum.VIDEO_REFERENCE,
                    AssetRoleEnum.EDIT_SOURCE,
                }:
                    video_role_present = True

        start_image_present = bool(self.start_image_asset_id)
        end_image_present = bool(self.end_image_asset_id)
        source_video_present = bool(self.source_video_asset_id)
        total_ref_images = (
            len(self.reference_images) if self.reference_images else 0
        ) + image_reference_role_count
        has_audio_ref = bool(
            self.reference_audio or audio_reference_role_present
        )

        if model == GenerationModelEnum.VEO_EXP_PERF_ESTIMATION:
            has_video = bool(
                self.edit_source
                or source_video_present
                or self.reference_video
                or video_role_present
            )
            if not has_video:
                raise ValueError(
                    "Performance estimation requires an input video"
                    " (edit_source or video source_media_items).",
                )
            if self.duration_seconds and self.duration_seconds > 10:
                raise ValueError(
                    "Performance estimation supports up to 10 seconds, got"
                    f" {self.duration_seconds}.",
                )
            return self

        if model == GenerationModelEnum.OMNI_PERF:
            if not self.prompt or not self.prompt.strip():
                raise ValueError("Omni-Perf requires a non-empty prompt.")
            if total_ref_images < 1 or total_ref_images > 5:
                raise ValueError(
                    "Omni-Perf requires between 1 and 5 reference images, got"
                    f" {total_ref_images}.",
                )
            has_driving_video = bool(
                self.reference_video
                or self.reference_videos
                or self.edit_source
                or source_video_present
                or video_role_present
            )
            if not has_driving_video:
                raise ValueError(
                    "Omni-Perf requires a driving performance video."
                )
            if self.duration_seconds and self.duration_seconds > 10:
                raise ValueError(
                    "Omni-Perf supports up to 10 seconds, got"
                    f" {self.duration_seconds}.",
                )
            if self.audio_control_mode == "target_audio" and not has_audio_ref:
                raise ValueError(
                    "Omni-Perf audio_control_mode 'target_audio' requires"
                    " reference_audio.",
                )
            return self

        if model == GenerationModelEnum.OMNI_CINE:
            if not self.prompt or not self.prompt.strip():
                raise ValueError("Omni-Cine requires a non-empty prompt.")
            if total_ref_images > 7:
                raise ValueError(
                    "Omni-Cine supports at most 7 reference images, got"
                    f" {total_ref_images}.",
                )
            if self.duration_seconds and self.duration_seconds > 10:
                raise ValueError(
                    "Omni-Cine supports up to 10 seconds, got"
                    f" {self.duration_seconds}.",
                )
            has_start = bool(start_image_present or start_frame_role_present)
            has_end = bool(end_image_present or end_frame_role_present)
            if has_end and not has_start:
                raise ValueError(
                    "Omni-Cine lastFrame (end_image_asset_id) requires image"
                    " (start_image_asset_id).",
                )
            if (
                self.audio_control_mode in ("target_audio", "reference_audio")
                and not has_audio_ref
            ):
                raise ValueError(
                    f"Omni-Cine audio_control_mode '{self.audio_control_mode}'"
                    " requires reference_audio.",
                )
            return self

        if model == GenerationModelEnum.VEO_EXP_VIDEO_TEXTURES:
            if not (
                self.seamless_loop
                or self.tessellate_horizontal
                or self.tessellate_vertical
            ):
                raise ValueError(
                    "Video textures requires at least one of seamless_loop,"
                    " tessellate_horizontal, or tessellate_vertical to be True."
                )
            has_any_image = bool(
                start_image_present
                or end_image_present
                or start_frame_role_present
                or end_frame_role_present
            )
            if self.seamless_loop and has_any_image:
                raise ValueError(
                    "Simultaneous looping and image conditioning is not"
                    " supported",
                )
            if (not self.prompt or not self.prompt.strip()) and not (
                start_image_present or start_frame_role_present
            ):
                raise ValueError(
                    "Video textures requires a prompt or a conditioning image."
                )
            if self.duration_seconds and self.duration_seconds > 8:
                raise ValueError(
                    "Video textures supports up to 8 seconds, got"
                    f" {self.duration_seconds}.",
                )
            return self

        if model == GenerationModelEnum.VEO_EXP_VIDEO_TRANSFORM:
            has_source_video = bool(
                source_video_present
                or self.edit_source
                or video_role_present
                or start_image_present
                or start_frame_role_present
            )
            if not has_source_video:
                raise ValueError(
                    "Video transform model requires an input video (or first frame image).",
                )
            if self.resolution not in {"1K", "720p"}:
                raise ValueError(
                    f"Video transform model only supports 1K resolution (720p), got '{self.resolution}'.",
                )
            if self.duration_seconds and self.duration_seconds > 8:
                raise ValueError(
                    f"Video transform model supports up to 8 seconds, got {self.duration_seconds}.",
                )
            return self

        if model == GenerationModelEnum.VEO_EXP_A2V_GENERATION:
            if (
                end_image_present
                or source_video_present
                or conflicting_roles_present
                or self.edit_source
                or self.reference_video
                or self.reference_videos
                or video_reference_role_count > 0
            ):
                raise ValueError(
                    "Dialogue-driven model only supports an image input and an audio reference track.",
                )
            if not (
                start_image_present
                or start_frame_role_present
                or self.reference_images
                or reference_roles_present
            ):
                raise ValueError(
                    "Dialogue-driven model requires a character image (reference image or start frame).",
                )
            if not self.reference_audio:
                raise ValueError(
                    "Dialogue-driven model requires a reference audio track.",
                )
            if self.duration_seconds != 8:
                raise ValueError(
                    f"Dialogue-driven model only supports an 8-second duration, got {self.duration_seconds}.",
                )
            if self.resolution not in {"1K", "720p"}:
                raise ValueError(
                    f"Dialogue-driven model only supports 1K resolution (720p), got '{self.resolution}'.",
                )
            return self

        has_asset_references = (
            bool(self.reference_images)
            or bool(self.reference_video)
            or bool(self.reference_videos)
            or bool(self.reference_audio)
        )
        has_any_references = has_asset_references or reference_roles_present

        if has_any_references:
            supported_reference_models = {
                GenerationModelEnum.VEO_3_1_PREVIEW,
                GenerationModelEnum.VEO_3_1_GENERATE_001,
                GenerationModelEnum.VEO_3_1_LITE_GENERATE_001,
                GenerationModelEnum.VEO_3_1_LITE_PREVIEW,
                GenerationModelEnum.VEO_3_1_FAST_GENERATE_001,
                GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
                GenerationModelEnum.GEMINI_OMNI_1_1_FLASH,
                GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
                GenerationModelEnum.GEMINI_OMNI_1_2_FLASH,
            }
            if model not in supported_reference_models:
                supported = ", ".join(
                    sorted(m.value for m in supported_reference_models)
                )
                raise ValueError(
                    "Reference images/media are only supported by these "
                    f"models: {supported}.",
                )

            if (
                self.reference_video
                or self.reference_videos
                or video_reference_role_count > 0
            ) and model not in OMNI_1_1_PLUS_MODELS:
                raise ValueError(
                    f"Model '{model.value}' does not support video references. "
                    f"Use '{GenerationModelEnum.GEMINI_OMNI_1_2_FLASH.value}'.",
                )

            references_conflict = (
                end_image_present
                or source_video_present
                or conflicting_roles_present
                or (
                    (start_image_present or start_frame_role_present)
                    and model not in OMNI_MODELS
                )
            )

            if references_conflict:
                raise ValueError(
                    "Reference media cannot be used at the same time as a start frame, end frame, or source video.",
                )

        # Validate model-specific resolution limits
        is_omni_1_0 = model in OMNI_1_0_MODELS
        is_omni_1_1_plus = model in OMNI_1_1_PLUS_MODELS
        is_omni_1_2 = model in OMNI_1_2_MODELS
        is_omni = is_omni_1_0 or is_omni_1_1_plus

        if is_omni_1_0:
            allowed_resolutions = {"1K", "720p"}
        elif is_omni_1_1_plus:
            allowed_resolutions = {
                "1K",
                "2K",
                "4K",
                "360p",
                "720p",
                "1080p",
            }
        elif model in {
            GenerationModelEnum.VEO_3_1_LITE_GENERATE_001,
            GenerationModelEnum.VEO_3_1_LITE_PREVIEW,
        }:
            allowed_resolutions = {"1K", "2K", "720p", "1080p"}
        else:
            allowed_resolutions = {"1K", "2K", "4K", "720p", "1080p"}

        if self.resolution not in allowed_resolutions:
            raise ValueError(
                f"Model '{model.value}' does not support resolution '{self.resolution}'. "
                f"Supported resolutions: {sorted(list(allowed_resolutions))}"
            )

        # Reference image counts differ per model.
        if is_omni_1_2:
            max_reference_images = OMNI_1_2_MAX_REFERENCE_IMAGES
        elif is_omni:
            max_reference_images = OMNI_MAX_REFERENCE_IMAGES
        else:
            max_reference_images = VEO_MAX_REFERENCE_IMAGES

        if (
            self.reference_images
            and len(self.reference_images) > max_reference_images
        ):
            raise ValueError(
                f"Model '{model.value}' supports at most "
                f"{max_reference_images} reference images, "
                f"got {len(self.reference_images)}.",
            )

        max_reference_videos = (
            OMNI_MAX_REFERENCE_VIDEOS if is_omni_1_1_plus else 0
        )
        explicit_ref_videos_count = (
            len(self.reference_videos)
            if self.reference_videos
            else (1 if self.reference_video else 0)
        )
        total_ref_videos = (
            explicit_ref_videos_count + video_reference_role_count
        )
        if total_ref_videos > max_reference_videos:
            raise ValueError(
                f"Model '{model.value}' supports at most "
                f"{max_reference_videos} reference videos, "
                f"got {total_ref_videos}.",
            )

        # Duration bounds per model.
        if is_omni:
            if is_omni_1_2 and not (
                has_any_references
                or source_video_present
                or end_image_present
                or conflicting_roles_present
            ):
                min_duration, max_duration = OMNI_1_2_DURATION_RANGE
            else:
                min_duration, max_duration = OMNI_DURATION_RANGE
            if not min_duration <= self.duration_seconds <= max_duration:
                raise ValueError(
                    f"Model '{model.value}' supports durations between "
                    f"{min_duration} and {max_duration} seconds, "
                    f"got {self.duration_seconds}.",
                )
        elif self.duration_seconds not in VEO_DURATION_CHOICES:
            allowed = ", ".join(str(d) for d in sorted(VEO_DURATION_CHOICES))
            raise ValueError(
                f"Model '{model.value}' supports durations of {allowed} "
                f"seconds, got {self.duration_seconds}.",
            )

        if is_omni:
            self._validate_omni_unsupported_inputs(model)
        elif self.edit_source:
            raise ValueError(
                f"Model '{model.value}' does not support video editing. "
                f"Use '{GenerationModelEnum.GEMINI_OMNI_1_2_FLASH.value}'.",
            )

        if self.edit_source and self.start_image_asset_id:
            raise ValueError(
                "edit_source cannot be combined with a start frame: an edit "
                "modifies an existing clip rather than starting a new one.",
            )
        if self.edit_source and (
            self.reference_video
            or self.reference_videos
            or video_reference_role_count > 0
        ):
            raise ValueError(
                "edit_source cannot be combined with reference videos: the "
                "model cannot reason across two video sources in one request.",
            )

        return self

    def _validate_omni_unsupported_inputs(
        self,
        model: GenerationModelEnum,
    ) -> None:
        """Rejects inputs Gemini Omni accepts in its schema but cannot process."""
        unsupported: list[str] = []

        use_veo = "use a Veo model instead"
        extension = "video extension"
        interpolation = "first+last frame interpolation"

        if model in OMNI_1_0_MODELS:
            if self.source_video_asset_id:
                unsupported.append(
                    f"{extension} (source_video_asset_id) — {use_veo}",
                )

            if self.end_image_asset_id:
                unsupported.append(
                    f"{interpolation} (end_image_asset_id) — {use_veo}",
                )

            if self.reference_video or self.reference_videos:
                unsupported.append(
                    "video references (reference_video/reference_videos) — "
                    "use Gemini Omni 1.2 Flash instead",
                )

            if self.source_media_items:
                roles = {item.role for item in self.source_media_items}
                if AssetRoleEnum.VIDEO_EXTENSION_SOURCE in roles:
                    unsupported.append(
                        f"{extension} (video_extension_source role) — {use_veo}",
                    )
                if AssetRoleEnum.END_FRAME in roles:
                    unsupported.append(
                        f"{interpolation} (end_frame role) — {use_veo}",
                    )
                if AssetRoleEnum.VIDEO_REFERENCE in roles:
                    unsupported.append(
                        "video references (video_reference role) — "
                        "use Gemini Omni 1.2 Flash instead",
                    )

        if self.reference_audio:
            unsupported.append(
                "audio references (reference_audio) — describe the "
                "audio in the prompt instead",
            )

        if unsupported:
            raise ValueError(
                f"Model '{model.value}' does not support: "
                + "; ".join(unsupported),
            )

    @field_validator("aspect_ratio")
    @classmethod
    def validate_video_aspect_ratio(
        cls, value: AspectRatioEnum
    ) -> AspectRatioEnum:
        """Ensures that only supported aspect ratios for video are used."""
        valid_video_ratios = [
            AspectRatioEnum.RATIO_16_9,
            AspectRatioEnum.RATIO_9_16,
        ]
        if value not in valid_video_ratios:
            raise ValueError(
                "Invalid aspect ratio for video. Only '16:9' and '9:16' are supported.",
            )
        return value

    @field_validator("generation_model")
    @classmethod
    def validate_video_generation_model(
        cls,
        value: GenerationModelEnum,
    ) -> GenerationModelEnum:
        """Ensures that only supported generation models for video are used."""
        valid_video_models = [
            GenerationModelEnum.GEMINI_OMNI_1_2_FLASH,
            GenerationModelEnum.GEMINI_OMNI_1_1_FLASH,
            GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
            GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            GenerationModelEnum.VEO_3_1_PREVIEW,
            GenerationModelEnum.VEO_3_1_GENERATE_001,
            GenerationModelEnum.VEO_3_1_LITE_GENERATE_001,
            GenerationModelEnum.VEO_3_1_LITE_PREVIEW,
            GenerationModelEnum.VEO_3_1_FAST_GENERATE_001,
            GenerationModelEnum.VEO_3_FAST,
            GenerationModelEnum.VEO_3_QUALITY,
            GenerationModelEnum.VEO_3_FAST_PREVIEW,
            GenerationModelEnum.VEO_3_QUALITY_PREVIEW,
            GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            GenerationModelEnum.VEO_EXP_VIDEO_TRANSFORM,
            GenerationModelEnum.VEO_EXP_PERF_ESTIMATION,
            GenerationModelEnum.OMNI_PERF,
            GenerationModelEnum.OMNI_CINE,
            GenerationModelEnum.VEO_EXP_VIDEO_TEXTURES,
        ]
        if value not in valid_video_models:
            raise ValueError("Invalid generation model for video.")
        return value
