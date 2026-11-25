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
"""Tests for Create Veo Dto."""

import pytest
from pydantic import ValidationError

from src.common.base_dto import (
    GenerationModelEnum,
    ReferenceImageTypeEnum,
)
from src.common.schema.media_item_model import SourceMediaItemLink
from src.videos.dto.create_veo_dto import CreateVeoDto, ReferenceImageDto


def test_create_veo_dto_valid():
    dto = CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_QUALITY,
        aspect_ratio="16:9",
    )
    assert dto.prompt == "Test"


def test_validate_video_aspect_ratio_error():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test", workspace_id=1, aspect_ratio="1:1"
        )  # Invalid
    assert "Invalid aspect ratio for video" in str(exc_info.value)


def test_validate_source_media_items_invalid_role():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            source_media_items=[
                SourceMediaItemLink(
                    media_item_id=1,
                    media_index=0,
                    role="invalid_role",
                ),
            ],
        )
    # Pydantic validation error or enum validation error
    assert "invalid_role" in str(exc_info.value)


def test_validate_source_media_items_model_conflict():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_3_QUALITY,
            reference_images=[
                ReferenceImageDto(
                    asset_id=1,
                    reference_type=ReferenceImageTypeEnum.ASSET,
                ),
            ],
            source_media_items=[],  # Force validator to run
        )
    assert "Reference images/media are only supported by" in str(exc_info.value)


def test_validate_source_media_items_conflicting_inputs():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_3_1_PREVIEW,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            reference_images=[
                ReferenceImageDto(
                    asset_id=2,
                    reference_type=ReferenceImageTypeEnum.ASSET,
                ),
            ],
            source_media_items=[],  # Force validator to run
        )
    assert "Reference media cannot be used at the same time" in str(
        exc_info.value
    )


def test_omni_allows_start_frame_with_reference_images():
    """Anchoring a shot to an opening frame while holding character identities.

    Veo rejects an input image alongside reference images, but Omni has no
    typed reference field - they all ride the multimodal input - so the pairing
    is legal and is the core consistency workflow. Verified live against the
    API before relaxing this.
    """
    dto = CreateVeoDto(
        prompt="<IMAGE_REF_0> is the opening frame; keep <IMAGE_REF_1>'s face.",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        start_image_asset_id={"id": 1, "type": "source_asset"},
        reference_images=[
            ReferenceImageDto(
                asset_id=2,
                reference_type=ReferenceImageTypeEnum.ASSET,
            ),
        ],
        source_media_items=[],  # Force validator to run
    )
    assert dto.start_image_asset_id.id == 1
    assert len(dto.reference_images) == 1


def test_omni_allows_start_frame_role_with_reference_images():
    """The same pairing when the frame arrives as a media item, not an asset."""
    dto = CreateVeoDto(
        prompt="Anchor to the frame, keep the character.",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        reference_images=[
            ReferenceImageDto(
                asset_id=2,
                reference_type=ReferenceImageTypeEnum.ASSET,
            ),
        ],
        source_media_items=[
            SourceMediaItemLink(
                media_item_id=5,
                media_index=0,
                role="start_frame",
            ),
        ],
    )
    assert len(dto.source_media_items) == 1


def test_omni_still_rejects_end_frame_with_reference_images():
    """Relaxing the start frame must not relax interpolation, which Omni lacks."""
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            end_image_asset_id={"id": 1, "type": "source_asset"},
            reference_images=[
                ReferenceImageDto(
                    asset_id=2,
                    reference_type=ReferenceImageTypeEnum.ASSET,
                ),
            ],
            source_media_items=[],  # Force validator to run
        )
    assert "Reference media cannot be used at the same time" in str(
        exc_info.value
    )


def test_validate_video_generation_model_error():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test", workspace_id=1, generation_model="invalid_model"
        )
    assert (
        "Invalid generation model for video" in str(exc_info.value)
        or "enum" in str(exc_info.value).lower()
    )


def test_create_veo_dto_with_omni_references():
    dto = CreateVeoDto(
        prompt="Test Omni",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
        reference_video={"id": 10, "type": "media_item"},
        parent_media_item_id=15,
    )
    assert (
        dto.generation_model
        == GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW
    )
    assert dto.reference_video.id == 10
    assert dto.reference_video.type == "media_item"
    assert dto.parent_media_item_id == 15
    assert dto.parent_media_index == 0


