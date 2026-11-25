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
"""Request shape for a VPE upscale."""

from typing import Literal

from pydantic import Field, model_validator

from src.common.base_dto import BaseDto
from src.videos.vpe.capabilities import (
    VpeCapabilityId,
    VpeUpscaleResolution,
    get_capability,
)

# Read off the registry rather than written out again here, so the bounds the
# form enforces cannot drift from the bounds the payload builder enforces.
_SHARPNESS = get_capability(VpeCapabilityId.UPSCALE).parameter("sharpness")


class UpscaleVideoDto(BaseDto):
    """Data Transfer Object for upscaling an existing gallery video."""

    workspace_id: int = Field(
        ge=1,
        description="The ID of the workspace for this job.",
    )
    media_item_id: int = Field(
        ge=1,
        description="The gallery video to upscale.",
    )
    media_index: int = Field(
        default=0,
        ge=0,
        description=(
            "Which clip of that media item to upscale, for rows holding more"
            " than one."
        ),
    )
    resolution: VpeUpscaleResolution = Field(
        default=VpeUpscaleResolution.UHD_4K,
        description=(
            "Target resolution (legacy 1080p / 4k). Kept for backward"
            " compatibility when upscaleMode is omitted."
        ),
    )
    upscale_mode: Literal["1080p", "4k", "4k_pro", "8k"] | None = Field(
        default=None,
        description=(
            "GMP upscale tier ('1080p', '4k', '4k_pro', '8k'). Overrides"
            " resolution when provided."
        ),
    )
    sharpness: int | None = Field(
        default=None,
        ge=_SHARPNESS.minimum,
        le=_SHARPNESS.maximum,
        description=(
            "Optional sharpening strength, higher being sharper. Left unset,"
            " the parameter is not sent and the model uses its own default."
        ),
    )
    steps: Literal[1, 2] | None = Field(
        default=None,
        description="Optional diffusion steps for GMP Super Resolution (1 or 2).",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=4294967295,
        description="Optional deterministic seed (0 to 4294967295).",
    )
    output_codec: Literal["h264", "prores", "dnxhr"] = Field(
        default="h264",
        description="Output container/codec: h264, prores (.mov), or dnxhr (.mxf).",
    )
    compression_quality: Literal["optimized", "lossless"] = Field(
        default="optimized",
        description="Compression quality profile: optimized or lossless.",
    )

    @property
    def resolved_mode(self) -> Literal["1080p", "4k", "4k_pro", "8k"]:
        """Returns the effective upscale mode, falling back to resolution."""
        if self.upscale_mode is not None:
            return self.upscale_mode
        if self.resolution == VpeUpscaleResolution.HD_1080P:
            return "1080p"
        return "4k"

    @model_validator(mode="after")
    def validate_codec_and_mode(self) -> "UpscaleVideoDto":
        """Enforces GMP Super Resolution codec and compression rules."""
        if (
            self.compression_quality == "lossless"
            and self.output_codec == "h264"
        ):
            raise ValueError(
                "Lossless compressionQuality requires output_codec 'prores' or"
                " 'dnxhr'."
            )
        if (
            self.resolved_mode == "8k"
            and self.output_codec == "dnxhr"
            and self.compression_quality == "lossless"
        ):
            raise ValueError(
                "8K lossless DNxHR is not supported; use 'prores' or"
                " 'optimized'."
            )
        return self
