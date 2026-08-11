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
"""Contract tests for the VPE payload builders.

The VPE program is allowlist-gated, so no request built here can be sent
anywhere. The documented curl samples are therefore the specification, and
the first half of this file is those samples transcribed one for one: every
JSON body in the nine VPE pages, with the shell variables resolved, asserted
against what the builder emits. If a builder ever disagrees with one of
these fixtures, the builder is what changed by mistake.

The second half checks the things no sample can show: that a value a
capability cannot express is refused rather than dropped, that nothing is
defaulted in on the caller's behalf, and that every parameter the registry
declares is reachable from some request field.
"""

import json
from typing import Any

import pytest

from src.videos.vpe.capabilities import (
    VPE_CAPABILITY_LIST,
    VpeCapability,
    VpeCapabilityId,
    VpeCodec,
    VpeCompressionQuality,
    VpeInstanceField,
    VpeParameterLocation,
    get_capability,
)
from src.videos.vpe.payloads import (
    VpeConditioningFrame,
    VpeMediaRef,
    VpePayloadError,
    VpeRequest,
    VpeSeamlessFlags,
    build_payload,
)

# Every sample resolves BUCKET to 'yourbucketname'; the two standalone
# fragments on the professional-formats page use 'my-bucket'.
BUCKET = "gs://yourbucketname"
OTHER_BUCKET = "gs://my-bucket"

# Samples that timestamp their output folder are pinned to one instant so
# the fixtures stay comparable.
OUTPUT_DIR = f"{BUCKET}/outputs/"
OUTPUT_STAMPED = f"{BUCKET}/outputs/20260805-120000"


# --- Doc sample fixtures -------------------------------------------------
#
# Prompts marked "truncated" are cut off mid-word by the PDF's column width.
# The visible text is used verbatim and completed with the obvious word; the
# prompt string is not load-bearing for the payload shape.

# video transform samples 1 and 3 (truncated).
CAR_PROMPT = (
    "The video provides a low-angle perspective following an orange car"
)
# video transform sample 2 (truncated).
BALLERINA_PROMPT = (
    "A practice performance by a ballerina on stage with all seats empty"
)
# video textures sample 1 (truncated).
FOAM_PROMPT = (
    "Orthographic macro of thick car wash foam pressed flat against glass"
)
CAT_PROMPT = "A long hair cat walks on a stone wall"
SHAMPOO_PROMPT = "A person speaking passionately about shampoo"


def test_doc_sample_upscaler():
    """upscaler: "Construct the JSON payload"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=f"{BUCKET}/outputs/video_20260805_120000.mp4",
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/video.mp4",
                mime_type="video/mp4",
            ),
            compression_quality="lossless_16bit_png",
            resolution="4k",
            aspect_ratio="16:9",
        ),
    )

    assert payload == {
        "instances": [
            {
                "video": {
                    "gcsUri": f"{BUCKET}/inputs/video.mp4",
                    "mimeType": "video/mp4",
                },
            },
        ],
        "parameters": {
            "task": "upscale",
            "compressionQuality": "lossless_16bit_png",
            "resolution": "4k",
            "aspectRatio": "16:9",
            "storageUri": f"{BUCKET}/outputs/video_20260805_120000.mp4",
            "experiments": {"modelName": "veo3p1_upscale"},
        },
    }


def test_doc_sample_upscaling_tessellation_and_looping():
    """upscaling-tessellation-and-looping: "Sample request"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
            storage_uri=OUTPUT_DIR,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/720p_tessellated_video.mp4",
                mime_type="video/mp4",
            ),
            resolution="4k",
            aspect_ratio="16:9",
            compression_quality="lossless_16bit_png",
            sharpness=2,
            seamless=VpeSeamlessFlags(
                loop=True,
                tessellate_horizontal=True,
                tessellate_vertical=True,
            ),
        ),
    )

    assert payload == {
        "instances": [
            {
                "video": {
                    "gcsUri": f"{BUCKET}/inputs/720p_tessellated_video.mp4",
                    "mimeType": "video/mp4",
                },
            },
        ],
        "parameters": {
            "task": "upscale",
            "resolution": "4k",
            "aspectRatio": "16:9",
            "compressionQuality": "lossless_16bit_png",
            "sharpness": 2,
            "experiments": {
                "modelName": "veo3p1_upscale",
                "seamless": {
                    "loop": True,
                    "tessellateHorizontal": True,
                    "tessellateVertical": True,
                },
            },
            "storageUri": OUTPUT_DIR,
        },
    }


def test_doc_sample_professional_formats_6_png16_output_4k_upscale():
    """professional-formats sample 6: "16-bit PNG sequence output"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
            storage_uri=OUTPUT_STAMPED,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/source_video.mp4",
                mime_type="video/mp4",
            ),
            resolution="4k",
            aspect_ratio="16:9",
            compression_quality="lossless_16bit_png",
            sharpness=2,
            seamless=VpeSeamlessFlags(
                loop=False,
                tessellate_horizontal=True,
                tessellate_vertical=True,
            ),
        ),
    )

    assert payload == {
        "instances": [
            {
                "video": {
                    "gcsUri": f"{BUCKET}/inputs/source_video.mp4",
                    "mimeType": "video/mp4",
                },
            },
        ],
        "parameters": {
            "task": "upscale",
            "resolution": "4k",
            "aspectRatio": "16:9",
            "compressionQuality": "lossless_16bit_png",
            "sharpness": 2,
            "experiments": {
                "modelName": "veo3p1_upscale",
                "seamless": {
                    "loop": False,
                    "tessellateHorizontal": True,
                    "tessellateVertical": True,
                },
            },
            "storageUri": OUTPUT_STAMPED,
        },
    }


def test_doc_sample_omni_cine():
    """omni-cine: "Sending a request"."""
    plate = f"{BUCKET}/shots/shot_001/source/main_plate_01/*.exr"
    clean_plate = f"{BUCKET}/shots/shot_001/source/ref/clean_plate_001.exr"
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=f"{BUCKET}/outputs/20260805-120000/",
            prompt="Remove the person wearing a red jacket from the scene",
            video=VpeMediaRef(gcs_uri=plate, mime_type="image/x-exr"),
            reference_images=(
                VpeMediaRef(gcs_uri=clean_plate, mime_type="image/x-exr"),
            ),
            compression_quality="lossless",
            codec="prores",
        ),
    )

    assert payload == {
        "instances": [
            {
                "prompt": (
                    "Remove the person wearing a red jacket from the scene"
                ),
                "video": {
                    "gcsUri": plate,
                    "mimeType": "image/x-exr",
                },
                "referenceImages": [
                    {
                        "image": {
                            "gcsUri": clean_plate,
                            "mimeType": "image/x-exr",
                        },
                        "referenceType": "asset",
                    },
                ],
            },
        ],
        "parameters": {
            "storageUri": f"{BUCKET}/outputs/20260805-120000/",
            "compressionQuality": "lossless",
            "experiments": {"modelName": "omni-cine", "codec": "prores"},
        },
    }


def test_doc_sample_video_transform_1_v2v():
    """video-transform sample 1: "Video transform (V2V)"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=OUTPUT_DIR,
            prompt=CAR_PROMPT,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/video.mp4",
                mime_type="video/mp4",
            ),
            seed=777,
            video_transform_strength=0.88,
            num_diffusion_steps=20,
        ),
    )

    assert payload == {
        "instances": [
            {
                "prompt": CAR_PROMPT,
                "video": {
                    "gcsUri": f"{BUCKET}/inputs/video.mp4",
                    "mimeType": "video/mp4",
                },
            },
        ],
        "parameters": {
            "seed": 777,
            "storageUri": OUTPUT_DIR,
            "experiments": {
                "modelName": "veo-exp-video-transform",
                "videoTransformStrength": 0.88,
                "numDiffusionSteps": 20,
            },
        },
    }


