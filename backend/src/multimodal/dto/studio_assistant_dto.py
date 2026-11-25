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
"""DTOs for the Creative Studio & GMP Q&A Assistant endpoint."""

from typing import Literal

from pydantic import Field

from src.common.base_dto import BaseDto


class AssistantHistoryMessageDto(BaseDto):
    """A single prior turn in the Studio Assistant chat history."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class StudioAssistantRequestDto(BaseDto):
    """Request payload for asking the Studio & GMP Guide a question."""

    question: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "The user's question about Creative Studio or GMP features."
        ),
    )
    current_route: str = Field(
        default="/",
        max_length=200,
        description=(
            "The active frontend route/tab where the user is asking from."
        ),
    )
    active_model: str = Field(
        default="",
        max_length=120,
        description="Currently selected generation model in the UI, if any.",
    )
    active_mode: str = Field(
        default="",
        max_length=120,
        description="Currently selected generation mode in the UI, if any.",
    )
    history: list[AssistantHistoryMessageDto] = Field(
        default_factory=list,
        max_length=12,
        description="Recent conversation turns for multi-turn context.",
    )


class StudioAssistantResponseDto(BaseDto):
    """Response returned by the Studio & GMP Guide."""

    answer: str
    model_used: str
