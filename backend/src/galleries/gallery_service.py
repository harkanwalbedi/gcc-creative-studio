# Copyright 2025 Google LLC
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

import asyncio
import logging
import mimetypes
import pathlib
import tempfile
import zipfile

from fastapi import Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from src.auth.iam_signer_credentials_service import IamSignerCredentials
from src.common.dto.pagination_response_dto import PaginationResponseDto
from src.common.schema.media_item_model import (
    JobStatusEnum,
    MediaItemModel,
    SourceAssetLink,
    SourceMediaItemLink,
)
from src.common.storage_service import GcsService
from src.galleries.dto.bulk_copy_dto import BulkCopyDto
from src.galleries.dto.bulk_delete_dto import BulkDeleteDto
from src.galleries.dto.bulk_download_dto import BulkDownloadDto
from src.galleries.dto.gallery_response_dto import (
    MediaItemResponse,
    SourceAssetLinkResponse,
    SourceMediaItemLinkResponse,
)
from src.galleries.dto.gallery_search_dto import GallerySearchDto
from src.galleries.dto.unified_gallery_response import (
    UnifiedGalleryItemResponse,
)
from src.galleries.repository.unified_gallery_repository import (
    UnifiedGalleryRepository,
)
from src.images.imagen_service import ImagenService
from src.images.repository.media_item_repository import MediaRepository
from src.source_assets.repository.source_asset_repository import (
    SourceAssetRepository,
)
from src.users.repository.user_repository import UserRepository
from src.users.user_model import UserModel, UserRoleEnum
from src.workspaces.repository.workspace_repository import WorkspaceRepository
from src.workspaces.workspace_auth_guard import WorkspaceAuth
from src.tags.repository.tags_repository import TagsRepository

logger = logging.getLogger(__name__)