def test_doc_sample_video_transform_first_frame_instances():
    """video-transform: the inline "declare the image object" note.

    The doc shows only the instances array for this variant, so only the
    instances array is asserted.
    """
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=f"{OTHER_BUCKET}/outputs/",
            prompt="Your descriptive prompt",
            image=VpeMediaRef(
                gcs_uri=f"{OTHER_BUCKET}/inputs/image.jpg",
                mime_type="image/jpeg",
            ),
            video=VpeMediaRef(
                gcs_uri=f"{OTHER_BUCKET}/inputs/video.mp4",
                mime_type="video/mp4",
            ),
        ),
    )

    assert payload["instances"] == [
        {
            "prompt": "Your descriptive prompt",
            "image": {
                "gcsUri": f"{OTHER_BUCKET}/inputs/image.jpg",
                "mimeType": "image/jpeg",
            },
            "video": {
                "gcsUri": f"{OTHER_BUCKET}/inputs/video.mp4",
                "mimeType": "video/mp4",
            },
        },
    ]


def test_doc_sample_video_transform_2_multi_keyframe_i2v():
    """video-transform sample 2: "I2V with multiple conditioning frames"."""
    frames = f"{BUCKET}/inputs/conditioning_frames"
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=OUTPUT_DIR,
            prompt=BALLERINA_PROMPT,
            image=VpeMediaRef(
                gcs_uri=f"{frames}/0000_s.png",
                mime_type="image/png",
            ),
            seed=777,
            conditioning_frames=(
                VpeConditioningFrame(
                    image=VpeMediaRef(
                        gcs_uri=f"{frames}/0024_s.png",
                        mime_type="image/png",
                    ),
                    frame_num=24,
                ),
                VpeConditioningFrame(
                    image=VpeMediaRef(
                        gcs_uri=f"{frames}/0128_s.png",
                        mime_type="image/png",
                    ),
                    frame_num=128,
                ),
            ),
        ),
    )

    assert payload == {
        "instances": [
            {
                "prompt": BALLERINA_PROMPT,
                "image": {
                    "gcsUri": f"{frames}/0000_s.png",
                    "mimeType": "image/png",
                },
            },
        ],
        "parameters": {
            "seed": 777,
            "storageUri": OUTPUT_DIR,
            "experiments": {
                "modelName": "veo-exp-video-transform",
                "conditioningFrames": [
                    {
                        "image": {
                            "gcsUri": f"{frames}/0024_s.png",
                            "mimeType": "image/png",
                        },
                        "frameNum": 24,
                    },
                    {
                        "image": {
                            "gcsUri": f"{frames}/0128_s.png",
                            "mimeType": "image/png",
                        },
                        "frameNum": 128,
                    },
                ],
            },
        },
    }


def test_doc_sample_video_transform_3_masked():
    """video-transform sample 3: "Masked video transform"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=OUTPUT_DIR,
            prompt=CAR_PROMPT,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/video.mp4",
                mime_type="video/mp4",
            ),
            seed=777,
            video_transform_strength=0.88,
            num_diffusion_steps=20,
            video_transform_mask_gcs_uri=f"{BUCKET}/inputs/maskvideo.mp4",
        ),
    )

    assert payload == {
        "instances": [
            {
                "prompt": CAR_PROMPT,
                "video": {
                    "gcsUri": f"{BUCKET}/inputs/video.mp4",
                    "mimeType": "video/mp4",
                },
            },
        ],
        "parameters": {
            "seed": 777,
            "storageUri": OUTPUT_DIR,
            "experiments": {
                "modelName": "veo-exp-video-transform",
                "videoTransformStrength": 0.88,
                "numDiffusionSteps": 20,
                "videoTransformMaskGcsUri": (f"{BUCKET}/inputs/maskvideo.mp4"),
            },
        },
    }


def test_doc_sample_performance_estimation():
    """performance-controls: "Step 1: Performance estimation blue mesh"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.PERF_ESTIMATION,
            storage_uri=OUTPUT_DIR,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/actor_performance.mp4",
                mime_type="video/mp4",
            ),
            seed=777,
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {"modelName": "veo-exp-perf-estimation"},
            "seed": 777,
            "storageUri": OUTPUT_DIR,
        },
        "instances": [
            {
                "video": {
                    "gcsUri": f"{BUCKET}/inputs/actor_performance.mp4",
                    "mimeType": "video/mp4",
                },
            },
        ],
    }