def test_omni_rejects_audio_reference():
    """Omni cannot process audio references.

    The Interactions API accepts an audio content part without erroring and then
    ignores it, so the request has to be rejected here or the caller silently
    gets a video that ignored their audio.
    """
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test Omni",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            reference_audio={"id": 20, "type": "media_item"},
        )
    assert "audio references" in str(exc_info.value)


def test_omni_rejects_video_extension():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Extend this",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            source_video_asset_id={"id": 5, "type": "source_asset"},
        )
    assert "video extension" in str(exc_info.value)


def test_omni_rejects_last_frame_interpolation():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Interpolate",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            start_image_asset_id={"id": 5, "type": "source_asset"},
            end_image_asset_id={"id": 6, "type": "source_asset"},
        )
    assert "interpolation" in str(exc_info.value)


def test_veo_still_allows_extension_and_interpolation():
    """The Omni restrictions must not leak onto Veo, which supports both."""
    dto = CreateVeoDto(
        prompt="Extend this",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
        source_video_asset_id={"id": 5, "type": "source_asset"},
    )
    assert dto.source_video_asset_id.id == 5

    dto = CreateVeoDto(
        prompt="Interpolate",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
        start_image_asset_id={"id": 5, "type": "source_asset"},
        end_image_asset_id={"id": 6, "type": "source_asset"},
    )
    assert dto.end_image_asset_id.id == 6


@pytest.mark.parametrize("duration", [3, 7, 10])
def test_omni_accepts_its_full_duration_range(duration):
    dto = CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        duration_seconds=duration,
    )
    assert dto.duration_seconds == duration


@pytest.mark.parametrize("duration", [2, 11])
def test_omni_rejects_out_of_range_durations(duration):
    with pytest.raises(ValidationError):
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            duration_seconds=duration,
        )


@pytest.mark.parametrize("duration", [4, 6, 8])
def test_veo_accepts_its_discrete_durations(duration):
    dto = CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
        duration_seconds=duration,
    )
    assert dto.duration_seconds == duration


@pytest.mark.parametrize("duration", [5, 7, 10])
def test_veo_rejects_durations_outside_its_fixed_set(duration):
    """Veo offers discrete lengths, so in-range values can still be invalid.

    5 and 7 fall between supported lengths; 10 is above them and must not be
    allowed through by Omni's wider range.
    """
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
            duration_seconds=duration,
        )
    assert "supports durations of" in str(exc_info.value)


def test_omni_accepts_seven_reference_images():
    dto = CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        reference_images=[{"asset_id": i} for i in range(7)],
    )
    assert len(dto.reference_images) == 7


def test_veo_reference_ceiling_not_widened_by_omni():
    """Relaxing max_length to 7 must not let Veo past its own limit of 3."""
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
            reference_images=[{"asset_id": i} for i in range(4)],
        )
    assert "reference images" in str(exc_info.value)


def test_validate_resolution_by_model():
    # Gemini Omni 1.0 - 1K is OK
    CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        resolution="1K",
    )

    # Gemini Omni 1.0 - 2K is error
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            resolution="2K",
        )
    assert "does not support resolution '2K'" in str(exc_info.value)

    # Gemini Omni 1.1 - 360p, 720p, 1080p, 4K, 1K, 2K are all OK
    for res in ["360p", "720p", "1080p", "4K", "1K", "2K"]:
        dto = CreateVeoDto(
            prompt="Test Omni 1.1",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH,
            resolution=res,
        )
        assert dto.resolution == res

        dto_preview = CreateVeoDto(
            prompt="Test Omni 1.1 Preview",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
            resolution=res,
        )
        assert dto_preview.resolution == res

    # Veo 3.1 Lite - 2K is OK
    CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_1_LITE_GENERATE_001,
        resolution="2K",
    )

    # Veo 3.1 Lite - 4K is error
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_3_1_LITE_GENERATE_001,
            resolution="4K",
        )
    assert "does not support resolution '4K'" in str(exc_info.value)

    # Veo 3.1 Generate 001 - 4K is OK
    CreateVeoDto(
        prompt="Test",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
        resolution="4K",
    )


