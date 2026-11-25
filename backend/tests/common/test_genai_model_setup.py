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
"""Tests for GenAIModelSetup multi-project client factory."""

from unittest.mock import MagicMock, patch
import pytest

from src.common.schema.genai_model_setup import GenAIModelSetup
from src.multimodal.schema.gemini_model_setup import GeminiModelSetup


@pytest.fixture(autouse=True)
def clean_clients():
    """Clears cached clients before and after each test."""
    GenAIModelSetup._clients.clear()
    GenAIModelSetup._global_clients.clear()
    GenAIModelSetup._omni_clients.clear()
    yield
    GenAIModelSetup._clients.clear()
    GenAIModelSetup._global_clients.clear()
    GenAIModelSetup._omni_clients.clear()


class TestGenAIModelSetup:
    """Tests for GenAIModelSetup multi-project routing."""

    @patch("src.common.schema.genai_model_setup.Client")
    def test_get_client_default_project(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        client1 = GenAIModelSetup.get_client()
        client2 = GenAIModelSetup.get_client()

        assert client1 == mock_instance
        assert client2 == mock_instance
        # Should be cached, so constructor called once
        assert mock_client_cls.call_count == 1

    @patch("src.common.schema.genai_model_setup.Client")
    def test_get_client_custom_projects(self, mock_client_cls):
        mock_client_a = MagicMock()
        mock_client_b = MagicMock()
        mock_client_cls.side_effect = [mock_client_a, mock_client_b]

        client_a = GenAIModelSetup.get_client(project_id="project-a")
        client_b = GenAIModelSetup.get_client(project_id="project-b")
        client_a_cached = GenAIModelSetup.get_client(project_id="project-a")

        assert client_a == mock_client_a
        assert client_b == mock_client_b
        assert client_a_cached == mock_client_a
        assert mock_client_cls.call_count == 2

    @patch("src.common.schema.genai_model_setup.Client")
    def test_get_global_client_custom_projects(self, mock_client_cls):
        mock_global_a = MagicMock()
        mock_global_b = MagicMock()
        mock_client_cls.side_effect = [mock_global_a, mock_global_b]

        client_a = GenAIModelSetup.get_global_client(project_id="global-proj-a")
        client_b = GenAIModelSetup.get_global_client(project_id="global-proj-b")
        client_a_cached = GenAIModelSetup.get_global_client(
            project_id="global-proj-a"
        )

        assert client_a == mock_global_a
        assert client_b == mock_global_b
        assert client_a_cached == mock_global_a
        assert mock_client_cls.call_count == 2
        assert mock_client_cls.call_args.kwargs["location"] == "global"

    @patch("src.common.schema.genai_model_setup.config_service")
    def test_get_global_client_missing_project_raises(self, mock_cfg):
        mock_cfg.PROJECT_ID = None
        with pytest.raises(ValueError, match="Project ID must be set"):
            GenAIModelSetup.get_global_client(project_id=None)

    @patch("src.common.schema.genai_model_setup.Client")
    def test_get_omni_client_custom_projects(self, mock_client_cls):
        mock_omni_a = MagicMock()
        mock_omni_b = MagicMock()
        mock_client_cls.side_effect = [mock_omni_a, mock_omni_b]

        client_a = GenAIModelSetup.get_omni_client(
            project_id="novela-project-a"
        )
        client_b = GenAIModelSetup.get_omni_client(
            project_id="novela-project-b"
        )
        client_a_cached = GenAIModelSetup.get_omni_client(
            project_id="novela-project-a"
        )

        assert client_a == mock_omni_a
        assert client_b == mock_omni_b
        assert client_a_cached == mock_omni_a
        assert mock_client_cls.call_count == 2

    @patch("src.common.schema.genai_model_setup.Client")
    def test_gemini_model_setup_inherits_multi_project(self, mock_client_cls):
        mock_instance = MagicMock()
        mock_client_cls.return_value = mock_instance

        client = GeminiModelSetup.init(project_id="gemini-project")
        assert client == mock_instance