def test_doc_sample_performance_generation():
    """performance-controls: "Step 2: Performance generation"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.PERF_GENERATION,
            storage_uri=OUTPUT_DIR,
            prompt="A cinematic character performing in a studio",
            reference_images=(
                VpeMediaRef(
                    gcs_uri=f"{BUCKET}/inputs/character_reference.png",
                    mime_type="image/png",
                ),
            ),
            perf_mesh_gcs_uri=f"{BUCKET}/outputs/bluemesh_sample.mp4",
            seed=78,
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {
                "modelName": "veo-exp-perf-generation",
                "perfMeshGcsUri": f"{BUCKET}/outputs/bluemesh_sample.mp4",
            },
            "seed": 78,
            "storageUri": OUTPUT_DIR,
        },
        "instances": [
            {
                "prompt": "A cinematic character performing in a studio",
                "referenceImages": [
                    {
                        "image": {
                            "gcsUri": (
                                f"{BUCKET}/inputs/character_reference.png"
                            ),
                            "mimeType": "image/png",
                        },
                        "referenceType": "ASSET",
                    },
                ],
            },
        ],
    }


def test_doc_sample_dialogue_driven():
    """dialogue-driven-generation: "Sample request"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
            storage_uri=OUTPUT_DIR,
            prompt=SHAMPOO_PROMPT,
            image=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/first_frame.png",
                mime_type="image/png",
            ),
            reference_audios=(
                VpeMediaRef(
                    gcs_uri=f"{BUCKET}/inputs/dialogue.wav",
                    mime_type="audio/wav",
                ),
            ),
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {"modelName": "veo-exp-a2v-generation"},
            "storageUri": OUTPUT_DIR,
        },
        "instances": [
            {
                "prompt": SHAMPOO_PROMPT,
                "image": {
                    "gcsUri": f"{BUCKET}/inputs/first_frame.png",
                    "mimeType": "image/png",
                },
                "referenceAudios": [
                    {
                        "audio": {
                            "gcsUri": f"{BUCKET}/inputs/dialogue.wav",
                            "mimeType": "audio/wav",
                        },
                    },
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    ("codec", "compression_quality"),
    [
        # professional-formats sample 1: "ProRes 12-bit output".
        ("prores", "lossless"),
        # professional-formats sample 2: "DNxHR HQ 8-bit output".
        ("dnxhr", "optimized"),
    ],
)
def test_doc_samples_professional_formats_1_and_2_dialogue_driven(
    codec: str,
    compression_quality: str,
):
    """professional-formats samples 1 and 2, which differ only by codec."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
            storage_uri=OUTPUT_STAMPED,
            prompt=SHAMPOO_PROMPT,
            image=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/first_frame.png",
                mime_type="image/png",
            ),
            reference_audios=(
                VpeMediaRef(
                    gcs_uri=f"{BUCKET}/inputs/dialogue.wav",
                    mime_type="audio/wav",
                ),
            ),
            codec=codec,
            compression_quality=compression_quality,
            seed=42,
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {
                "modelName": "veo-exp-a2v-generation",
                "codec": codec,
            },
            "compressionQuality": compression_quality,
            "seed": 42,
            "storageUri": OUTPUT_STAMPED,
        },
        "instances": [
            {
                "prompt": SHAMPOO_PROMPT,
                "image": {
                    "gcsUri": f"{BUCKET}/inputs/first_frame.png",
                    "mimeType": "image/png",
                },
                "referenceAudios": [
                    {
                        "audio": {
                            "gcsUri": f"{BUCKET}/inputs/dialogue.wav",
                            "mimeType": "audio/wav",
                        },
                    },
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    ("source", "source_mime", "codec"),
    [
        # professional-formats sample 3: ProRes in, ProRes out.
        ("inputs/source_prores.mov", "video/quicktime", "prores"),
        # sample 4: DNxHR in, DNxHR out.
        ("inputs/source_dnxhr.mxf", "application/mxf", "dnxhr"),
        # sample 5: a 16-bit PNG frame sequence in, ProRes out.
        ("inputs/frames/*.png", "image/png", "prores"),
    ],
)
def test_doc_samples_professional_formats_3_to_5_video_transform(
    source: str,
    source_mime: str,
    codec: str,
):
    """professional-formats samples 3, 4 and 5.

    All three send videoTransformStrength but no numDiffusionSteps, which
    is why the builder must not force the documented default in.
    """
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=OUTPUT_STAMPED,
            prompt=CAT_PROMPT,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/{source}",
                mime_type=source_mime,
            ),
            video_transform_strength=0.68,
            codec=codec,
            compression_quality="optimized",
            seed=42,
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {
                "modelName": "veo-exp-video-transform",
                "videoTransformStrength": 0.68,
                "codec": codec,
            },
            "compressionQuality": "optimized",
            "seed": 42,
            "storageUri": OUTPUT_STAMPED,
        },
        "instances": [
            {
                "prompt": CAT_PROMPT,
                "video": {
                    "gcsUri": f"{BUCKET}/{source}",
                    "mimeType": source_mime,
                },
            },
        ],
    }


def test_doc_sample_professional_formats_png_sequence_fragments():
    """professional-formats: the two standalone payload fragments.

    The page shows an "Example instance payload" for a PNG sequence and an
    output-parameter block on its own, neither with a modelName. Both are
    asserted as subsets of a complete video transform request.
    """
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=f"{OTHER_BUCKET}/outputs/",
            prompt="Add dramatic storm clouds rolling in over the valley",
            video=VpeMediaRef(
                gcs_uri=f"{OTHER_BUCKET}/inputs/frames/*.png",
                mime_type="image/png",
            ),
            compression_quality="lossless",
            codec="prores",
        ),
    )

    assert payload["instances"] == [
        {
            "prompt": "Add dramatic storm clouds rolling in over the valley",
            "video": {
                "gcsUri": f"{OTHER_BUCKET}/inputs/frames/*.png",
                "mimeType": "image/png",
            },
        },
    ]
    assert_contains(
        payload["parameters"],
        {
            "storageUri": f"{OTHER_BUCKET}/outputs/",
            "compressionQuality": "lossless",
            "experiments": {"codec": "prores"},
        },
    )


def test_doc_sample_video_textures_1_looping_and_tessellation_t2v():
    """video-textures sample 1: "Looping and tessellation (T2V)"."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TEXTURES,
            storage_uri=OUTPUT_DIR,
            prompt=FOAM_PROMPT,
            seed=42,
            seamless=VpeSeamlessFlags(
                loop=True,
                tessellate_horizontal=True,
                tessellate_vertical=True,
            ),
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {
                "modelName": "veo-exp-video-textures",
                "seamless": {
                    "loop": True,
                    "tessellateHorizontal": True,
                    "tessellateVertical": True,
                },
            },
            "seed": 42,
            "storageUri": OUTPUT_DIR,
        },
        "instances": [{"prompt": FOAM_PROMPT}],
    }


def test_doc_sample_video_textures_2_tessellation_only_i2v():
    """video-textures sample 2: "Tessellation only (I2V ...)"."""
    prompt = "A seamless marble texture with gold veins, high detail"
    start = f"{BUCKET}/inputs/marble_reference_start.png"
    end = f"{BUCKET}/inputs/marble_reference_end.png"
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TEXTURES,
            storage_uri=OUTPUT_STAMPED,
            prompt=prompt,
            image=VpeMediaRef(gcs_uri=start, mime_type="image/png"),
            last_frame=VpeMediaRef(gcs_uri=end, mime_type="image/png"),
            seed=42,
            seamless=VpeSeamlessFlags(
                loop=False,
                tessellate_horizontal=True,
                tessellate_vertical=True,
            ),
        ),
    )

    assert payload == {
        "parameters": {
            "experiments": {
                "modelName": "veo-exp-video-textures",
                "seamless": {
                    "loop": False,
                    "tessellateHorizontal": True,
                    "tessellateVertical": True,
                },
            },
            "seed": 42,
            "storageUri": OUTPUT_STAMPED,
        },
        "instances": [
            {
                "prompt": prompt,
                "image": {"gcsUri": start, "mimeType": "image/png"},
                "lastFrame": {"gcsUri": end, "mimeType": "image/png"},
            },
        ],
    }


# --- Shared fixtures for the behavioural tests ---------------------------

MP4 = VpeMediaRef(gcs_uri=f"{BUCKET}/in/clip.mp4", mime_type="video/mp4")
PNG = VpeMediaRef(gcs_uri=f"{BUCKET}/in/still.png", mime_type="image/png")
WAV = VpeMediaRef(gcs_uri=f"{BUCKET}/in/line.wav", mime_type="audio/wav")
MESH = f"{BUCKET}/out/bluemesh.mp4"