class GalleryService:
    """Provides business logic for querying media items and preparing them for the gallery."""

    def __init__(
        self,
        media_repo: MediaRepository = Depends(),
        source_asset_repo: SourceAssetRepository = Depends(),
        unified_gallery_repo: UnifiedGalleryRepository = Depends(),
        user_repo: UserRepository = Depends(),
        workspace_repo: WorkspaceRepository = Depends(),
        iam_signer_credentials: IamSignerCredentials = Depends(),
        workspace_auth: WorkspaceAuth = Depends(),
        imagen_service: ImagenService = Depends(),
        gcs_service: GcsService = Depends(),
        tags_repo: TagsRepository = Depends(),
    ):
        """Initializes the service with its dependencies."""
        self.media_repo = media_repo
        self.source_asset_repo = source_asset_repo
        self.unified_gallery_repo = unified_gallery_repo
        self.user_repo = user_repo
        self.workspace_repo = workspace_repo
        self.iam_signer_credentials = iam_signer_credentials
        self.workspace_auth = workspace_auth
        self.imagen_service = imagen_service
        self.gcs_service = gcs_service
        self.tags_repo = tags_repo

    async def _enrich_source_asset_link(
        self,
        link: SourceAssetLink,
    ) -> SourceAssetLinkResponse | None:
        """Fetches the source asset document and generates a presigned URL for it."""
        asset_doc = await self.source_asset_repo.get_by_id(link.asset_id)

        if not asset_doc:
            return None

        gcs_uri = (
            asset_doc.gcs_uri
            if isinstance(getattr(asset_doc, "gcs_uri", None), str)
            else None
        )
        external_url = (
            asset_doc.external_url
            if isinstance(getattr(asset_doc, "external_url", None), str)
            else None
        )
        thumbnail_gcs_uri = (
            asset_doc.thumbnail_gcs_uri
            if isinstance(getattr(asset_doc, "thumbnail_gcs_uri", None), str)
            else None
        )
        raw_mime = getattr(asset_doc, "mime_type", None)
        mime_type = (
            str(raw_mime.value)
            if hasattr(raw_mime, "value")
            else (raw_mime if isinstance(raw_mime, str) else None)
        )

        if not gcs_uri:
            from src.common.media_utils import extract_youtube_video_id

            presigned_thumbnail_url = None
            if thumbnail_gcs_uri:
                presigned_thumbnail_url = await asyncio.to_thread(
                    self.iam_signer_credentials.generate_presigned_url,
                    thumbnail_gcs_uri,
                )
            elif external_url:
                video_id = extract_youtube_video_id(external_url)
                if video_id:
                    presigned_thumbnail_url = (
                        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    )
            return SourceAssetLinkResponse(
                **link.model_dump(),
                presigned_url=external_url or "",
                presigned_thumbnail_url=presigned_thumbnail_url,
                gcs_uri=None,
                mime_type=mime_type,
                external_url=external_url,
            )

        tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url,
                gcs_uri,
            ),
        ]

        # Check if the asset has a thumbnail and create a task for it.
        if thumbnail_gcs_uri:
            tasks.append(
                asyncio.to_thread(
                    self.iam_signer_credentials.generate_presigned_url,
                    thumbnail_gcs_uri,
                ),
            )

        results = await asyncio.gather(*tasks)
        presigned_url = results[0]
        presigned_thumbnail_url = results[1] if len(results) > 1 else None

        return SourceAssetLinkResponse(
            **link.model_dump(),
            presigned_url=presigned_url,
            presigned_thumbnail_url=presigned_thumbnail_url,
            gcs_uri=gcs_uri,
            mime_type=mime_type,
            external_url=external_url,
        )

    async def _enrich_source_media_item_link(
        self,
        link: SourceMediaItemLink,
    ) -> SourceMediaItemLinkResponse | None:
        """Fetches the parent MediaItem document and generates a presigned URL
        for the specific image that was used as input.
        """
        parent_item = await self.media_repo.get_by_id(link.media_item_id)
        if (
            not parent_item
            or not parent_item.gcs_uris
            or not (0 <= link.media_index < len(parent_item.gcs_uris))
        ):
            return None

        # Get the specific GCS URI of the parent image that was edited.
        parent_gcs_uri = parent_item.gcs_uris[link.media_index]

        # Prepare tasks for both the main media and its thumbnail
        tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url,
                parent_gcs_uri,
            ),
        ]

        parent_thumbnail_gcs_uri = None
        if parent_item.thumbnail_uris and 0 <= link.media_index < len(
            parent_item.thumbnail_uris,
        ):
            parent_thumbnail_gcs_uri = parent_item.thumbnail_uris[
                link.media_index
            ]
            tasks.append(
                asyncio.to_thread(
                    self.iam_signer_credentials.generate_presigned_url,
                    parent_thumbnail_gcs_uri,
                ),
            )

        results = await asyncio.gather(*tasks)
        presigned_url = results[0]
        presigned_thumbnail_url = results[1] if len(results) > 1 else None

        return SourceMediaItemLinkResponse(
            **link.model_dump(),
            presigned_url=presigned_url,
            presigned_thumbnail_url=presigned_thumbnail_url,
            gcs_uri=parent_gcs_uri,
            mime_type=parent_item.mime_type,
        )

    async def _create_gallery_response(
        self, item: MediaItemModel
    ) -> MediaItemResponse:
        """Helper function to convert a MediaItem into a GalleryItemResponse
        by generating presigned URLs in parallel for its GCS URIs.
        """
        all_gcs_uris = item.gcs_uris or []

        # 1. Create tasks for main media URLs
        main_url_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in all_gcs_uris
            if uri
        ]

        # 1.5 Create tasks for original media URLs
        all_original_gcs_uris = item.original_gcs_uris or []
        original_url_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in all_original_gcs_uris
            if uri
        ]

        # 1.6 Create tasks for ProRes / DNxHR master media URLs in raw_data
        raw_data = (
            item.raw_data
            if isinstance(getattr(item, "raw_data", None), dict)
            else {}
        )
        raw_master_uris = raw_data.get("master_gcs_uris") or []
        all_master_gcs_uris = (
            [u for u in raw_master_uris if isinstance(u, str) and u]
            if isinstance(raw_master_uris, list)
            else []
        )
        master_url_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in all_master_gcs_uris
        ]

        # 2. Create tasks for thumbnail URLs
        thumbnail_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in (item.thumbnail_uris or "")
            if uri
        ]

        # 3. Create tasks for source asset URLs
        source_asset_tasks = []
        if item.source_assets:
            source_asset_tasks = [
                self._enrich_source_asset_link(link)
                for link in item.source_assets
            ]

        # 4. Create tasks for generated input asset URLs
        source_media_item_tasks = []
        if item.source_media_items:
            source_media_item_tasks = [
                self._enrich_source_media_item_link(link)
                for link in item.source_media_items
            ]

        # 5. Gather all results concurrently
        (
            presigned_urls,
            original_presigned_urls,
            master_presigned_urls,
            presigned_thumbnail_urls,
            enriched_source_assets_with_nones,
            enriched_source_media_items_with_nones,
        ) = await asyncio.gather(
            asyncio.gather(*main_url_tasks),
            asyncio.gather(*original_url_tasks),
            asyncio.gather(*master_url_tasks),
            asyncio.gather(*thumbnail_tasks),
            asyncio.gather(*source_asset_tasks),
            asyncio.gather(*source_media_item_tasks),
        )

        enriched_source_assets = [
            asset for asset in enriched_source_assets_with_nones if asset
        ]
        enriched_source_media_items = [
            asset for asset in enriched_source_media_items_with_nones if asset
        ]

        # Create the response DTO, copying all original data and adding the new URLs
        return MediaItemResponse(
            **item.model_dump(exclude={"source_assets"}),
            presigned_urls=presigned_urls,
            original_presigned_urls=original_presigned_urls,
            master_presigned_urls=(
                list(master_presigned_urls) if all_master_gcs_uris else None
            ),
            presigned_thumbnail_urls=presigned_thumbnail_urls,
            enriched_source_assets=enriched_source_assets or None,
            enriched_source_media_items=enriched_source_media_items or None,
        )

    async def _enrich_unified_item(
        self,
        item: UnifiedGalleryItemResponse,
    ) -> UnifiedGalleryItemResponse:
        """Enriches a UnifiedGalleryItemResponse with presigned URLs."""
        uris_to_sign = [u for u in (item.gcs_uris or []) if u]
        thumbnail_uris_to_sign = [u for u in (item.thumbnail_uris or []) if u]
        master_uris_to_sign: list[str] = []
        if isinstance(item.metadata, dict):
            raw_master = item.metadata.get(
                "masterGcsUris"
            ) or item.metadata.get("master_gcs_uris")
            if not raw_master:
                nested_raw = item.metadata.get("rawData") or item.metadata.get(
                    "raw_data"
                )
                if isinstance(nested_raw, dict):
                    raw_master = nested_raw.get(
                        "master_gcs_uris"
                    ) or nested_raw.get("masterGcsUris")
            if isinstance(raw_master, list):
                master_uris_to_sign = [
                    u for u in raw_master if isinstance(u, str) and u
                ]

        url_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in uris_to_sign
        ]

        thumbnail_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in thumbnail_uris_to_sign
        ]

        master_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in master_uris_to_sign
        ]

        (
            presigned_urls,
            presigned_thumbnail_urls,
            master_presigned_urls,
        ) = await asyncio.gather(
            asyncio.gather(*url_tasks),
            asyncio.gather(*thumbnail_tasks),
            asyncio.gather(*master_tasks),
        )

        external_url = None
        if isinstance(item.metadata, dict):
            raw_ext = item.metadata.get("externalUrl") or item.metadata.get(
                "external_url"
            )
            if isinstance(raw_ext, str) and raw_ext:
                external_url = raw_ext

        if not presigned_urls and external_url:
            presigned_urls = [external_url]
            if not presigned_thumbnail_urls:
                from src.common.media_utils import extract_youtube_video_id

                video_id = extract_youtube_video_id(external_url)
                if video_id:
                    presigned_thumbnail_urls = [
                        f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    ]

        item.presigned_urls = presigned_urls
        item.presigned_thumbnail_urls = presigned_thumbnail_urls
        item.master_presigned_urls = (
            list(master_presigned_urls) if master_uris_to_sign else None
        )
        return item

    async def get_paginated_gallery(
        self,
        search_dto: GallerySearchDto,
        current_user: UserModel,
    ) -> PaginationResponseDto[UnifiedGalleryItemResponse]:
        """Performs a paginated and filtered search for media items.
        Authorization is handled by a dependency in the controller.
        """
        _ = current_user
        unified_items_query = await self.unified_gallery_repo.query(
            search_dto,
        )
        unified_items = unified_items_query.data or []

        # Convert each MediaItem to a GalleryItemResponse in parallel
        response_tasks = [
            self._enrich_unified_item(item) for item in unified_items
        ]
        enriched_items = await asyncio.gather(*response_tasks)

        return PaginationResponseDto[UnifiedGalleryItemResponse](
            count=unified_items_query.count,
            page=unified_items_query.page,
            page_size=unified_items_query.page_size,
            total_pages=unified_items_query.total_pages,
            data=enriched_items,
        )

    async def get_media_by_id(
        self,
        item_id: int,
        current_user: UserModel,
    ) -> MediaItemResponse | None:
        """Retrieves a single media item, performs an authorization check,
        and enriches it with presigned URLs.
        """
        # Run the synchronous database query in a separate thread
        is_admin = UserRoleEnum.ADMIN in current_user.roles
        item = await self.media_repo.get_by_id(
            item_id, include_deleted=is_admin
        )

        if not item:
            return None

        # Fetch the workspace for authorization check
        workspace = await self.workspace_repo.get_by_id(item.workspace_id)

        # This should ideally not happen if data is consistent, but it's a good safeguard.
        if not workspace:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent workspace for this item not found.",
            )

        # Use the centralized authorization logic
        await self.workspace_auth.authorize(
            workspace_id=item.workspace_id,
            user=current_user,
        )

        response = await self._create_gallery_response(item)
        response.tags = await self.tags_repo.get_tags_for_media_item(item_id)
        if item.user_id:
            user = await self.user_repo.get_by_id(item.user_id)
            if user:
                response.user_picture = user.picture
        return response

    async def bulk_delete(
        self,
        bulk_delete_dto: BulkDeleteDto,
        current_user: UserModel,
    ) -> dict[str, int]:
        """Deletes multiple gallery items after authorizing the workspace access."""
        # 1. Authorize workspace access
        await self.workspace_auth.authorize(
            workspace_id=bulk_delete_dto.workspace_id,
            user=current_user,
        )

        deleted_count = 0
        for item in bulk_delete_dto.items:
            try:
                if item.type == "media_item":
                    media_item = await self.media_repo.get_by_id(item.id)
                    if not media_item:
                        continue

                    if media_item.workspace_id != bulk_delete_dto.workspace_id:
                        logger.warning(
                            f"Refusing to delete media_item {item.id} outside requested workspace bounds.",
                        )
                        continue

                    is_admin = UserRoleEnum.ADMIN in current_user.roles
                    is_owner = (
                        getattr(media_item, "user_id", None) == current_user.id
                    )
                    if not is_admin and not is_owner:
                        logger.warning(
                            f"User {current_user.id} unauthorized to delete media {item.id}",
                        )
                        continue

                    await self.media_repo.soft_delete(
                        item.id,
                        deleted_by=current_user.id,
                    )
                    deleted_count += 1
                elif item.type == "source_asset":
                    asset = await self.source_asset_repo.get_by_id(item.id)
                    if not asset:
                        continue

                    if asset.workspace_id != bulk_delete_dto.workspace_id:
                        logger.warning(
                            f"Refusing to delete source_asset {item.id} outside requested workspace bounds.",
                        )
                        continue

                    is_admin = UserRoleEnum.ADMIN in current_user.roles
                    is_owner = (
                        getattr(asset, "user_id", None) == current_user.id
                    )
                    if not is_admin and not is_owner:
                        logger.warning(
                            f"User {current_user.id} unauthorized to delete asset {item.id}",
                        )
                        continue

                    await self.source_asset_repo.soft_delete(
                        item.id,
                        deleted_by=current_user.id,
                    )
                    deleted_count += 1
            except Exception as e:
                logger.error(f"Error deleting {item.type} {item.id}: {e}")

        return {"deleted_count": deleted_count}

    async def restore_item(
        self,
        item_id: int,
        item_type: str,
        current_user: UserModel,
    ) -> bool:
        """Restores a soft-deleted item (media_item or source_asset) after authorizing roles."""
        is_admin = UserRoleEnum.ADMIN in current_user.roles
        if not is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only administrators can restore items.",
            )

        if item_type == "media_item":
            result = await self.media_repo.restore(item_id)
        elif item_type == "source_asset":
            result = await self.source_asset_repo.restore(item_id)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid item_type: {item_type}",
            )

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"{item_type} with ID {item_id} not found",
            )

        return True

    async def bulk_download(
        self,
        bulk_download_dto: BulkDownloadDto,
        current_user: UserModel,
    ) -> StreamingResponse:
        """Creates a ZIP archive of the selected media items and streams it to the client."""
        # 1. Authorize workspace access
        await self.workspace_auth.authorize(
            workspace_id=bulk_download_dto.workspace_id,
            user=current_user,
        )

        # Create a temporary file to hold the ZIP archive to avoid OOM
        temp_file = tempfile.TemporaryFile()
        downloads_log = []

        try:
            with zipfile.ZipFile(
                temp_file,
                "w",
                zipfile.ZIP_DEFLATED,
                True,
            ) as zip_file:
                for item in bulk_download_dto.items:
                    try:
                        # A media item row holds every asset of one
                        # generation, so it contributes as many ZIP entries as
                        # it has URIs.
                        entries: list[tuple[str, str]] = []

                        if item.type == "media_item":
                            media_item = await self.media_repo.get_by_id(
                                item.id
                            )
                            if not media_item:
                                continue

                            # Authorize workspace access for this item
                            await self.workspace_auth.authorize(
                                workspace_id=media_item.workspace_id,
                                user=current_user,
                            )
                            gcs_uris = media_item.gcs_uris or []
                            mime_type = getattr(media_item, "mime_type", None)
                            for index, gcs_uri in enumerate(gcs_uris):
                                # Use mimetypes library for guessing extension
                                ext = "bin"
                                if mime_type:
                                    ext = (
                                        mimetypes.guess_extension(
                                            str(mime_type)
                                        )
                                        or "bin"
                                    )
                                    if ext.startswith("."):
                                        ext = ext[1:]
                                elif "." in gcs_uri:
                                    ext = gcs_uri.split(".")[-1]

                                # Single-asset items keep the name users
                                # already get today; only siblings need
                                # telling apart.
                                suffix = (
                                    f"_{index}" if len(gcs_uris) > 1 else ""
                                )
                                entries.append(
                                    (gcs_uri, f"media_{item.id}{suffix}.{ext}"),
                                )
                        elif item.type == "source_asset":
                            asset = await self.source_asset_repo.get_by_id(
                                item.id
                            )
                            if not asset:
                                continue

                            # Authorize workspace access for this asset
                            await self.workspace_auth.authorize(
                                workspace_id=asset.workspace_id,
                                user=current_user,
                            )
                            if (
                                isinstance(getattr(asset, "gcs_uri", None), str)
                                and asset.gcs_uri
                            ):
                                gcs_uri = asset.gcs_uri
                                ext = (
                                    gcs_uri.split(".")[-1]
                                    if "." in gcs_uri
                                    else "bin"
                                )
                                entries.append(
                                    (gcs_uri, f"asset_{item.id}.{ext}"),
                                )

                        for gcs_uri, filename in entries:
                            try:
                                # Stream from GCS directly into ZipFile.open() to avoid OOM
                                def stream_to_zip(uri=gcs_uri, name=filename):
                                    with zip_file.open(name, "w") as zf:
                                        for (
                                            chunk
                                        ) in self.gcs_service.download_stream_from_gcs(
                                            uri,
                                        ):
                                            zf.write(chunk)

                                await asyncio.to_thread(stream_to_zip)
                                downloads_log.append(f"- Success: {filename}")
                            except Exception as e:
                                downloads_log.append(
                                    f"- Failed: {filename} ({e})"
                                )
                                logger.error(
                                    f"Error streaming {item.type} {item.id} to ZIP: {e}",
                                )
                    except Exception as e:
                        logger.error(
                            f"Error processing {item.type} {item.id}: {e}"
                        )

                # Add failure / success manifest README
                if downloads_log:
                    manifest_content = (
                        "Bulk Download Manifest\n======================\n\n"
                        + "\n".join(downloads_log)
                    )
                    zip_file.writestr("manifest.txt", manifest_content)

            # Seek to beginning for streaming
            temp_file.seek(0)

            async def iter_file():
                try:
                    while True:
                        # Use asyncio.to_thread to read from disk without blocking the event loop
                        chunk = await asyncio.to_thread(temp_file.read, 8192)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    temp_file.close()  # Delete TemporaryFile

            return StreamingResponse(
                iter_file(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": f"attachment; filename=workspace_{bulk_download_dto.workspace_id}_bulk_download.zip",
                },
            )

        except Exception as e:
            temp_file.close()
            raise e

    async def bulk_copy(
        self,
        bulk_copy_dto: BulkCopyDto,
        current_user: UserModel,
    ) -> dict:
        """Copies multiple gallery items to a target workspace."""
        # 1. Authorize target workspace access
        await self.workspace_auth.authorize(
            workspace_id=bulk_copy_dto.target_workspace_id,
            user=current_user,
        )

        copied_count = 0
        for item in bulk_copy_dto.items:
            try:
                if item.type == "media_item":
                    media_item = await self.media_repo.get_by_id(item.id)
                    if not media_item:
                        continue

                    # Authorize source workspace access (where the item is currently)
                    await self.workspace_auth.authorize(
                        workspace_id=media_item.workspace_id,
                        user=current_user,
                    )

                    # Create a new MediaItem instance with updated workspace_id
                    # exclude 'id', 'created_at', 'updated_at', 'deleted_at', 'deleted_by'
                    new_item_data = media_item.model_dump(
                        exclude={
                            "id",
                            "created_at",
                            "updated_at",
                            "deleted_at",
                            "deleted_by",
                            "workspace_id",
                        },
                    )
                    new_item_data["workspace_id"] = (
                        bulk_copy_dto.target_workspace_id
                    )

                    # Ensure user_id and user_email are set to the current user copying
                    new_item_data["user_id"] = current_user.id
                    new_item_data["user_email"] = current_user.email

                    await self.media_repo.create(new_item_data)
                    copied_count += 1

                elif item.type == "source_asset":
                    asset = await self.source_asset_repo.get_by_id(item.id)
                    if not asset:
                        continue

                    # Authorize source workspace access
                    await self.workspace_auth.authorize(
                        workspace_id=asset.workspace_id,
                        user=current_user,
                    )

                    # Create a new SourceAsset instance with updated workspace_id
                    new_asset_data = asset.model_dump(
                        exclude={
                            "id",
                            "created_at",
                            "updated_at",
                            "deleted_at",
                            "deleted_by",
                            "workspace_id",
                        },
                    )
                    new_asset_data["workspace_id"] = (
                        bulk_copy_dto.target_workspace_id
                    )

                    # Ensure user_id is set to the current user copying
                    new_asset_data["user_id"] = current_user.id

                    await self.source_asset_repo.create(new_asset_data)
                    copied_count += 1

            except Exception as e:
                logger.error(f"Error copying {item.type} {item.id}: {e}")

        return {"copied_count": copied_count}

    async def stream_gcs_media(
        self,
        gcs_uri: str,
        range_header: str | None = None,
    ) -> StreamingResponse:
        """Streams a GCS media file directly to the client, supporting HTTP Range requests."""
        if not gcs_uri or not gcs_uri.startswith("gs://"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid GCS URI provided",
            )

        uri_body = gcs_uri.replace("gs://", "", 1)
        if "/" not in uri_body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid GCS URI provided",
            )

        try:
            bucket_name, blob_name = uri_body.split("/", 1)
            bucket = self.gcs_service.client.bucket(bucket_name)
            blob = bucket.blob(blob_name)

            if not await asyncio.to_thread(blob.exists):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Media file not found in storage",
                )

            if not isinstance(getattr(blob, "size", None), int):
                try:
                    await asyncio.to_thread(blob.reload)
                except Exception:  # noqa: BLE001
                    pass

            total_size = (
                blob.size
                if isinstance(getattr(blob, "size", None), int)
                else None
            )

            content_type = (
                blob.content_type
                if isinstance(getattr(blob, "content_type", None), str)
                and blob.content_type
                else None
            )
            if not content_type:
                guessed_type, _ = mimetypes.guess_type(blob_name)
                content_type = guessed_type or "application/octet-stream"

            safe_name = pathlib.Path(blob_name).name
            base_headers = {
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "Cache-Control": "public, max-age=3600",
            }

            if range_header and total_size is not None:
                range_spec = range_header.strip()
                if not range_spec.lower().startswith("bytes="):
                    raise HTTPException(
                        status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                        detail="Invalid Range unit",
                        headers={"Content-Range": f"bytes */{total_size}"},
                    )

                byte_range = range_spec[6:].split(",", 1)[0].strip()
                if "-" not in byte_range or total_size == 0:
                    raise HTTPException(
                        status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                        detail="Requested range not satisfiable",
                        headers={"Content-Range": f"bytes */{total_size}"},
                    )

                start_str, end_str = byte_range.split("-", 1)
                start_str = start_str.strip()
                end_str = end_str.strip()

                try:
                    if start_str == "":
                        if not end_str:
                            raise ValueError("Empty range")
                        suffix_len = int(end_str)
                        if suffix_len <= 0:
                            raise ValueError("Invalid suffix length")
                        start = max(0, total_size - suffix_len)
                        end = total_size - 1
                    else:
                        start = int(start_str)
                        end = int(end_str) if end_str else total_size - 1
                except ValueError as val_err:
                    raise HTTPException(
                        status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                        detail="Malformed Range header",
                        headers={"Content-Range": f"bytes */{total_size}"},
                    ) from val_err

                if start < 0 or start >= total_size or end < start:
                    raise HTTPException(
                        status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                        detail="Requested range not satisfiable",
                        headers={"Content-Range": f"bytes */{total_size}"},
                    )

                end = min(end, total_size - 1)
                content_length = end - start + 1

                def iter_range(
                    byte_start: int = start,
                    length: int = content_length,
                ):
                    remaining = length
                    with blob.open("rb") as f:
                        f.seek(byte_start)
                        while remaining > 0:
                            chunk = f.read(min(256 * 1024, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk

                partial_headers = {
                    **base_headers,
                    "Content-Range": f"bytes {start}-{end}/{total_size}",
                    "Content-Length": str(content_length),
                }
                return StreamingResponse(
                    iter_range(),
                    status_code=status.HTTP_206_PARTIAL_CONTENT,
                    media_type=content_type,
                    headers=partial_headers,
                )

            if total_size is not None:
                base_headers["Content-Length"] = str(total_size)

            def iter_blob():
                with blob.open("rb") as f:
                    while chunk := f.read(256 * 1024):
                        yield chunk

            return StreamingResponse(
                iter_blob(),
                media_type=content_type,
                headers=base_headers,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Error streaming media for %s: %s", gcs_uri, e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            ) from e
