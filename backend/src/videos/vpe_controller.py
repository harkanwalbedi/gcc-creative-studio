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
"""HTTP surface for Veo Pro Experimental.

Its own router rather than more routes on ``veo_controller``, because the
whole surface is gated: VPE is an allowlisted private preview, and a
deployment without access must not advertise endpoints it cannot serve. The
gate is a dependency on the router itself, so a route added later cannot
forget it.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as Status

from src.auth.auth_guard import RoleChecker, get_current_user
from src.config.config_service import config_service
from src.galleries.dto.gallery_response_dto import MediaItemResponse
from src.users.user_model import UserModel, UserRoleEnum
from src.videos.dto.upscale_video_dto import UpscaleVideoDto
from src.videos.dto.vpe_screening_dto import VpeScreeningResponse
from src.videos.vpe.capabilities import VpeCapabilityId
from src.videos.vpe_service import VpeService
from src.workspaces.workspace_auth_guard import WorkspaceAuth

user_only = Depends(
    RoleChecker(allowed_roles=[UserRoleEnum.USER, UserRoleEnum.ADMIN])
)


def require_vpe_enabled() -> None:
    """Closes every route in this router on a deployment without VPE.

    Raises:
        HTTPException: 503 if VPE is switched off.
    """
    if not config_service.VPE_ENABLED:
        raise HTTPException(
            status_code=Status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VPE is not enabled on this deployment.",
        )


router = APIRouter(
    prefix="/api/videos/vpe",
    tags=["Veo Pro Experimental"],
    responses={404: {"description": "Not found"}},
    dependencies=[user_only, Depends(require_vpe_enabled)],
)


@router.get(
    "/screening/{media_item_id}",
    response_model=VpeScreeningResponse,
    summary="Check whether a gallery video is worth offering an upscale on",
)
async def screen_media_item(
    media_item_id: int,
    service: VpeService = Depends(),
) -> VpeScreeningResponse:
    """Screens a stored video against the upscaler's input rules.

    Cheap: it reads the row and downloads nothing. It can only rule the
    capability out, never confirm it - the row carries no frame rate and
    stores resolution as a name rather than as pixels - so a job that passes
    here is still checked properly against the file itself before it runs.

    Args:
        media_item_id: The gallery row to screen.
        service: The VPE service.

    Returns:
        Whether to offer the action, and why not when the answer is no.
    """
    result = await service.screen_media_item(
        media_item_id,
        VpeCapabilityId.UPSCALE,
    )
    return VpeScreeningResponse.from_result(result)


@router.post(
    "/upscale",
    response_model=MediaItemResponse,
    summary="Upscale an existing gallery video to 1080p or 4K",
)
async def upscale_video(
    upscale_request: UpscaleVideoDto,
    request: Request,
    current_user: UserModel = Depends(get_current_user),
    service: VpeService = Depends(),
    workspace_auth: WorkspaceAuth = Depends(),
) -> MediaItemResponse:
    """Queues an upscale and returns its placeholder row immediately.

    A clip longer than the upscaler's eight second window is split, upscaled
    in pieces and rejoined, which the caller does not need to know: the job
    either produces a full-length master or fails.

    Args:
        upscale_request: The validated request.
        request: The incoming request, for the app's VPE thread pool.
        current_user: The authenticated user.
        service: The VPE service.
        workspace_auth: Workspace authorization dependency.

    Returns:
        The placeholder row, visible in the gallery as processing.

    Raises:
        HTTPException: Passed through from the service, or 500 for anything
            unexpected.
    """
    try:
        await workspace_auth.authorize(
            workspace_id=upscale_request.workspace_id,
            user=current_user,
        )
        # VPE's own pool, not app.state.executor. A measured upscale runs for
        # around five minutes holding its thread, and a split clip is two of
        # them, so sharing the four-worker general pool would let one user's
        # library stall every unrelated generation in the application.
        return await service.start_upscale_job(
            request_dto=upscale_request,
            user=current_user,
            executor=request.app.state.vpe_executor,
        )
    except HTTPException as http_exception:
        raise http_exception
    except ValueError as value_error:
        raise HTTPException(
            status_code=Status.HTTP_400_BAD_REQUEST,
            detail=str(value_error),
        ) from value_error
    except Exception as error:
        raise HTTPException(
            status_code=Status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error