# The smallest request each capability accepts, for the sweeps below.
MINIMAL_REQUESTS: dict[VpeCapabilityId, VpeRequest] = {
    VpeCapabilityId.UPSCALE: VpeRequest(
        capability_id=VpeCapabilityId.UPSCALE,
        storage_uri=OUTPUT_DIR,
        video=MP4,
    ),
    VpeCapabilityId.UPSCALE_SEAMLESS: VpeRequest(
        capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
        storage_uri=OUTPUT_DIR,
        video=MP4,
        # Not decoration: seamless output is refused live unless the
        # delivery format can carry it, so a request without this is not
        # minimal, it is invalid.
        compression_quality="lossless_16bit_png",
    ),
    VpeCapabilityId.OMNI_CINE: VpeRequest(
        capability_id=VpeCapabilityId.OMNI_CINE,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
    ),
    VpeCapabilityId.VIDEO_TRANSFORM: VpeRequest(
        capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
        video=MP4,
    ),
    VpeCapabilityId.PERF_ESTIMATION: VpeRequest(
        capability_id=VpeCapabilityId.PERF_ESTIMATION,
        storage_uri=OUTPUT_DIR,
        video=MP4,
    ),
    VpeCapabilityId.PERF_GENERATION: VpeRequest(
        capability_id=VpeCapabilityId.PERF_GENERATION,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
        reference_images=(PNG,),
        perf_mesh_gcs_uri=MESH,
    ),
    VpeCapabilityId.DIALOGUE_DRIVEN: VpeRequest(
        capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
        image=PNG,
        reference_audios=(WAV,),
    ),
    VpeCapabilityId.VIDEO_TEXTURES: VpeRequest(
        capability_id=VpeCapabilityId.VIDEO_TEXTURES,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
    ),
}

# Reverse of the module's private slot table, restated here so the test is
# an independent statement of where each request field belongs rather than
# a copy of the code under test. None marks a parameter the builder always
# supplies itself.
PARAMETER_ATTRIBUTES: dict[tuple[str, VpeParameterLocation], str | None] = {
    ("storageUri", VpeParameterLocation.PARAMETERS): None,
    ("task", VpeParameterLocation.PARAMETERS): None,
    ("modelName", VpeParameterLocation.EXPERIMENTS): None,
    ("seed", VpeParameterLocation.PARAMETERS): "seed",
    ("compressionQuality", VpeParameterLocation.PARAMETERS): (
        "compression_quality"
    ),
    ("resolution", VpeParameterLocation.PARAMETERS): "resolution",
    ("aspectRatio", VpeParameterLocation.PARAMETERS): "aspect_ratio",
    ("sharpness", VpeParameterLocation.PARAMETERS): "sharpness",
    ("codec", VpeParameterLocation.EXPERIMENTS): "codec",
    ("videoTransformStrength", VpeParameterLocation.EXPERIMENTS): (
        "video_transform_strength"
    ),
    ("numDiffusionSteps", VpeParameterLocation.EXPERIMENTS): (
        "num_diffusion_steps"
    ),
    ("videoTransformMaskGcsUri", VpeParameterLocation.EXPERIMENTS): (
        "video_transform_mask_gcs_uri"
    ),
    ("conditioningFrames", VpeParameterLocation.EXPERIMENTS): (
        "conditioning_frames"
    ),
    ("perfMeshGcsUri", VpeParameterLocation.EXPERIMENTS): "perf_mesh_gcs_uri",
    ("loop", VpeParameterLocation.EXPERIMENTS_SEAMLESS): "seamless",
    ("tessellateHorizontal", VpeParameterLocation.EXPERIMENTS_SEAMLESS): (
        "seamless"
    ),
    ("tessellateVertical", VpeParameterLocation.EXPERIMENTS_SEAMLESS): (
        "seamless"
    ),
}

# A legal value for every optional request field, chosen so that one value
# satisfies every capability that accepts the field.
MAXIMAL_VALUES: dict[str, Any] = {
    "seed": 42,
    "compression_quality": "lossless",
    "resolution": "4k",
    "aspect_ratio": "16:9",
    "sharpness": 2,
    "codec": "prores",
    "video_transform_strength": 0.68,
    "num_diffusion_steps": 20,
    "video_transform_mask_gcs_uri": f"{BUCKET}/in/mask.mp4",
    "conditioning_frames": (VpeConditioningFrame(image=PNG, frame_num=24),),
    "perf_mesh_gcs_uri": MESH,
    "seamless": VpeSeamlessFlags(
        loop=True,
        tessellate_horizontal=True,
        tessellate_vertical=True,
    ),
}

ALL_CAPABILITIES = pytest.mark.parametrize(
    "capability",
    VPE_CAPABILITY_LIST,
    ids=[item.capability_id.value for item in VPE_CAPABILITY_LIST],
)


def assert_contains(actual: Any, expected: dict[str, Any]) -> None:
    """Asserts that every key and value in expected appears in actual.

    Args:
        actual: The built payload, or a block of it.
        expected: A doc fragment that is only part of a whole request.
    """
    for key, value in expected.items():
        assert key in actual, f"missing {key}"
        if isinstance(value, dict):
            assert_contains(actual[key], value)
        else:
            assert actual[key] == value


def as_kwargs(request: VpeRequest) -> dict[str, Any]:
    """Copies a request into constructor keyword arguments.

    ``dataclasses.asdict`` would recurse into the media references and turn
    them into plain dicts, so the fields are read one by one instead. The
    refusal tests use this to vary one field of a known-good request.

    Args:
        request: The request to copy.

    Returns:
        Keyword arguments reproducing it.
    """
    return {
        name: getattr(request, name) for name in VpeRequest.__dataclass_fields__
    }


def lookup(payload: dict[str, Any], json_path: str) -> Any:
    """Reads a dotted path out of a payload.

    Args:
        payload: A built request body.
        json_path: A dotted path such as
            ``parameters.experiments.modelName``.

    Returns:
        The value at that path.

    Raises:
        KeyError: If any segment is missing.
    """
    node: Any = payload
    for segment in json_path.split("."):
        node = node[segment]
    return node


def leaves(node: Any) -> list[Any]:
    """Flattens a payload into its scalar leaves.

    Args:
        node: Any part of a built payload.

    Returns:
        Every scalar reachable from the node.
    """
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in leaves(value)]
    if isinstance(node, list):
        return [leaf for item in node for leaf in leaves(item)]
    return [node]


def maximal_request(capability: VpeCapability) -> VpeRequest:
    """Builds a request exercising every field a capability declares.

    Args:
        capability: The registry entry to saturate.

    Returns:
        A request carrying every accepted instance field and parameter.
    """
    fields: dict[str, Any] = {
        "capability_id": capability.capability_id,
        "storage_uri": OUTPUT_DIR,
    }
    for spec in capability.instance_fields:
        if spec.field is VpeInstanceField.PROMPT:
            fields["prompt"] = "A prompt"
            continue
        ref = VpeMediaRef(
            gcs_uri=f"{BUCKET}/in/media",
            mime_type=spec.mime_types[0],
        )
        if spec.field is VpeInstanceField.VIDEO:
            fields["video"] = ref
        elif spec.field is VpeInstanceField.IMAGE:
            fields["image"] = ref
        elif spec.field is VpeInstanceField.LAST_FRAME:
            fields["last_frame"] = ref
        elif spec.field is VpeInstanceField.REFERENCE_IMAGES:
            fields["reference_images"] = (ref,)
        elif spec.field is VpeInstanceField.REFERENCE_AUDIOS:
            fields["reference_audios"] = (ref,)
    for spec in capability.parameters:
        attribute = PARAMETER_ATTRIBUTES[(spec.name, spec.location)]
        if attribute is not None:
            fields[attribute] = MAXIMAL_VALUES[attribute]
    return VpeRequest(**fields)


