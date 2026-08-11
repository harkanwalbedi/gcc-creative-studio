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

from pydantic import Field

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
    """Data Transfer Object for upscaling an existing gallery video.

    Deliberately thin. The upscaler takes a clip and gives back the same clip
    with more pixels, so there is no prompt, no seed and no aspect ratio to
    choose: the ratio is whatever the source measures, and sending anything
    else is rejected by the API.
    """

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
            "Target resolution. The API's own default is 720p, which upscales"
            " nothing, so this defaults to 4K instead."
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