def test_omni_1_1_allows_extension_and_interpolation():
    """Omni 1.1 supports video extension and first+last frame interpolation."""
    dto_ext = CreateVeoDto(
        prompt="Extend this scene",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
        source_video_asset_id={"id": 5, "type": "source_asset"},
    )
    assert dto_ext.source_video_asset_id.id == 5

    dto_interp = CreateVeoDto(
        prompt="Interpolate between frames",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH,
        start_image_asset_id={"id": 5, "type": "source_asset"},
        end_image_asset_id={"id": 6, "type": "source_asset"},
    )
    assert dto_interp.start_image_asset_id.id == 5
    assert dto_interp.end_image_asset_id.id == 6


def test_omni_1_1_rejects_audio_references():
    """Omni 1.1 still does not support audio references."""
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Test Omni 1.1",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
            reference_audio={"id": 20, "type": "media_item"},
        )
    assert "audio references" in str(exc_info.value)


def test_omni_accepts_edit_source():
    """Editing an uploaded clip is distinct from extending one.

    Extension is unsupported by Omni and rejected; editing is supported and
    verified against the live API, so the two must not share a field.
    """
    dto = CreateVeoDto(
        prompt="Make the balloon blue",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        edit_source={"id": 7, "type": "source_asset"},
    )
    assert dto.edit_source.id == 7
    assert dto.edit_source.type == "source_asset"


def test_veo_rejects_edit_source():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Make the balloon blue",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_3_1_GENERATE_001,
            edit_source={"id": 7, "type": "source_asset"},
        )
    assert "does not support video editing" in str(exc_info.value)


def test_edit_source_allows_reference_images():
    """Compositing a character into existing footage is a supported pattern.

    Google's Vertex sample sends text + image + video with task=edit, and it
    was confirmed working against the live API. An earlier version of this
    validator rejected the combination and blocked the use case outright.
    """
    dto = CreateVeoDto(
        prompt="Place the woman from the reference into this scene",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
        edit_source={"id": 7, "type": "source_asset"},
        reference_images=[{"asset_id": 1}],
    )
    assert dto.edit_source.id == 7
    assert len(dto.reference_images) == 1
    # Omni refuses to edit a clip containing speech when references are also
    # supplied, and its own output always has audio, so this defaults on.
    assert dto.strip_source_audio is True


def test_edit_source_rejects_a_second_video():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Edit this",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
            edit_source={"id": 7, "type": "source_asset"},
            reference_video={"id": 8, "type": "source_asset"},
        )
    assert "two video sources" in str(exc_info.value)


def test_edit_source_rejects_reference_videos_list():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Edit this",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
            edit_source={"id": 7, "type": "source_asset"},
            reference_videos=[{"id": 8, "type": "source_asset"}],
        )
    assert "two video sources" in str(exc_info.value)


def test_omni_1_1_allows_multiple_reference_videos():
    dto = CreateVeoDto(
        prompt="Generate video using reference videos",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
        reference_videos=[
            {"id": 1, "type": "source_asset"},
            {"id": 2, "type": "source_asset"},
        ],
    )
    assert dto.reference_videos is not None
    assert len(dto.reference_videos) == 2


def test_omni_1_1_rejects_exceeding_max_reference_videos():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Too many reference videos",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_1_FLASH_PREVIEW,
            reference_videos=[
                {"id": 1, "type": "source_asset"},
                {"id": 2, "type": "source_asset"},
                {"id": 3, "type": "source_asset"},
                {"id": 4, "type": "source_asset"},
            ],
        )
    assert "at most 3 reference videos" in str(
        exc_info.value
    ) or "List should have at most 3 items" in str(exc_info.value)


def test_omni_1_0_rejects_reference_videos():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Omni 1.0 does not support video references",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            reference_videos=[{"id": 1, "type": "source_asset"}],
        )
    assert "video references" in str(exc_info.value)