# --- Dispatch and cross-capability invariants ----------------------------


@ALL_CAPABILITIES
def test_every_capability_has_a_builder(capability: VpeCapability):
    payload = build_payload(MINIMAL_REQUESTS[capability.capability_id])
    assert set(payload) == {"instances", "parameters"}
    assert len(payload["instances"]) == 1
    assert (
        payload["parameters"]["experiments"]["modelName"]
        == capability.model_name
    )
    assert payload["parameters"]["storageUri"] == OUTPUT_DIR


@ALL_CAPABILITIES
def test_payload_is_json_serializable(capability: VpeCapability):
    """The client posts this straight to REST, so it must be plain JSON."""
    payload = build_payload(maximal_request(capability))
    assert json.loads(json.dumps(payload)) == payload


@ALL_CAPABILITIES
def test_payload_never_carries_nulls(capability: VpeCapability):
    """An absent option is an absent key, never an explicit null."""
    payload = build_payload(MINIMAL_REQUESTS[capability.capability_id])
    assert None not in leaves(payload)


@ALL_CAPABILITIES
def test_enum_inputs_are_written_as_plain_strings(capability: VpeCapability):
    """Registry enums may be passed in, but must not reach the wire."""
    payload = build_payload(maximal_request(capability))
    assert all(
        type(leaf) in (str, int, float, bool) for leaf in leaves(payload)
    )


@ALL_CAPABILITIES
def test_every_declared_parameter_is_reachable(capability: VpeCapability):
    """No registry parameter may be unreachable from a request field.

    A parameter that exists in the registry but that no request field feeds
    is a capability the app silently cannot use.
    """
    payload = build_payload(maximal_request(capability))
    for spec in capability.parameters:
        assert lookup(payload, spec.json_path) is not None, spec.json_path


@ALL_CAPABILITIES
def test_every_declared_instance_field_is_reachable(
    capability: VpeCapability,
):
    payload = build_payload(maximal_request(capability))
    instance = payload["instances"][0]
    for spec in capability.instance_fields:
        assert spec.field.value in instance


def test_zero_valued_parameters_are_sent_not_swallowed():
    """A sharpness of 0 and a seed of 0 are requests, not absences."""
    upscale = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=OUTPUT_DIR,
            video=MP4,
            sharpness=0,
        ),
    )
    transform = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
            storage_uri=OUTPUT_DIR,
            prompt="A prompt",
            video=MP4,
            seed=0,
        ),
    )

    assert upscale["parameters"]["sharpness"] == 0
    assert transform["parameters"]["seed"] == 0


def test_capability_id_may_be_a_plain_string():
    """The DTO layer will hand this module a string off the wire."""
    request = VpeRequest(
        capability_id="upscale",  # type: ignore[arg-type]
        storage_uri=OUTPUT_DIR,
        video=MP4,
    )
    assert (
        lookup(
            build_payload(request),
            "parameters.experiments.modelName",
        )
        == "veo3p1_upscale"
    )


def test_unknown_capability_id_is_rejected():
    request = VpeRequest(
        capability_id="deblur",  # type: ignore[arg-type]
        storage_uri=OUTPUT_DIR,
        video=MP4,
    )
    with pytest.raises(ValueError, match="Unknown VPE capability"):
        build_payload(request)


# --- Nothing is invented, nothing is dropped -----------------------------


def test_optional_parameters_are_omitted_not_defaulted():
    """A 720p 'default' resolution would upscale nothing; never send it."""
    payload = build_payload(MINIMAL_REQUESTS[VpeCapabilityId.UPSCALE])

    assert payload["parameters"] == {
        "task": "upscale",
        "storageUri": OUTPUT_DIR,
        "experiments": {"modelName": "veo3p1_upscale"},
    }


def test_seamless_upscale_defaults_resolution_to_4k():
    """4K is the only resolution documented for a seamless source."""
    payload = build_payload(
        MINIMAL_REQUESTS[VpeCapabilityId.UPSCALE_SEAMLESS],
    )

    assert payload["parameters"]["resolution"] == "4k"


@pytest.mark.parametrize(
    "capability_id",
    [VpeCapabilityId.UPSCALE_SEAMLESS, VpeCapabilityId.VIDEO_TEXTURES],
)
def test_seamless_flags_are_always_sent_in_full(
    capability_id: VpeCapabilityId,
):
    """A missing flag makes the upscaler apply the wrong wrapping."""
    payload = build_payload(MINIMAL_REQUESTS[capability_id])

    assert lookup(payload, "parameters.experiments.seamless") == {
        "loop": False,
        "tessellateHorizontal": False,
        "tessellateVertical": False,
    }


def test_reference_type_casing_differs_between_capabilities():
    """Lowercase in the omni-cine sample, uppercase in perf-generation."""
    omni = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=OUTPUT_DIR,
            prompt="A prompt",
            reference_images=(PNG,),
        ),
    )
    perf = build_payload(MINIMAL_REQUESTS[VpeCapabilityId.PERF_GENERATION])

    assert (
        omni["instances"][0]["referenceImages"][0]["referenceType"] == "asset"
    )
    assert (
        perf["instances"][0]["referenceImages"][0]["referenceType"] == "ASSET"
    )


def test_reference_audios_carry_no_reference_type():
    payload = build_payload(MINIMAL_REQUESTS[VpeCapabilityId.DIALOGUE_DRIVEN])

    assert payload["instances"][0]["referenceAudios"] == [
        {
            "audio": {
                "gcsUri": f"{BUCKET}/in/line.wav",
                "mimeType": "audio/wav",
            },
        },
    ]


def test_frame_sequence_glob_is_passed_through_unchanged():
    """A PNG sequence is a wildcard gcsUri plus the frame's mime type."""
    glob = VpeMediaRef(
        gcs_uri=f"{BUCKET}/in/frames/*.png",
        mime_type="image/png",
    )
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=OUTPUT_DIR,
            video=glob,
        ),
    )

    assert glob.is_sequence
    assert not MP4.is_sequence
    assert payload["instances"][0]["video"] == {
        "gcsUri": f"{BUCKET}/in/frames/*.png",
        "mimeType": "image/png",
    }


def test_mime_types_are_sent_in_their_documented_spelling():
    """Every mime type in the docs is spelled once, in lower case.

    The field check accepts any casing, so a reference carrying "Video/MP4"
    passes every local check; it must not then be handed to the service in
    a spelling no doc page uses.
    """
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=OUTPUT_DIR,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/in/clip.mp4",
                mime_type="Video/MP4",
            ),
        ),
    )

    assert payload["instances"][0]["video"]["mimeType"] == "video/mp4"


