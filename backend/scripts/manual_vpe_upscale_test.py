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
"""Exercises VpeService.start_upscale_job for real, without HTTP or auth.

The controller's job is only to authenticate a caller and hand off to
VpeService - it adds nothing this script needs to reproduce. Everything
worth proving (the screening gate, the placeholder row, the worker with the
new VPE_PROJECT_ID override) lives in the service layer, which takes plain
Python objects and has no dependency on FastAPI's request/response cycle.

Run inside the backend container, where the real config and Cloud SQL
connection already live:

    docker compose exec backend python -m scripts.manual_vpe_upscale_test

With no arguments it lists recent video rows and exits. Pass one to run it:

    docker compose exec backend python -m scripts.manual_vpe_upscale_test 123
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

# pylint: disable=wrong-import-position
from src.common.storage_service import GcsService
from src.config.config_service import config_service
from src.database import WorkerDatabase
from src.images.repository.media_item_repository import MediaRepository
from src.users.user_model import UserModel, UserRoleEnum
from src.videos.dto.upscale_video_dto import UpscaleVideoDto
from src.videos.vpe.capabilities import VpeUpscaleResolution
from src.videos.vpe_service import VpeService

# pylint: enable=wrong-import-position


async def _list_candidates(db) -> None:
    """Prints recent video rows worth trying, since an id is otherwise a
    guess."""
    rows = (
        await db.execute(
            text(
                "SELECT id, workspace_id, duration_seconds, resolution,"
                " array_length(gcs_uris, 1) AS clips"
                " FROM media_items"
                " WHERE mime_type = 'video/mp4'"
                " AND gcs_uris IS NOT NULL"
                " AND array_length(gcs_uris, 1) > 0"
                " ORDER BY created_at DESC LIMIT 10",
            ),
        )
    ).all()
    if not rows:
        print("No video rows with a stored clip were found.")
        return
    print(
        "Recent candidates (id, workspace_id, duration_s, resolution, clips):"
    )
    for row in rows:
        print(
            f"  {row.id}, {row.workspace_id}, {row.duration_seconds},"
            f" {row.resolution}, {row.clips}"
        )


async def _first_user(db) -> UserModel:
    """Picks any existing user, since ownership is not what this is testing."""
    row = (
        await db.execute(
            text("SELECT id, email, name FROM users ORDER BY id LIMIT 1"),
        )
    ).one()
    return UserModel(
        id=row.id,
        email=row.email,
        name=row.name or row.email,
        roles=[UserRoleEnum.ADMIN],
    )


_SAFE_ENVIRONMENTS = frozenset({"local", "development", "test"})


async def _run(media_item_id: int) -> None:
    env = config_service.ENVIRONMENT
    if env not in _SAFE_ENVIRONMENTS:
        allowed = sorted(_SAFE_ENVIRONMENTS)
        print(
            f"Refusing to run against ENVIRONMENT={env!r}. This script"
            " fabricates an admin identity and calls the service layer"
            f" directly, bypassing auth - safe for {allowed} only.",
        )
        return

    async with WorkerDatabase() as db_factory:
        async with db_factory() as db:
            if media_item_id is None:
                await _list_candidates(db)
                return

            user = await _first_user(db)
            service = VpeService(
                media_repo=MediaRepository(db),
                gcs_service=GcsService(),
            )
            request_dto = UpscaleVideoDto(
                workspace_id=1,  # overwritten below with the row's own
                media_item_id=media_item_id,
                media_index=0,
                resolution=VpeUpscaleResolution.UHD_4K,
            )
            source = await service.media_repo.get_by_id(media_item_id)
            if not source:
                print(f"No media item {media_item_id}")
                return
            request_dto.workspace_id = source.workspace_id

            with ThreadPoolExecutor(max_workers=1) as executor:
                placeholder = await service.start_upscale_job(
                    request_dto=request_dto,
                    user=user,
                    executor=executor,
                )
                print(
                    f"Queued placeholder row {placeholder.id}, polling until"
                    " it leaves PROCESSING (this can take several minutes)..."
                )

                while True:
                    await asyncio.sleep(15)
                    row = (
                        await db.execute(
                            text(
                                "SELECT status, error_message, gcs_uris"
                                " FROM media_items WHERE id = :id",
                            ),
                            {"id": placeholder.id},
                        )
                    ).one()
                    print(f"  status={row.status}")
                    if row.status != "processing":
                        print(f"  error_message={row.error_message}")
                        print(f"  gcs_uris={row.gcs_uris}")
                        break


if __name__ == "__main__":
    arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(_run(arg))