def test_edit_source_rejects_a_start_frame():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Edit this",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_FLASH_PREVIEW,
            edit_source={"id": 7, "type": "source_asset"},
            start_image_asset_id={"id": 9, "type": "source_asset"},
        )
    assert "start frame" in str(exc_info.value)


def test_dialogue_model_valid():
    dto = CreateVeoDto(
        prompt="Character speaking in tavern",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
        start_image_asset_id={"id": 1, "type": "source_asset"},
        reference_audio={"id": 2, "type": "source_asset"},
        duration_seconds=8,
        resolution="1K",
    )
    assert dto.generation_model == GenerationModelEnum.VEO_EXP_A2V_GENERATION
    assert dto.duration_seconds == 8
    assert dto.resolution == "1K"


def test_dialogue_model_missing_audio():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Character speaking in tavern",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            duration_seconds=8,
            resolution="1K",
        )
    assert "requires a reference audio track" in str(exc_info.value)


def test_dialogue_model_missing_image():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Character speaking in tavern",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            reference_audio={"id": 2, "type": "source_asset"},
            duration_seconds=8,
            resolution="1K",
        )
    assert "requires a character image" in str(exc_info.value)


def test_dialogue_model_invalid_duration():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Character speaking in tavern",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            reference_audio={"id": 2, "type": "source_asset"},
            duration_seconds=6,
            resolution="1K",
        )
    assert "8-second duration" in str(exc_info.value)


def test_dialogue_model_invalid_resolution():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Character speaking in tavern",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            reference_audio={"id": 2, "type": "source_asset"},
            duration_seconds=8,
            resolution="4K",
        )
    assert "1K resolution" in str(exc_info.value)


def test_dialogue_model_conflicting_inputs():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Character speaking in tavern",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            reference_audio={"id": 2, "type": "source_asset"},
            end_image_asset_id={"id": 3, "type": "source_asset"},
            duration_seconds=8,
            resolution="1K",
        )
    assert "only supports an image input and an audio reference" in str(
        exc_info.value
    )


def test_omni_1_2_flash_allows_15s_text_to_video_and_10_ref_images():
    dto_t2v = CreateVeoDto(
        prompt="Epic 15-second camera flythrough",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_1_2_FLASH,
        duration_seconds=15,
    )
    assert dto_t2v.duration_seconds == 15

    dto_refs = CreateVeoDto(
        prompt="Cast ensemble shot",
        workspace_id=1,
        generation_model=GenerationModelEnum.GEMINI_OMNI_1_2_FLASH,
        duration_seconds=10,
        reference_images=[{"asset_id": i} for i in range(1, 11)],
    )
    assert len(dto_refs.reference_images) == 10


def test_omni_1_2_flash_rejects_over_10s_with_references():
    with pytest.raises(ValidationError) as exc_info:
        CreateVeoDto(
            prompt="Too long for references",
            workspace_id=1,
            generation_model=GenerationModelEnum.GEMINI_OMNI_1_2_FLASH,
            duration_seconds=12,
            reference_images=[{"asset_id": 1}],
        )
    assert "10" in str(exc_info.value)


def test_veo_3_1_lite_preview_valid():
    dto = CreateVeoDto(
        prompt="Fast cinematic shot",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_3_1_LITE_PREVIEW,
        duration_seconds=8,
        resolution="720p",
    )
    assert dto.generation_model == GenerationModelEnum.VEO_3_1_LITE_PREVIEW


def test_gmp_perf_estimation_validation():
    dto = CreateVeoDto(
        prompt="",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_EXP_PERF_ESTIMATION,
        edit_source={"id": 10, "type": "source_asset"},
        duration_seconds=10,
    )
    assert dto.generation_model == GenerationModelEnum.VEO_EXP_PERF_ESTIMATION

    with pytest.raises(ValidationError, match="requires an input video"):
        CreateVeoDto(
            prompt="",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_PERF_ESTIMATION,
            duration_seconds=8,
        )

    with pytest.raises(ValidationError, match="10 seconds"):
        CreateVeoDto(
            prompt="",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_PERF_ESTIMATION,
            edit_source={"id": 10, "type": "source_asset"},
            duration_seconds=12,
        )