def test_inline_bytes_are_written_as_base64_for_dialogue_driven():
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
            storage_uri=OUTPUT_DIR,
            prompt="A prompt",
            image=VpeMediaRef(bytes_base64="aW1n", mime_type="image/png"),
            reference_audios=(
                VpeMediaRef(bytes_base64="d2F2", mime_type="audio/wav"),
            ),
        ),
    )

    assert payload["instances"][0]["image"] == {
        "bytesBase64Encoded": "aW1n",
        "mimeType": "image/png",
    }
    assert payload["instances"][0]["referenceAudios"][0]["audio"] == {
        "bytesBase64Encoded": "d2F2",
        "mimeType": "audio/wav",
    }


@pytest.mark.parametrize("codec", ["prores", "dnxhr"])
def test_upscaler_sends_a_professional_codec_at_4k(codec: str):
    """A 4K upscale can ask for ProRes or DNxHR.

    The tessellation page says the codec "is specified by
    parameters.experiments.codec", and both the upscaler and
    professional-formats pages list ProRes and DNxHR among the 4K outputs,
    so refusing the parameter would make a documented format unreachable.
    """
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=OUTPUT_DIR,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/video.mp4",
                mime_type="video/mp4",
            ),
            resolution="4k",
            aspect_ratio="16:9",
            codec=codec,
        ),
    )

    assert lookup(payload, "parameters.experiments.codec") == codec
    # It belongs in experiments, unlike the upscaler's other knobs.
    assert "codec" not in payload["parameters"]


@pytest.mark.parametrize("resolution", [None, "1080p"])
def test_professional_codec_is_refused_below_4k(resolution: str | None):
    """Only the 4K path emits ProRes or DNxHR.

    The upscaler's output table gives 720p-to-1080p as "H264 (video/mp4)"
    alone, while only the 720p-to-4K row carries ProRes and DNxHR. An
    omitted resolution counts, because the documented default is 720p.
    """
    with pytest.raises(VpePayloadError, match="only available at 4k"):
        build_payload(
            VpeRequest(
                capability_id=VpeCapabilityId.UPSCALE,
                storage_uri=OUTPUT_DIR,
                video=VpeMediaRef(
                    gcs_uri=f"{BUCKET}/inputs/video.mp4",
                    mime_type="video/mp4",
                ),
                resolution=resolution,
                aspect_ratio="16:9",
                codec="prores",
            ),
        )


