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
"""Tests for the VPE HTTP surface.

The controller authenticates a caller, authorizes the workspace and hands
off to ``VpeService``, so the service is mocked here: what is under test is
the handoff rather than the upscale. Four things about it are worth
checking, and each is a way a working pipeline could still be unreachable or
harmful from outside.

* The router-level gate, which has to close **every** route on a deployment
  without VPE rather than the routes someone remembered.
* Which pool the job is handed to. A measured upscale holds its thread for
  around five minutes and a split clip is two of them, so handing this to
  the app's shared four-worker pool would let one user's library stall every
  unrelated generation in the application. That is invisible in review and
  invisible in a single manual run.
* That authorization happens before anything is queued, since what is queued
  is billed.
* Which failures reach the caller as which status code.

Screening responses are built by running the real ``screen_stored_video``
rather than by hand, so the wire shape is checked against outcomes the
gating module actually produces.
"""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.auth.auth_guard import get_current_user
from src.common.base_dto import MimeTypeEnum
from src.config.config_service import config_service
from src.galleries.dto.gallery_response_dto import MediaItemResponse
from src.users.user_model import UserModel, UserRoleEnum
from src.videos.vpe.capabilities import VpeCapabilityId, get_capability
from src.videos.vpe.gating import VpeScreeningResult, screen_stored_video
from src.videos.vpe_controller import require_vpe_enabled, router
from src.videos.vpe_service import MAX_SEGMENTS_PER_JOB, VpeService
from src.workspaces.workspace_auth_guard import WorkspaceAuth

_UPSCALE = get_capability(VpeCapabilityId.UPSCALE)

# The row the customer's real shot produces: ten seconds at 720x1280, which
# the gallery stores as "1K". Too long for a single job and comfortably
# inside the window once splitting is priced in.
_A_REAL_ROW = {"duration_seconds": 10.0, "resolution": "1K"}

_PAYLOAD = {
    "workspace_id": 1,
    "media_item_id": 2,
    "media_index": 0,
    "resolution": "4k",
}

_SHARPNESS = _UPSCALE.parameter("sharpness")


def _screening(**row) -> VpeScreeningResult:
    """Screens a row the way the service does.

    Args:
        **row: ``duration_seconds`` and ``resolution``, as the gallery
            stores them.

    Returns:
        The screening outcome, with the upscaler's splitting credit applied.
    """
    return screen_stored_video(
        _UPSCALE,
        max_segments=MAX_SEGMENTS_PER_JOB,
        **row,
    )


def _placeholder() -> MediaItemResponse:
    """Builds the processing row an accepted upscale returns.

    Returns:
        The row the gallery shows while the job runs.
    """
    return MediaItemResponse(
        id=99,
        workspace_id=1,
        user_id=1,
        user_email="user@example.com",
        mime_type=MimeTypeEnum.VIDEO_MP4,
        status="processing",
        original_prompt="Upscale to 4k",
        gcs_uris=[],
        thumbnail_uris=[],
        presigned_urls=[],
        presigned_thumbnail_urls=[],
        aspect_ratio="9:16",
        model="veo-experimental",
    )


@pytest.fixture(name="mock_user")
def fixture_mock_user():
    """A caller holding a role this router accepts."""
    return UserModel(
        id=1,
        email="user@example.com",
        name="Regular User",
        roles=[UserRoleEnum.USER],
    )


@pytest.fixture(name="mock_vpe_service")
def fixture_mock_vpe_service():
    """The service, whose behaviour has its own tests."""
    service = AsyncMock()
    service.screen_media_item = AsyncMock()
    service.start_upscale_job = AsyncMock(return_value=_placeholder())
    return service


@pytest.fixture(name="mock_workspace_auth")
def fixture_mock_workspace_auth():
    """Workspace authorization, permissive unless a test says otherwise."""
    auth = AsyncMock()
    auth.authorize = AsyncMock()
    return auth


@pytest.fixture(name="app")
def fixture_app(mock_user, mock_vpe_service, mock_workspace_auth, monkeypatch):
    """Mounts the router on a bare app with both thread pools present.

    Both pools are real ``MagicMock`` objects rather than one, so a test can
    tell which of them a job was handed to. Sharing them would make the
    distinction unobservable, which is the distinction most worth keeping.

    Args:
        mock_user: The authenticated caller.
        mock_vpe_service: The stand-in service.
        mock_workspace_auth: The stand-in workspace guard.
        monkeypatch: pytest's patcher, for the deployment flag.

    Returns:
        The configured application.
    """
    # The gate is left real and switched on, so every test below exercises
    # it rather than an override that would hide it going missing.
    monkeypatch.setattr(config_service, "VPE_ENABLED", True)

    application = FastAPI()
    application.include_router(router)
    application.state.executor = MagicMock(name="general_executor")
    application.state.vpe_executor = MagicMock(name="vpe_executor")

    application.dependency_overrides[get_current_user] = lambda: mock_user
    application.dependency_overrides[VpeService] = lambda: mock_vpe_service
    application.dependency_overrides[WorkspaceAuth] = (
        lambda: mock_workspace_auth
    )
    return application


