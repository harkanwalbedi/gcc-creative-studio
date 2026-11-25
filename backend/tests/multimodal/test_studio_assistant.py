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
"""Tests for the Creative Studio & GMP Q&A Assistant."""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.auth.auth_guard import get_current_user
from src.multimodal.dto.studio_assistant_dto import (
    AssistantHistoryMessageDto,
    StudioAssistantRequestDto,
    StudioAssistantResponseDto,
)
from src.multimodal.gemini_controller import router as gemini_router
from src.multimodal.gemini_service import GeminiService
from src.multimodal.studio_knowledge import build_studio_system_instruction
from src.users.user_model import UserModel, UserRoleEnum


def test_build_studio_system_instruction_includes_gmp_specs():
    build_studio_system_instruction.cache_clear()
    enabled_text = build_studio_system_instruction(True)
    assert "ENABLED (`VPE_ENABLED=true`)" in enabled_text
    assert "Omni-Cine" in enabled_text
    assert "veo3p1_upscale" in enabled_text

    build_studio_system_instruction.cache_clear()
    disabled_text = build_studio_system_instruction(False)
    assert "DISABLED (`VPE_ENABLED=false`" in disabled_text


def test_answer_studio_question_primary_model():
    with (
        patch("src.multimodal.gemini_service.GeminiModelSetup.init"),
        patch(
            "src.multimodal.gemini_service.GeminiModelSetup.get_global_client"
        ) as mock_get_global,
    ):
        mock_global_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Use **Omni-Perf** with a 2D Blue Mesh clip."
        mock_global_client.models.generate_content.return_value = mock_response
        mock_get_global.return_value = mock_global_client

        service = GeminiService()
        req = StudioAssistantRequestDto(
            question="How does Omni-Perf work?",
            current_route="/video",
            active_model="omni-perf",
            active_mode="Ingredients to Video",
            history=[
                AssistantHistoryMessageDto(role="user", content="Hi"),
                AssistantHistoryMessageDto(
                    role="assistant", content="Hello! Ask away."
                ),
            ],
        )

        result = service.answer_studio_question(req)

        assert result.answer == "Use **Omni-Perf** with a 2D Blue Mesh clip."
        assert result.model_used == "gemini-3-flash-preview"
        mock_global_client.models.generate_content.assert_called_once()


def test_answer_studio_question_falls_back_on_preview_error():
    with (
        patch("src.multimodal.gemini_service.GeminiModelSetup.init"),
        patch(
            "src.multimodal.gemini_service.GeminiModelSetup.get_global_client"
        ) as mock_get_global,
    ):
        mock_global_client = MagicMock()
        fallback_response = MagicMock()
        fallback_response.text = ""
        mock_global_client.models.generate_content.side_effect = [
            RuntimeError("Preview model unavailable"),
            fallback_response,
        ]
        mock_get_global.return_value = mock_global_client

        service = GeminiService()
        req = StudioAssistantRequestDto(question="What is 4K upscale?")
        result = service.answer_studio_question(req)

        assert result.model_used == "gemini-2.5-flash"
        assert "Please try rephrasing it" in result.answer
        assert mock_global_client.models.generate_content.call_count == 2


def test_answer_studio_question_raises_when_all_models_fail():
    with (
        patch("src.multimodal.gemini_service.GeminiModelSetup.init"),
        patch(
            "src.multimodal.gemini_service.GeminiModelSetup.get_global_client"
        ) as mock_get_global,
    ):
        mock_global_client = MagicMock()
        mock_global_client.models.generate_content.side_effect = RuntimeError(
            "Quota exceeded"
        )
        mock_get_global.return_value = mock_global_client

        service = GeminiService()
        req = StudioAssistantRequestDto(question="Help")

        with pytest.raises(RuntimeError, match="All assistant models failed"):
            service.answer_studio_question(req)


def test_studio_assistant_controller_endpoint():
    app = FastAPI()
    app.include_router(gemini_router)

    mock_user = UserModel(
        id=1,
        email="tester@example.com",
        name="Tester",
        roles=[UserRoleEnum.USER],
    )
    mock_service = MagicMock(spec=GeminiService)
    mock_service.answer_studio_question.return_value = (
        StudioAssistantResponseDto(
            answer="Select Veo Dialogue Lip Sync in /video.",
            model_used="gemini-3-flash-preview",
        )
    )

    app.dependency_overrides[get_current_user] = lambda: mock_user
    app.dependency_overrides[GeminiService] = lambda: mock_service

    client = TestClient(app)
    resp = client.post(
        "/api/gemini/assistant",
        json={
            "question": "How do I use lip sync?",
            "currentRoute": "/gmp",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Select Veo Dialogue Lip Sync in /video."
    assert body["modelUsed"] == "gemini-3-flash-preview"

    mock_service.answer_studio_question.side_effect = RuntimeError("boom")
    err_resp = client.post(
        "/api/gemini/assistant",
        json={"question": "Fail please"},
    )
    assert err_resp.status_code == 500
