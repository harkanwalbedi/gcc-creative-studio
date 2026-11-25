# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may
# obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import importlib.metadata
import logging

from google.genai import Client

from src.config.config_service import config_service

logger = logging.getLogger(__name__)


try:
    VERSION = importlib.metadata.version("creative-studio")
except importlib.metadata.PackageNotFoundError:
    VERSION = "0.1.0"


class GenAIModelSetup:
    """A base class to handle the initialization and caching of Google GenAI clients.
    Supports dynamic multi-project client routing for workspace-specific billing.
    """

    _clients: dict[str, Client] = {}
    _global_clients: dict[str, Client] = {}
    _omni_clients: dict[str, Client] = {}

    @classmethod
    def get_client(cls, project_id: str | None = None) -> Client:
        """Initializes and returns a GenAI client instance for Vertex AI.
        Caches clients per project_id to reuse connection pools.
        """
        config = config_service
        target_project = project_id or config.PROJECT_ID
        location = config.LOCATION
        if None in [target_project, location]:
            raise ValueError("All parameters must be set.")

        if target_project not in cls._clients:
            try:
                logger.info(
                    "Initializing shared GenAI client for project '%s' in "
                    "location '%s'",
                    target_project,
                    location,
                )
                cls._clients[target_project] = Client(
                    project=target_project,
                    location=location,
                    vertexai=config.INIT_VERTEX,
                    http_options={
                        "headers": {
                            "user-agent": (
                                f"creative-studio/{VERSION} "
                                "(+https://github.com/GoogleCloudPlatform/"
                                "gcc-creative-studio)"
                            )
                        }
                    },
                )
            except Exception as e:
                logger.error("Failed to initialize GenAI client: %s", e)
                raise
        return cls._clients[target_project]

    @classmethod
    def get_global_client(cls, project_id: str | None = None) -> Client:
        """Initializes and returns a global-location GenAI client for Vertex AI.
        Caches clients per project_id to reuse connection pools.
        """
        config = config_service
        target_project = project_id or config.PROJECT_ID
        if target_project is None:
            raise ValueError("Project ID must be set.")

        if target_project not in cls._global_clients:
            try:
                logger.info(
                    "Initializing shared global GenAI client for project "
                    "'%s' in location 'global'",
                    target_project,
                )
                cls._global_clients[target_project] = Client(
                    project=target_project,
                    location="global",
                    vertexai=config.INIT_VERTEX,
                    http_options={
                        "headers": {
                            "user-agent": (
                                f"creative-studio/{VERSION} "
                                "(+https://github.com/GoogleCloudPlatform/"
                                "gcc-creative-studio)"
                            )
                        }
                    },
                )
            except Exception as e:
                logger.error("Failed to initialize global GenAI client: %s", e)
                raise
        return cls._global_clients[target_project]

    @classmethod
    def get_omni_client(cls, project_id: str | None = None) -> Client:
        """Initializes and returns an Omni GenAI client instance for Vertex AI.
        Caches clients per project_id.
        """
        config = config_service
        target_project = project_id or config.PROJECT_ID
        if target_project is None:
            raise ValueError("Project ID must be set.")

        if target_project not in cls._omni_clients:
            try:
                logger.info(
                    "Initializing shared Gemini Omni GenAI client for project "
                    "'%s' in location 'global'",
                    target_project,
                )
                cls._omni_clients[target_project] = Client(
                    vertexai=True,
                    project=target_project,
                    location="global",
                    http_options={
                        "base_url": "https://aiplatform.googleapis.com",
                        "headers": {
                            "user-agent": (
                                f"creative-studio/{VERSION} "
                                "(+https://github.com/GoogleCloudPlatform/"
                                "gcc-creative-studio)"
                            )
                        },
                    },
                )
            except Exception as e:
                logger.error(
                    "Failed to initialize Gemini Omni GenAI client: %s", e
                )
                raise
        return cls._omni_clients[target_project]

    @staticmethod
    def init(project_id: str | None = None) -> Client:
        """Returns the client instance for the specified or default project."""
        return GenAIModelSetup.get_client(project_id=project_id)