@pytest.fixture(name="client")
def fixture_client(app):
    """A client for the mounted router.

    Args:
        app: The configured application.

    Returns:
        A test client.
    """
    return TestClient(app)


def _every_route() -> list[tuple[str, str]]:
    """Lists the router's routes, with path parameters filled in.

    Read off the router rather than written out, so a route added later is
    covered without anyone remembering to add it here - which is the same
    property the router-level gate exists to have.

    Returns:
        (method, path) pairs.
    """
    return [
        (method, re.sub(r"\{[^}]+\}", "1", route.path))
        for route in router.routes
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


class TestTheDeploymentGate:
    """VPE is an allowlisted preview, so the surface has to be closable."""

    @pytest.mark.parametrize(("method", "path"), _every_route())
    def test_every_route_closes_when_vpe_is_off(
        self,
        client,
        monkeypatch,
        method,
        path,
    ):
        """A deployment without access must not advertise the endpoints."""
        monkeypatch.setattr(config_service, "VPE_ENABLED", False)

        response = client.request(method, path, json=_PAYLOAD)

        assert response.status_code == 503
        assert "not enabled" in response.json()["detail"]

    def test_the_gate_hangs_off_the_router_not_the_routes(self):
        """Which is what makes the test above cover a route added later.

        Stated structurally as well as behaviourally: moving the gate onto
        each route individually would keep every assertion above passing
        right up until someone adds the route that omits it.
        """
        gates = [dependency.dependency for dependency in router.dependencies]
        assert require_vpe_enabled in gates


class TestScreening:
    """Whether to offer the action, and why not when the answer is no."""

    def test_an_eligible_row_is_offered(self, client, mock_vpe_service):
        """The customer's real shot shape: long, but splittable."""
        mock_vpe_service.screen_media_item.return_value = _screening(
            **_A_REAL_ROW,
        )

        response = client.get("/api/videos/vpe/screening/2")

        assert response.status_code == 200
        body = response.json()
        assert body["offer"] is True
        assert body["screening"] == "not_ruled_out"
        assert body["capability_id"] == str(VpeCapabilityId.UPSCALE)

    def test_a_ruled_out_row_is_a_200_carrying_the_reason(
        self,
        client,
        mock_vpe_service,
    ):
        """Screening is an answer, not an error.

        The frontend greys the action out and explains why, so a refusal
        that arrived as a 4xx would leave it with a status code and no
        sentence to show anyone.
        """
        mock_vpe_service.screen_media_item.return_value = _screening(
            duration_seconds=600.0,
            resolution="1K",
        )

        response = client.get("/api/videos/vpe/screening/2")

        assert response.status_code == 200
        body = response.json()
        assert body["offer"] is False
        assert body["screening"] == "ruled_out"
        assert body["findings"][0]["code"] == "frame_count_too_high"
        assert body["findings"][0]["message"].strip()

    def test_a_row_it_cannot_judge_is_still_offered(
        self,
        client,
        mock_vpe_service,
    ):
        """An unmeasured row must not hide a capability that may work.

        ``offer`` is true for both not_ruled_out and unknown, and the two
        are different states, so serialising them as one boolean is exactly
        where that could be got wrong.
        """
        mock_vpe_service.screen_media_item.return_value = _screening(
            duration_seconds=None,
            resolution=None,
        )

        response = client.get("/api/videos/vpe/screening/2")

        body = response.json()
        assert body["screening"] == "unknown"
        assert body["offer"] is True

    def test_it_screens_against_the_upscaler(self, client, mock_vpe_service):
        """The route is the upscale offer, so it must not ask about another
        capability."""
        mock_vpe_service.screen_media_item.return_value = _screening(
            **_A_REAL_ROW,
        )

        client.get("/api/videos/vpe/screening/2")

        assert mock_vpe_service.screen_media_item.await_args.args == (
            2,
            VpeCapabilityId.UPSCALE,
        )

    def test_a_missing_row_reaches_the_caller_as_a_404(
        self,
        client,
        mock_vpe_service,
    ):
        """The service raises it; nothing in between may flatten it."""
        mock_vpe_service.screen_media_item.side_effect = HTTPException(
            status_code=404,
            detail="MediaItem '2' not found or is not a video.",
        )

        response = client.get("/api/videos/vpe/screening/2")

        assert response.status_code == 404


class TestUpscale:
    """Queueing a job that costs money."""

    def test_it_returns_the_placeholder_row(self, client):
        """The caller gets a row to watch, not a wait."""
        response = client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        assert response.status_code == 200
        assert response.json()["id"] == 99
        assert response.json()["status"] == "processing"

    def test_the_job_goes_to_vpes_own_pool(
        self,
        client,
        app,
        mock_vpe_service,
    ):
        """Regression: not ``app.state.executor``.

        A five minute job holding one of the general pool's four threads,
        two of them for a split clip, means four upscales consume the
        application's whole background capacity and every unrelated
        generation stops. Nothing about the request looks different when
        this is wrong.
        """
        client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        handed = mock_vpe_service.start_upscale_job.await_args.kwargs[
            "executor"
        ]
        assert handed is app.state.vpe_executor
        assert handed is not app.state.executor

    def test_the_request_is_passed_through_intact(
        self,
        client,
        mock_vpe_service,
        mock_user,
    ):
        """The resolution the caller asked for is the one queued."""
        client.post(
            "/api/videos/vpe/upscale",
            json={**_PAYLOAD, "resolution": "1080p", "sharpness": 3},
        )

        queued = mock_vpe_service.start_upscale_job.await_args.kwargs
        assert queued["user"] is mock_user
        assert queued["request_dto"].media_item_id == 2
        assert queued["request_dto"].resolution == "1080p"
        assert queued["request_dto"].sharpness == 3

    def test_an_unauthorized_workspace_queues_nothing(
        self,
        client,
        mock_vpe_service,
        mock_workspace_auth,
    ):
        """Authorization comes first, because what follows it is billed."""
        mock_workspace_auth.authorize.side_effect = HTTPException(
            status_code=403,
            detail="Not a member of this workspace.",
        )

        response = client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        assert response.status_code == 403
        mock_vpe_service.start_upscale_job.assert_not_awaited()

    def test_a_caller_without_the_role_queues_nothing(
        self,
        app,
        client,
        mock_vpe_service,
    ):
        """The role gate is on the router, so it runs before the handler."""
        app.dependency_overrides[get_current_user] = lambda: UserModel(
            id=3,
            email="creator@example.com",
            name="Creator User",
            roles=[UserRoleEnum.CREATOR],
        )

        response = client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        assert response.status_code == 403
        mock_vpe_service.start_upscale_job.assert_not_awaited()

    def test_a_service_http_error_is_not_flattened(
        self,
        client,
        mock_vpe_service,
    ):
        """The handler catches broadly, so the narrow catch has to come
        first: a missing row is a 404 and not a 500."""
        mock_vpe_service.start_upscale_job.side_effect = HTTPException(
            status_code=404,
            detail="MediaItem '2' not found or is not a video.",
        )

        response = client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_a_bad_request_is_a_400(self, client, mock_vpe_service):
        """A ValueError is the caller's fault, so it is not a 500."""
        mock_vpe_service.start_upscale_job.side_effect = ValueError(
            "media_index 4 is out of range for this row.",
        )

        response = client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        assert response.status_code == 400
        assert "out of range" in response.json()["detail"]

    def test_anything_else_is_a_500(self, client, mock_vpe_service):
        """An unexpected failure still answers, rather than hanging."""
        mock_vpe_service.start_upscale_job.side_effect = RuntimeError(
            "VPE_PROJECT_ID is not set.",
        )

        response = client.post("/api/videos/vpe/upscale", json=_PAYLOAD)

        assert response.status_code == 500
        assert "VPE_PROJECT_ID" in response.json()["detail"]


class TestRequestValidation:
    """Bounds the form declares have to hold on the wire too."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({**_PAYLOAD, "media_item_id": 0}, id="no_such_row"),
            pytest.param({**_PAYLOAD, "workspace_id": 0}, id="no_such_space"),
            pytest.param({**_PAYLOAD, "media_index": -1}, id="negative_index"),
            pytest.param(
                {**_PAYLOAD, "sharpness": _SHARPNESS.maximum + 1},
                id="sharpness_over",
            ),
            pytest.param(
                {**_PAYLOAD, "sharpness": _SHARPNESS.minimum - 1},
                id="sharpness_under",
            ),
            pytest.param({**_PAYLOAD, "resolution": "8k"}, id="no_such_size"),
            pytest.param({"workspace_id": 1}, id="no_row_at_all"),
        ],
    )
    def test_a_malformed_request_queues_nothing(
        self,
        client,
        mock_vpe_service,
        payload,
    ):
        """Rejected before the service, so none of these cost anything."""
        response = client.post("/api/videos/vpe/upscale", json=payload)

        assert response.status_code == 422
        mock_vpe_service.start_upscale_job.assert_not_awaited()

    def test_the_resolution_defaults_to_4k(self, client, mock_vpe_service):
        """The API's own default is 720p, which upscales nothing."""
        client.post(
            "/api/videos/vpe/upscale",
            json={"workspace_id": 1, "media_item_id": 2},
        )

        queued = mock_vpe_service.start_upscale_job.await_args.kwargs
        assert queued["request_dto"].resolution == "4k"
