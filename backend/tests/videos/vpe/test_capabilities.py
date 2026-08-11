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
"""Tests for the VPE capability registry.

The registry cannot be validated against the live API - the program is
allowlist-gated - so these tests check the two things that are checkable:
that every entry is internally consistent, and that the handful of numbers
other modules will hard-code against still match the docs.
"""

import pytest

from src.videos.vpe.capabilities import (
    VPE_CAPABILITIES,
    VPE_CAPABILITY_LIST,
    VPE_FPS,
    VpeCapability,
    VpeCapabilityId,
    VpeFieldRequirement,
    VpeInstanceField,
    VpeInstanceFieldSpec,
    VpeKnownIssueId,
    VpeOrientation,
    VpeOutputFormat,
    VpeOutputSpec,
    VpeParameterLocation,
    VpeValueType,
    VpeVideoConstraints,
    capabilities_for_model,
    get_capability,
)

ALL_CAPABILITIES = pytest.mark.parametrize(
    "capability",
    VPE_CAPABILITY_LIST,
    ids=[item.capability_id.value for item in VPE_CAPABILITY_LIST],
)


def test_registry_covers_every_capability_id():
    assert set(VPE_CAPABILITIES) == set(VpeCapabilityId)
    assert len(VPE_CAPABILITY_LIST) == len(VpeCapabilityId)
    for capability_id, capability in VPE_CAPABILITIES.items():
        assert capability.capability_id is capability_id


def test_registry_is_read_only():
    with pytest.raises(TypeError):
        VPE_CAPABILITIES[VpeCapabilityId.UPSCALE] = None  # type: ignore


@ALL_CAPABILITIES
def test_capability_is_named_and_described(capability: VpeCapability):
    assert capability.model_name.strip() == capability.model_name
    assert capability.model_name
    assert " " not in capability.model_name
    assert capability.label
    assert capability.description
    assert capability.doc_url.startswith("https://")


@ALL_CAPABILITIES
def test_capability_routes_and_writes_to_gcs(capability: VpeCapability):
    """Every request is routed by modelName and must name an output folder."""
    model_param = capability.parameter(
        "modelName",
        VpeParameterLocation.EXPERIMENTS,
    )
    assert model_param is not None
    assert model_param.is_required
    assert model_param.allowed_values == (capability.model_name,)

    storage = capability.parameter(
        "storageUri",
        VpeParameterLocation.PARAMETERS,
    )
    assert storage is not None
    assert storage.is_required


@ALL_CAPABILITIES
def test_instance_fields_declare_mime_types(capability: VpeCapability):
    assert capability.instance_fields
    for spec in capability.instance_fields:
        if spec.field is VpeInstanceField.PROMPT:
            assert not spec.mime_types
            continue
        assert spec.mime_types, f"{spec.field.value} has no mime types"
        for mime_type in spec.mime_types:
            assert "/" in mime_type
            assert mime_type == mime_type.lower()
            assert spec.accepts(mime_type)


@ALL_CAPABILITIES
def test_list_fields_are_bounded(capability: VpeCapability):
    """Reference arrays must carry a cap; single objects must not."""
    for spec in capability.instance_fields:
        is_list = spec.field in (
            VpeInstanceField.REFERENCE_IMAGES,
            VpeInstanceField.REFERENCE_AUDIOS,
        )
        if is_list:
            assert spec.max_items is not None and spec.max_items >= 1
        else:
            assert spec.max_items is None


@ALL_CAPABILITIES
def test_frame_bounds_are_sane(capability: VpeCapability):
    constraints = capability.input_video
    if constraints is not None:
        assert constraints.fps == VPE_FPS
        assert constraints.min_frames >= 1
        assert constraints.min_frames <= constraints.max_frames
        assert constraints.frame_sizes
        assert constraints.accepts_frame_count(constraints.min_frames)
        assert constraints.accepts_frame_count(constraints.max_frames)
        assert not constraints.accepts_frame_count(
            constraints.max_frames + 1,
        )
    output = capability.output
    assert output.fps == VPE_FPS
    assert output.max_frames >= 1
    assert output.frame_sizes
    assert output.formats


@ALL_CAPABILITIES
def test_frame_sizes_are_positive(capability: VpeCapability):
    for size in capability.frame_sizes:
        assert size.width > 0 and size.height > 0
        assert str(size) == f"{size.width}x{size.height}"