def test_h264_is_allowed_at_any_upscale_resolution():
    """H.264 is the one codec both output paths share."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri=OUTPUT_DIR,
            video=VpeMediaRef(
                gcs_uri=f"{BUCKET}/inputs/video.mp4",
                mime_type="video/mp4",
            ),
            resolution="1080p",
            aspect_ratio="16:9",
            codec="h264",
        ),
    )

    assert lookup(payload, "parameters.experiments.codec") == "h264"


def test_seamless_needs_a_delivery_format_that_can_carry_it():
    """Refused live unless the output format supports tiling.

    "Seamless tiling is only supported when the compressionQuality
    parameter is set to lossless_16bit_png, or when using ProRes or DNxHR
    codecs." No prose documents this; only a live rejection revealed it.
    """
    with pytest.raises(VpePayloadError, match="seamless output needs"):
        build_payload(
            VpeRequest(
                capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
                storage_uri=OUTPUT_DIR,
                video=MP4,
                seamless=VpeSeamlessFlags(loop=True),
            ),
        )


@pytest.mark.parametrize(
    ("quality", "codec"),
    [
        ("lossless_16bit_png", None),
        (None, "prores"),
        (None, "dnxhr"),
    ],
)
def test_seamless_accepts_each_documented_carrier(
    quality: str | None,
    codec: str | None,
):
    """Each of the three the rejection names is sufficient on its own."""
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
            storage_uri=OUTPUT_DIR,
            video=MP4,
            compression_quality=quality,
            codec=codec,
            seamless=VpeSeamlessFlags(loop=True),
        ),
    )

    assert payload["parameters"]["experiments"]["seamless"]["loop"] is True


def test_registry_enums_may_be_passed_instead_of_strings():
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=OUTPUT_DIR,
            prompt="A prompt",
            codec=VpeCodec.PRORES,
            compression_quality=VpeCompressionQuality.LOSSLESS,
        ),
    )

    codec = lookup(payload, "parameters.experiments.codec")
    quality = lookup(payload, "parameters.compressionQuality")
    assert (codec, type(codec)) == ("prores", str)
    assert (quality, type(quality)) == ("lossless", str)


# --- Refusals ------------------------------------------------------------


@pytest.mark.parametrize(
    ("capability_id", "field", "value", "expected"),
    [
        # Omni-cine documents seed as unsupported.
        (VpeCapabilityId.OMNI_CINE, "seed", 7, "parameters.seed"),
        # The plain upscaler is a different entry from the seamless one.
        (
            VpeCapabilityId.UPSCALE,
            "seamless",
            VpeSeamlessFlags(loop=True),
            "seamless",
        ),
        # The blue mesh belongs to performance generation alone.
        (
            VpeCapabilityId.VIDEO_TRANSFORM,
            "perf_mesh_gcs_uri",
            MESH,
            "perfMeshGcsUri",
        ),
        # Keyframes belong to video transform alone.
        (
            VpeCapabilityId.VIDEO_TEXTURES,
            "conditioning_frames",
            (VpeConditioningFrame(image=PNG, frame_num=8),),
            "conditioningFrames",
        ),
        # Video textures is text or image driven; it takes no video.
        (VpeCapabilityId.VIDEO_TEXTURES, "video", MP4, "instances[].video"),
        # Dialogue-driven conditions the first frame only.
        (
            VpeCapabilityId.DIALOGUE_DRIVEN,
            "last_frame",
            PNG,
            "instances[].lastFrame",
        ),
        # The estimation model ignores prompts, so it must not take one.
        (
            VpeCapabilityId.PERF_ESTIMATION,
            "prompt",
            "A prompt",
            "instances[].prompt",
        ),
        # Only omni-cine takes an input video plus stills.
        (
            VpeCapabilityId.PERF_GENERATION,
            "video",
            MP4,
            "instances[].video",
        ),
        # Only the upscaler resizes.
        (
            VpeCapabilityId.VIDEO_TRANSFORM,
            "resolution",
            "4k",
            "parameters.resolution",
        ),
    ],
)
def test_unsupported_input_is_refused_not_dropped(
    capability_id: VpeCapabilityId,
    field: str,
    value: Any,
    expected: str,
):
    """A value the capability cannot express must fail loudly."""
    request = VpeRequest(
        **{**as_kwargs(MINIMAL_REQUESTS[capability_id]), field: value},
    )

    with pytest.raises(VpePayloadError, match=expected.replace("[", r"\[")):
        build_payload(request)


@pytest.mark.parametrize(
    ("capability_id", "field"),
    [
        (VpeCapabilityId.UPSCALE, "video"),
        (VpeCapabilityId.PERF_ESTIMATION, "video"),
        (VpeCapabilityId.OMNI_CINE, "prompt"),
        (VpeCapabilityId.VIDEO_TRANSFORM, "prompt"),
        (VpeCapabilityId.DIALOGUE_DRIVEN, "image"),
        (VpeCapabilityId.DIALOGUE_DRIVEN, "reference_audios"),
        (VpeCapabilityId.PERF_GENERATION, "reference_images"),
    ],
)
def test_required_input_must_be_present(
    capability_id: VpeCapabilityId,
    field: str,
):
    empty: Any = () if field.startswith("reference") else None
    request = VpeRequest(
        **{**as_kwargs(MINIMAL_REQUESTS[capability_id]), field: empty},
    )

    with pytest.raises(VpePayloadError, match="requires"):
        build_payload(request)


def test_performance_generation_requires_a_blue_mesh():
    request = VpeRequest(
        **{
            **as_kwargs(MINIMAL_REQUESTS[VpeCapabilityId.PERF_GENERATION]),
            "perf_mesh_gcs_uri": None,
        },
    )

    with pytest.raises(VpePayloadError, match="perfMeshGcsUri"):
        build_payload(request)


def test_video_textures_needs_a_prompt_or_an_image():
    """Its prompt is conditional, so emptiness has to be caught here."""
    request = VpeRequest(
        capability_id=VpeCapabilityId.VIDEO_TEXTURES,
        storage_uri=OUTPUT_DIR,
    )

    with pytest.raises(VpePayloadError, match="prompt"):
        build_payload(request)


@pytest.mark.parametrize(
    ("capability_id", "field", "value"),
    [
        # Seven reference images for omni-cine, five for perf generation.
        (
            VpeCapabilityId.OMNI_CINE,
            "reference_images",
            tuple([PNG] * 8),
        ),
        (
            VpeCapabilityId.PERF_GENERATION,
            "reference_images",
            tuple([PNG] * 6),
        ),
        # Only the first reference audio is used, so only one is accepted.
        (
            VpeCapabilityId.DIALOGUE_DRIVEN,
            "reference_audios",
            (WAV, WAV),
        ),
    ],
)
def test_array_inputs_respect_their_documented_limits(
    capability_id: VpeCapabilityId,
    field: str,
    value: Any,
):
    request = VpeRequest(
        **{**as_kwargs(MINIMAL_REQUESTS[capability_id]), field: value},
    )

    with pytest.raises(VpePayloadError, match="at most"):
        build_payload(request)


@pytest.mark.parametrize(
    ("capability_id", "field", "ref"),
    [
        # Performance estimation is the only H264-only input.
        (
            VpeCapabilityId.PERF_ESTIMATION,
            "video",
            VpeMediaRef(
                gcs_uri=f"{BUCKET}/in/clip.mov",
                mime_type="video/quicktime",
            ),
        ),
        # EXR is an omni-cine input only.
        (
            VpeCapabilityId.VIDEO_TRANSFORM,
            "image",
            VpeMediaRef(
                gcs_uri=f"{BUCKET}/in/still.exr",
                mime_type="image/x-exr",
            ),
        ),
        # A still is not a video, whatever the field is called.
        (
            VpeCapabilityId.UPSCALE,
            "video",
            VpeMediaRef(
                gcs_uri=f"{BUCKET}/in/still.jpg",
                mime_type="image/jpeg",
            ),
        ),
    ],
)
def test_mime_type_must_be_documented_for_the_field(
    capability_id: VpeCapabilityId,
    field: str,
    ref: VpeMediaRef,
):
    request = VpeRequest(
        **{**as_kwargs(MINIMAL_REQUESTS[capability_id]), field: ref},
    )

    with pytest.raises(VpePayloadError, match="accepts"):
        build_payload(request)


def test_inline_bytes_are_refused_where_only_gcs_is_documented():
    """Only dialogue-driven documents bytesBase64Encoded."""
    request = VpeRequest(
        **{
            **as_kwargs(MINIMAL_REQUESTS[VpeCapabilityId.VIDEO_TRANSFORM]),
            "image": VpeMediaRef(
                bytes_base64="aW1n",
                mime_type="image/png",
            ),
        },
    )

    with pytest.raises(VpePayloadError, match="inline bytes"):
        build_payload(request)


@pytest.mark.parametrize(
    ("capability_id", "field", "value"),
    [
        # sharpness is [0, 4].
        (VpeCapabilityId.UPSCALE, "sharpness", 5),
        (VpeCapabilityId.UPSCALE, "sharpness", -1),
        # 720p is the documented default and upscales nothing; it is also
        # not one of the two accepted values.
        (VpeCapabilityId.UPSCALE, "resolution", "720p"),
        (VpeCapabilityId.UPSCALE, "aspect_ratio", "1:1"),
        # The seamless path is 4K only.
        (VpeCapabilityId.UPSCALE_SEAMLESS, "resolution", "1080p"),
        # strength is (0.0, 1.0]: zero is excluded, one is not.
        (VpeCapabilityId.VIDEO_TRANSFORM, "video_transform_strength", 0.0),
        (VpeCapabilityId.VIDEO_TRANSFORM, "video_transform_strength", 1.5),
        (VpeCapabilityId.VIDEO_TRANSFORM, "num_diffusion_steps", 0),
        (VpeCapabilityId.VIDEO_TRANSFORM, "num_diffusion_steps", 251),
        (VpeCapabilityId.VIDEO_TRANSFORM, "seed", -1),
        (VpeCapabilityId.VIDEO_TRANSFORM, "seed", 4294967296),
        (VpeCapabilityId.VIDEO_TRANSFORM, "codec", "vp9"),
        # lossless_16bit_png is upscaler-only.
        (
            VpeCapabilityId.OMNI_CINE,
            "compression_quality",
            "lossless_16bit_png",
        ),
    ],
)
def test_scalar_values_outside_the_documented_domain_are_refused(
    capability_id: VpeCapabilityId,
    field: str,
    value: Any,
):
    request = VpeRequest(
        **{**as_kwargs(MINIMAL_REQUESTS[capability_id]), field: value},
    )

    with pytest.raises(VpePayloadError):
        build_payload(request)


def test_strength_of_one_is_accepted():
    """The range is (0.0, 1.0]; the upper bound is inclusive."""
    request = VpeRequest(
        **{
            **as_kwargs(MINIMAL_REQUESTS[VpeCapabilityId.VIDEO_TRANSFORM]),
            "video_transform_strength": 1.0,
        },
    )

    assert (
        lookup(
            build_payload(request),
            "parameters.experiments.videoTransformStrength",
        )
        == 1.0
    )


def test_upscaler_accepts_lossless_16bit_png_at_4k():
    """The one capability the 16-bit PNG quality is documented for.

    The upscaler sample pairs it with 4k, which is the only resolution the
    doc offers it at.
    """
    request = VpeRequest(
        **{
            **as_kwargs(MINIMAL_REQUESTS[VpeCapabilityId.UPSCALE]),
            "compression_quality": "lossless_16bit_png",
            "resolution": "4k",
        },
    )

    assert (
        lookup(build_payload(request), "parameters.compressionQuality")
        == "lossless_16bit_png"
    )


@pytest.mark.parametrize("resolution", [None, "1080p"])
def test_lossless_16bit_png_is_refused_below_4k(resolution: str | None):
    """upscaler: "lossless_16bit_png is only available at 4k resolution".

    An omitted resolution is a violation too: the documented default is
    720p, so the job would deliver 8-bit output at the source size.
    """
    request = VpeRequest(
        **{
            **as_kwargs(MINIMAL_REQUESTS[VpeCapabilityId.UPSCALE]),
            "compression_quality": "lossless_16bit_png",
            "resolution": resolution,
        },
    )

    with pytest.raises(VpePayloadError, match="only available at 4k"):
        build_payload(request)


def test_video_transform_needs_a_video_or_a_first_frame():
    """video-transform: instances[].video is documented "Required".

    The one sample that omits it conditions on instances[].image instead,
    so a prompt on its own is text-to-video, which this model never offers.
    """
    request = VpeRequest(
        capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
    )

    with pytest.raises(VpePayloadError, match="video or a first-frame image"):
        build_payload(request)


def test_video_transform_mask_needs_the_video_it_masks():
    """video-transform: "Masked video transform (V2V)".

    The mask is "a grayscale video matching the input video's resolution
    and duration", so a mask handed in beside a first frame alone has
    nothing to scale and would be silently ignored.
    """
    request = VpeRequest(
        capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
        image=PNG,
        video_transform_mask_gcs_uri=f"{BUCKET}/in/mask.mp4",
    )

    with pytest.raises(VpePayloadError, match="instances\\[\\].video"):
        build_payload(request)


def test_omni_cine_exr_reference_needs_an_exr_input_video():
    """omni-cine: "EXR: image/x-exr (requires EXR input video)"."""
    exr = VpeMediaRef(gcs_uri=f"{BUCKET}/in/ref.exr", mime_type="image/x-exr")
    request = VpeRequest(
        capability_id=VpeCapabilityId.OMNI_CINE,
        storage_uri=OUTPUT_DIR,
        prompt="A prompt",
        video=MP4,
        reference_images=(exr,),
    )

    with pytest.raises(VpePayloadError, match="EXR input video"):
        build_payload(request)

    with pytest.raises(VpePayloadError, match="EXR input video"):
        build_payload(
            VpeRequest(
                **{**as_kwargs(request), "video": None},
            ),
        )


def test_omni_cine_exr_reference_is_accepted_beside_an_exr_video():
    """The pairing the omni-cine sample itself sends."""
    exr_video = VpeMediaRef(
        gcs_uri=f"{BUCKET}/in/plate/*.exr",
        mime_type="image/x-exr",
    )
    exr_reference = VpeMediaRef(
        gcs_uri=f"{BUCKET}/in/ref.exr",
        mime_type="image/x-exr",
    )
    payload = build_payload(
        VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=OUTPUT_DIR,
            prompt="A prompt",
            video=exr_video,
            reference_images=(exr_reference,),
        ),
    )

    assert payload["instances"][0]["referenceImages"][0]["image"] == {
        "gcsUri": f"{BUCKET}/in/ref.exr",
        "mimeType": "image/x-exr",
    }


@pytest.mark.parametrize(
    ("capability_id", "field"),
    [
        (VpeCapabilityId.VIDEO_TRANSFORM, "video_transform_mask_gcs_uri"),
        (VpeCapabilityId.PERF_GENERATION, "perf_mesh_gcs_uri"),
    ],
)
def test_uri_parameters_must_point_at_cloud_storage(
    capability_id: VpeCapabilityId,
    field: str,
):
    """These two arrive as bare strings, not as a checked media reference.

    Every VPE input is read from Cloud Storage; the user guide says direct
    upload is not supported.
    """
    request = VpeRequest(
        **{
            **as_kwargs(MINIMAL_REQUESTS[capability_id]),
            field: "https://example.test/clip.mp4",
        },
    )

    with pytest.raises(VpePayloadError, match="Cloud Storage"):
        build_payload(request)


@pytest.mark.parametrize(
    "capability_id",
    [
        VpeCapabilityId.OMNI_CINE,
        VpeCapabilityId.VIDEO_TRANSFORM,
        VpeCapabilityId.DIALOGUE_DRIVEN,
        VpeCapabilityId.PERF_GENERATION,
    ],
)
def test_a_blank_prompt_does_not_satisfy_a_required_prompt(
    capability_id: VpeCapabilityId,
):
    """Whitespace is not "a text string to guide the video generation"."""
    request = VpeRequest(
        **{**as_kwargs(MINIMAL_REQUESTS[capability_id]), "prompt": "   \n"},
    )

    with pytest.raises(VpePayloadError, match="requires"):
        build_payload(request)


def test_a_blank_prompt_alone_does_not_satisfy_video_textures():
    """Its prompt is conditional, so blankness has to be caught by the guard."""
    request = VpeRequest(
        capability_id=VpeCapabilityId.VIDEO_TEXTURES,
        storage_uri=OUTPUT_DIR,
        prompt="   ",
    )

    with pytest.raises(VpePayloadError, match="prompt"):
        build_payload(request)


@pytest.mark.parametrize("frame_num", [0, 7, 12, 192, 184 + 8])
def test_conditioning_frame_numbers_must_be_documented_multiples(
    frame_num: int,
):
    """frameNum runs 8, 16, 24, ... 184."""
    with pytest.raises(VpePayloadError, match="frameNum"):
        VpeConditioningFrame(image=PNG, frame_num=frame_num)


@pytest.mark.parametrize("frame_num", [8, 24, 128, 184])
def test_conditioning_frame_numbers_in_range_are_accepted(frame_num: int):
    assert VpeConditioningFrame(image=PNG, frame_num=frame_num).frame_num


def test_media_reference_needs_exactly_one_source():
    with pytest.raises(VpePayloadError, match="exactly one"):
        VpeMediaRef(mime_type="image/png")
    with pytest.raises(VpePayloadError, match="exactly one"):
        VpeMediaRef(
            mime_type="image/png",
            gcs_uri=f"{BUCKET}/in/still.png",
            bytes_base64="aW1n",
        )


def test_media_reference_must_point_at_cloud_storage():
    with pytest.raises(VpePayloadError, match="Cloud Storage"):
        VpeMediaRef(
            mime_type="image/png",
            gcs_uri="https://example.test/still.png",
        )


def test_storage_uri_must_point_at_cloud_storage():
    with pytest.raises(VpePayloadError, match="Cloud Storage"):
        VpeRequest(
            capability_id=VpeCapabilityId.UPSCALE,
            storage_uri="/tmp/outputs",
            video=MP4,
        )


def test_registry_lookup_agrees_with_the_builders():
    """The endpoint is shared; only modelName selects the checkpoint."""
    for capability in VPE_CAPABILITY_LIST:
        payload = build_payload(MINIMAL_REQUESTS[capability.capability_id])
        assert (
            lookup(
                payload,
                "parameters.experiments.modelName",
            )
            == get_capability(capability.capability_id).model_name
        )
