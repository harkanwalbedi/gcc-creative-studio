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

A fresh database has no candidates at all - no users, no workspaces, no
media. --seed uploads a local clip into the app's own bucket and creates
whatever rows are missing (a user, a workspace, the media item itself) so
there is something real to run against:

    docker compose exec backend python -m scripts.manual_vpe_upscale_test \
        --seed /app/real_10s_shot.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

_REPO_BACKEND = Path(__file__).resolve().parent.parent
if str(_REPO_BACKEND) not in sys.path:
    sys.path.insert(0, str(_REPO_BACKEND))

# pylint: disable=wrong-import-position
from src.common.base_dto import (
    AspectRatioEnum,
    GenerationModelEnum,
    MimeTypeEnum,
)
from src.common.schema.media_item_model import JobStatusEnum, MediaItemModel
from src.common.storage_service import GcsService
from src.config.config_service import config_service
from src.database import WorkerDatabase
from src.images.repository.media_item_repository import MediaRepository
from src.users.user_model import UserModel, UserRoleEnum
from src.videos.dto.upscale_video_dto import UpscaleVideoDto
from src.videos.veo_service import (
    resolve_measured_aspect_ratio,
    resolve_measured_resolution,
)
from src.videos.vpe.capabilities import VpeUpscaleResolution
from src.videos.vpe.preflight import VpeProbeError, probe_media
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


async def _find_or_create_user(db) -> tuple[int, str]:
    """Reuses any existing user, or creates a throwaway one.

    Ownership is not what this script is testing, so an existing user is
    reused where one exists rather than piling up duplicates on repeated
    runs.

    Returns:
        The user's id and email.
    """
    row = (
        await db.execute(
            text("SELECT id, email FROM users ORDER BY id LIMIT 1")
        )
    ).first()
    if row:
        return row.id, row.email

    email = "manual-vpe-seed@example.com"
    row = (
        await db.execute(
            text(
                "INSERT INTO users (email, roles, name, picture)"
                " VALUES (:email, :roles, :name, '') RETURNING id",
            ),
            {"email": email, "roles": ["admin"], "name": "Manual VPE Seed"},
        )
    ).one()
    await db.commit()
    print(f"Created user {row.id} ({email})")
    return row.id, email


async def _find_or_create_workspace(db, owner_id: int) -> int:
    """Reuses any existing workspace, or creates one owned by owner_id."""
    row = (await db.execute(text("SELECT id FROM workspaces LIMIT 1"))).first()
    if row:
        return row.id

    row = (
        await db.execute(
            text(
                "INSERT INTO workspaces (name, owner_id, scope)"
                " VALUES (:name, :owner_id, 'private') RETURNING id",
            ),
            {"name": "Manual VPE Seed Workspace", "owner_id": owner_id},
        )
    ).one()
    await db.commit()
    print(f"Created workspace {row.id}")
    return row.id


async def _seed(local_path: str) -> None:
    """Uploads a real clip and creates a gallery row for it to upscale.

    Args:
        local_path: Path to an mp4 readable from inside the backend
            container - so a path under the repo, which is bind-mounted,
            rather than somewhere only the host can see.
    """
    source = Path(local_path)
    if not source.is_file():
        print(f"No such file: {source}")
        return

    try:
        probe = probe_media(str(source))
    except VpeProbeError as error:
        print(f"Could not measure {source}: {error}")
        return
    if probe.frame_count is None or probe.fps is None or not probe.width:
        print(f"{source} is not a video this script can measure.")
        return

    duration_seconds = probe.frame_count / float(probe.fps)
    resolution = resolve_measured_resolution(probe.width, probe.height)
    aspect_ratio = (
        resolve_measured_aspect_ratio(probe.width, probe.height)
        or AspectRatioEnum.RATIO_16_9
    )

    async with WorkerDatabase() as db_factory:
        async with db_factory() as db:
            user_id, user_email = await _find_or_create_user(db)
            workspace_id = await _find_or_create_workspace(db, user_id)

            gcs = GcsService()
            destination = f"vpe_manual_seed/{source.name}"
            gcs_uri = gcs.upload_file_to_gcs(
                str(source),
                destination,
                "video/mp4",
            )
            if not gcs_uri:
                print(
                    "Upload failed - is GENMEDIA_BUCKET set and reachable?",
                )
                return
            print(f"Uploaded to {gcs_uri}")

            media_repo = MediaRepository(db)
            item = MediaItemModel(
                workspace_id=workspace_id,
                user_email=user_email,
                user_id=user_id,
                mime_type=MimeTypeEnum.VIDEO_MP4,
                model=GenerationModelEnum.VEO_3_1_GENERATE_001,
                original_prompt=f"Seeded from {source.name}",
                status=JobStatusEnum.COMPLETED,
                aspect_ratio=aspect_ratio,
                duration_seconds=duration_seconds,
                resolution=resolution,
                gcs_uris=[gcs_uri],
                thumbnail_uris=[],
            )
            created = await media_repo.create(item)
            print(
                f"Created media item {created.id}: {duration_seconds:g}s,"
                f" {resolution}, {aspect_ratio}",
            )
            print(
                "Run: docker compose exec backend python -m"
                f" scripts.manual_vpe_upscale_test {created.id}",
            )


_SAFE_ENVIRONMENTS = frozenset({"local", "development", "test"})


def _refuse_if_unsafe() -> bool:
    """Reports whether this deployment is one this script may touch.

    Returns:
        True if it is safe to proceed.
    """
    env = config_service.ENVIRONMENT
    if env in _SAFE_ENVIRONMENTS:
        return True
    allowed = sorted(_SAFE_ENVIRONMENTS)
    print(
        f"Refusing to run against ENVIRONMENT={env!r}. This script"
        " fabricates data and an admin identity and calls the service"
        f" layer directly, bypassing auth - safe for {allowed} only.",
    )
    return False


async def _run(media_item_id: int) -> None:
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


def main() -> None:
    """Parses arguments and dispatches to list, seed, or run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "media_item_id",
        nargs="?",
        type=int,
        default=None,
        help="Row to upscale. Omit to list candidates.",
    )
    parser.add_argument(
        "--seed",
        metavar="LOCAL_MP4",
        help="Upload a clip and create a row to run against.",
    )
    args = parser.parse_args()

    if not _refuse_if_unsafe():
        return

    if args.seed:
        asyncio.run(_seed(args.seed))
    else:
        asyncio.run(_run(args.media_item_id))


if __name__ == "__main__":
    main()