@ALL_CAPABILITIES
def test_orientation_is_set_and_consistent(capability: VpeCapability):
    assert isinstance(capability.orientation, VpeOrientation)
    aspect_ratio = capability.parameter(
        "aspectRatio",
        VpeParameterLocation.PARAMETERS,
    )
    if capability.orientation is VpeOrientation.PORTRAIT_OK:
        # Portrait is only claimable if there is a way to ask for it.
        assert aspect_ratio is not None
        assert "9:16" in aspect_ratio.allowed_values
    else:
        assert all(size.is_landscape for size in capability.frame_sizes)


def test_only_the_upscaler_supports_portrait():
    portrait = {
        capability.capability_id
        for capability in VPE_CAPABILITY_LIST
        if capability.orientation is VpeOrientation.PORTRAIT_OK
    }
    assert portrait == {VpeCapabilityId.UPSCALE}

    landscape_only = {
        capability.capability_id
        for capability in VPE_CAPABILITY_LIST
        if capability.orientation is VpeOrientation.LANDSCAPE_ONLY
    }
    # The three the docs say "Landscape 16:9 orientation only" about; the
    # rest are the unstated-fixed-frame tier and must not be lumped in.
    assert landscape_only == {
        VpeCapabilityId.VIDEO_TRANSFORM,
        VpeCapabilityId.PERF_ESTIMATION,
        VpeCapabilityId.PERF_GENERATION,
    }


@ALL_CAPABILITIES
def test_parameters_are_uniquely_addressable(capability: VpeCapability):
    paths = [spec.json_path for spec in capability.parameters]
    assert len(paths) == len(set(paths))
    for spec in capability.parameters:
        assert spec.json_path.endswith(f".{spec.name}")
        assert spec.json_path.startswith("parameters")
        if spec.allowed_values and isinstance(spec.default, str):
            assert spec.default in spec.allowed_values
        if spec.minimum is not None and spec.maximum is not None:
            assert spec.minimum <= spec.maximum


@ALL_CAPABILITIES
def test_known_issues_are_unique_and_identified(capability: VpeCapability):
    issue_ids = [issue.issue_id for issue in capability.known_issues]
    assert len(issue_ids) == len(set(issue_ids))
    for issue in capability.known_issues:
        assert isinstance(issue.issue_id, VpeKnownIssueId)
        assert issue.summary
        assert capability.has_known_issue(issue.issue_id)


@ALL_CAPABILITIES
def test_media_constraints_exist_for_accepted_media(
    capability: VpeCapability,
):
    if capability.supports(VpeInstanceField.VIDEO):
        assert capability.input_video is not None
    takes_stills = any(
        capability.supports(field)
        for field in (
            VpeInstanceField.IMAGE,
            VpeInstanceField.LAST_FRAME,
            VpeInstanceField.REFERENCE_IMAGES,
        )
    )
    if takes_stills:
        assert capability.input_image is not None
    if capability.supports(VpeInstanceField.REFERENCE_AUDIOS):
        assert capability.input_audio is not None


def test_lookup_by_id_accepts_enum_and_string():
    by_enum = get_capability(VpeCapabilityId.OMNI_CINE)
    by_string = get_capability("omni_cine")
    assert by_enum is by_string
    assert by_enum.model_name == "omni-cine"


def test_lookup_rejects_unknown_id():
    with pytest.raises(ValueError, match="Unknown VPE capability"):
        get_capability("veo-4-does-not-exist")


def test_one_checkpoint_can_back_two_capabilities():
    upscalers = capabilities_for_model("veo3p1_upscale")
    assert {item.capability_id for item in upscalers} == {
        VpeCapabilityId.UPSCALE,
        VpeCapabilityId.UPSCALE_SEAMLESS,
    }
    assert capabilities_for_model("nope") == ()


def test_documented_reference_image_caps():
    assert get_capability(VpeCapabilityId.OMNI_CINE).max_reference_images == 7
    perf = get_capability(VpeCapabilityId.PERF_GENERATION)
    assert perf.max_reference_images == 5
    assert get_capability(VpeCapabilityId.UPSCALE).max_reference_images == 0


def test_documented_frame_bounds():
    upscale = get_capability(VpeCapabilityId.UPSCALE).input_video
    assert upscale is not None
    assert (upscale.min_frames, upscale.max_frames) == (96, 192)
    assert (upscale.min_seconds, upscale.max_seconds) == (4.0, 8.0)

    omni = get_capability(VpeCapabilityId.OMNI_CINE)
    assert omni.output.max_frames == 240
    assert omni.input_video is not None
    assert omni.input_video.max_frames == 240

    for capability_id in (
        VpeCapabilityId.PERF_ESTIMATION,
        VpeCapabilityId.PERF_GENERATION,
    ):
        constraints = get_capability(capability_id).input_video
        assert constraints is not None
        # Exactly 192 frames, not "about 8 seconds".
        assert constraints.min_frames == constraints.max_frames == 192

    transform = get_capability(VpeCapabilityId.VIDEO_TRANSFORM).input_video
    assert transform is not None
    assert (transform.min_frames, transform.max_frames) == (1, 192)