def test_gmp_omni_perf_validation():
    dto = CreateVeoDto(
        prompt="Sci-fi pilot acting in cockpit",
        workspace_id=1,
        generation_model=GenerationModelEnum.OMNI_PERF,
        reference_images=[{"asset_id": 1}, {"asset_id": 2}],
        reference_video={"id": 5, "type": "source_asset"},
        output_codec="prores",
        compression_quality="lossless",
        audio_control_mode="preserve_video_audio",
        duration_seconds=10,
    )
    assert dto.output_codec == "prores"
    assert dto.compression_quality == "lossless"

    # Missing prompt
    with pytest.raises(ValidationError, match="non-empty prompt"):
        CreateVeoDto(
            prompt="   ",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_PERF,
            reference_images=[{"asset_id": 1}],
            reference_video={"id": 5, "type": "source_asset"},
        )

    # Missing reference images
    with pytest.raises(
        ValidationError, match="between 1 and 5 reference images"
    ):
        CreateVeoDto(
            prompt="Acting",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_PERF,
            reference_video={"id": 5, "type": "source_asset"},
        )

    # Over 5 reference images
    with pytest.raises(
        ValidationError, match="between 1 and 5 reference images"
    ):
        CreateVeoDto(
            prompt="Acting",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_PERF,
            reference_images=[{"asset_id": i} for i in range(6)],
            reference_video={"id": 5, "type": "source_asset"},
        )

    # Missing driving video
    with pytest.raises(ValidationError, match="driving performance"):
        CreateVeoDto(
            prompt="Acting",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_PERF,
            reference_images=[{"asset_id": 1}],
        )

    # target_audio without reference_audio
    with pytest.raises(ValidationError, match="target_audio"):
        CreateVeoDto(
            prompt="Acting",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_PERF,
            reference_images=[{"asset_id": 1}],
            reference_video={"id": 5, "type": "source_asset"},
            audio_control_mode="target_audio",
        )


def test_gmp_omni_cine_validation():
    dto = CreateVeoDto(
        prompt="Cinematic crane shot over ancient ruins",
        workspace_id=1,
        generation_model=GenerationModelEnum.OMNI_CINE,
        start_image_asset_id={"id": 1, "type": "source_asset"},
        end_image_asset_id={"id": 2, "type": "source_asset"},
        reference_images=[{"asset_id": i} for i in range(1, 8)],
        reference_audio={"id": 9, "type": "source_asset"},
        audio_control_mode="reference_audio",
        duration_seconds=10,
    )
    assert len(dto.reference_images) == 7

    # lastFrame without firstFrame rejected
    with pytest.raises(ValidationError, match="lastFrame"):
        CreateVeoDto(
            prompt="Interpolate",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_CINE,
            end_image_asset_id={"id": 2, "type": "source_asset"},
        )

    # target_audio without audio rejected
    with pytest.raises(ValidationError, match="reference_audio"):
        CreateVeoDto(
            prompt="Shot",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_CINE,
            audio_control_mode="target_audio",
        )


def test_gmp_video_textures_and_pro_format_validation():
    dto = CreateVeoDto(
        prompt="Flowing lava texture",
        workspace_id=1,
        generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TEXTURES,
        seamless_loop=True,
        tessellate_horizontal=True,
        tessellate_vertical=True,
        output_codec="dnxhr",
        compression_quality="lossless",
    )
    assert dto.seamless_loop is True

    # Must enable at least one seamless flag
    with pytest.raises(ValidationError, match="seamless"):
        CreateVeoDto(
            prompt="Lava",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TEXTURES,
        )

    # Simultaneous looping + image conditioning rejected
    with pytest.raises(
        ValidationError,
        match="Simultaneous looping and image conditioning is not supported",
    ):
        CreateVeoDto(
            prompt="Lava",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TEXTURES,
            seamless_loop=True,
            start_image_asset_id={"id": 1, "type": "source_asset"},
        )

    # Lossless with h264 rejected
    with pytest.raises(
        ValidationError,
        match="Lossless compressionQuality requires output_codec",
    ):
        CreateVeoDto(
            prompt="Shot",
            workspace_id=1,
            generation_model=GenerationModelEnum.OMNI_CINE,
            output_codec="h264",
            compression_quality="lossless",
        )