def test_audio_is_only_claimed_where_documented():
    for capability in VPE_CAPABILITY_LIST:
        if capability.output.has_audio_stem:
            assert capability.output.produces_audio
    omni = get_capability(VpeCapabilityId.OMNI_CINE).output
    assert omni.produces_audio and omni.has_audio_stem
    assert not get_capability(
        VpeCapabilityId.DIALOGUE_DRIVEN,
    ).output.produces_audio


def test_upscaler_carries_the_issues_callers_must_handle():
    upscale = get_capability(VpeCapabilityId.UPSCALE)
    assert upscale.has_known_issue(
        VpeKnownIssueId.UPSCALE_1080P_INPUT_NEEDS_ASPECT_RATIO,
    )
    assert upscale.has_known_issue(
        VpeKnownIssueId.UPSCALE_4K_PNG16_NO_OPERATION_ID,
    )
    seamless = get_capability(VpeCapabilityId.UPSCALE_SEAMLESS)
    assert seamless.has_known_issue(
        VpeKnownIssueId.UPSCALE_SEAMLESS_FLAGS_MUST_MATCH_SOURCE,
    )
    # 1080p is unreachable for seamless sources, so it is not offered.
    resolution = seamless.parameter("resolution")
    assert resolution is not None
    assert resolution.allowed_values == ("4k",)


def test_seamless_flags_live_two_levels_deep():
    textures = get_capability(VpeCapabilityId.VIDEO_TEXTURES)
    loop = textures.parameter("loop")
    assert loop is not None
    assert loop.json_path == "parameters.experiments.seamless.loop"
    assert loop.value_type is VpeValueType.BOOLEAN
    assert loop.default is False


def test_prompt_caps_match_the_docs():
    assert (
        get_capability(VpeCapabilityId.DIALOGUE_DRIVEN).max_prompt_chars == 1024
    )
    assert (
        get_capability(VpeCapabilityId.PERF_GENERATION).max_prompt_chars == 1000
    )


def test_prompt_spec_rejects_mime_types():
    with pytest.raises(ValueError, match="prompt is text"):
        VpeInstanceFieldSpec(
            field=VpeInstanceField.PROMPT,
            requirement=VpeFieldRequirement.REQUIRED,
            mime_types=("text/plain",),
        )


def test_reference_list_spec_requires_a_cap():
    with pytest.raises(ValueError, match="needs a positive max_items"):
        VpeInstanceFieldSpec(
            field=VpeInstanceField.REFERENCE_IMAGES,
            requirement=VpeFieldRequirement.OPTIONAL,
            mime_types=("image/png",),
        )


def test_inverted_frame_bounds_are_rejected():
    with pytest.raises(ValueError, match="Invalid frame bounds"):
        VpeVideoConstraints(
            min_frames=192,
            max_frames=96,
            frame_sizes=(
                get_capability(
                    VpeCapabilityId.UPSCALE,
                ).output.frame_sizes[0],
            ),
        )


def test_frame_sequence_flag_must_match_the_formats():
    sizes = get_capability(VpeCapabilityId.OMNI_CINE).output.frame_sizes
    with pytest.raises(ValueError, match="emits_frame_sequence"):
        VpeOutputSpec(
            frame_sizes=sizes,
            formats=(VpeOutputFormat.H264_MP4,),
            max_frames=192,
            emits_frame_sequence=True,
        )


def test_vpe_config_ships_off_and_regional():
    """VPE_LOCATION cannot reuse LOCATION, which defaults to 'global'."""
    # Imported here rather than at module scope: config_service builds a
    # settings instance on import, so it needs conftest's credential mocks in
    # place first, and the registry tests should not depend on that at all.
    from src.config.config_service import ConfigService

    fields = ConfigService.model_fields
    assert fields["VPE_ENABLED"].default is False
    assert fields["VPE_LOCATION"].default == "us-central1"
    assert fields["VPE_BUCKET"].default == ""
    assert fields["VPE_DRY_RUN"].default is False
    assert fields["LOCATION"].default == "global"
