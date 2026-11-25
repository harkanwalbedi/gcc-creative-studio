# Copyright 2024 Google LLC
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
import io
import logging
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, HTTPException, status
from google.cloud.logging import Client as LoggerClient
from google.cloud.logging.handlers import CloudLoggingHandler
from google.genai import Client, types
from PIL import Image as PILImage

from src.auth.iam_signer_credentials_service import IamSignerCredentials
from src.brand_guidelines.repository.brand_guideline_repository import (
    BrandGuidelineRepository,
)
from src.common.base_dto import (
    AspectRatioEnum,
    GenerationModelEnum,
    MimeTypeEnum,
)
from src.common.media_utils import (
    extract_youtube_video_id,
    generate_image_thumbnail_from_gcs,
    get_youtube_aspect_ratio,
)
from src.common.schema.genai_model_setup import GenAIModelSetup
from src.common.schema.media_item_model import (
    AssetRoleEnum,
    JobStatusEnum,
    MediaItemModel,
    SourceAssetLink,
    SourceMediaItemLink,
)
from src.common.storage_service import GcsService
from src.config.config_service import config_service
from src.database import WorkerDatabase
from src.galleries.dto.gallery_response_dto import MediaItemResponse
from src.images.dto.create_imagen_dto import CreateImagenDto
from src.images.dto.upscale_imagen_dto import UpscaleImagenDto
from src.images.dto.vto_dto import VtoDto, VtoInputLink
from src.images.repository.media_item_repository import MediaRepository
from src.workspaces.repository.workspace_repository import WorkspaceRepository
from src.images.schema.imagen_result_model import (
    CustomImagenResult,
    ImageGenerationResult,
)
from src.multimodal.gemini_service import GeminiService, PromptTargetEnum
from src.source_assets.repository.source_asset_repository import (
    SourceAssetRepository,
)
from src.source_assets.schema.source_asset_model import (
    AssetScopeEnum,
    AssetTypeEnum,
    SourceAssetModel,
)
from src.users.repository.user_repository import UserRepository
from src.users.user_model import UserModel

logger = logging.getLogger(__name__)


def optimize_image_bytes(data: bytes, max_size: int = 1024) -> bytes:
    """Resizes image to a max dimension of `max_size` (maintaining aspect ratio)
    and compresses it as JPEG to optimize Vertex API payload size.
    """
    try:
        img = PILImage.open(io.BytesIO(data))
        # Convert RGBA to RGB (JPEG doesn't support transparency/alpha channel)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize if larger than max_size
        if max(img.width, img.height) > max_size:
            img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)

        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=90)
        return out_buf.getvalue()
    except Exception as e:
        logger.warning(
            "Failed to optimize image bytes: %s. Using original bytes.", e
        )
        return data


# --- STANDALONE WORKER FUNCTION FOR VTO ---
def _process_vto_in_background(
    media_item_id: int,
    request_dto: VtoDto,
    current_user: UserModel,
):  # type: ignore
    """Long-running worker task for VTO generation. Creates its own service
    instances because it runs in a completely separate process.
    """
    import asyncio
    import os
    import sys

    # google.cloud.logging moved to top level

    worker_logger = logging.getLogger(f"vto_worker.{media_item_id}")
    worker_logger.setLevel(logging.INFO)

    try:
        # Clear any handlers that might be inherited from the parent process
        if worker_logger.hasHandlers():
            worker_logger.handlers.clear()

        if os.getenv("ENVIRONMENT") == "production":
            log_client = LoggerClient()
            handler = CloudLoggingHandler(
                log_client,
                name=f"vto_worker.{media_item_id}",
            )
            worker_logger.addHandler(handler)
        else:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                "%(asctime)s - [VTO_WORKER] - %(levelname)s - %(message)s",
            )
            handler.setFormatter(formatter)
            worker_logger.addHandler(handler)

        # Create a new event loop for this process
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # WorkerDatabase moved to top level

        async def _async_worker():
            async with WorkerDatabase() as db_factory:
                async with db_factory() as db:
                    # Create new instances of dependencies within this process
                    media_repo = MediaRepository(db)
                    iam_signer_credentials = IamSignerCredentials()
                    source_asset_repo = SourceAssetRepository(db)
                    workspace_repo = WorkspaceRepository(db)
                    cfg = config_service

                    target_project_id: str | None = None
                    target_bucket_name: str | None = None
                    if request_dto.workspace_id:
                        try:
                            ws = await workspace_repo.get_by_id(
                                request_dto.workspace_id
                            )
                            if ws:
                                if isinstance(
                                    getattr(ws, "gcp_project_id", None), str
                                ):
                                    target_project_id = ws.gcp_project_id
                                if isinstance(
                                    getattr(ws, "gcs_bucket_name", None), str
                                ):
                                    target_bucket_name = ws.gcs_bucket_name
                        except Exception as e:
                            worker_logger.warning(
                                "Could not fetch workspace config: %s", e
                            )

                    gcs_service = GcsService(
                        bucket_name=target_bucket_name,
                        project_id=target_project_id,
                    )

                    try:
                        start_time = time.monotonic()
                        client = GenAIModelSetup.init(
                            project_id=target_project_id
                        )
                        effective_bucket = (
                            target_bucket_name or cfg.IMAGE_BUCKET
                        )
                        gcs_output_directory = (
                            f"gs://{effective_bucket}/"
                            f"{cfg.IMAGEN_RECONTEXT_SUBFOLDER}"
                        )

                        source_media_items: list[
                            SourceMediaItemLink
                        ] = []  # type: ignore
                        source_assets: list[SourceAssetLink] = []

                        # --- Resolve all input links sequentially to avoid concurrent DB sessions ---
                        garment_inputs = [
                            (request_dto.top_image, AssetRoleEnum.VTO_TOP),
                            (
                                request_dto.bottom_image,
                                AssetRoleEnum.VTO_BOTTOM,
                            ),
                            (request_dto.dress_image, AssetRoleEnum.VTO_DRESS),
                            (request_dto.shoe_image, AssetRoleEnum.VTO_SHOE),
                        ]

                        active_garment_inputs = [
                            (inp, role)
                            for inp, role in garment_inputs
                            if inp is not None
                        ]

                        inputs_to_resolve = [
                            (request_dto.person_image, AssetRoleEnum.VTO_PERSON)
                        ]
                        for inp, role in active_garment_inputs:
                            inputs_to_resolve.append((inp, role))

                        gcs_uris_with_roles = []
                        for inp, role in inputs_to_resolve:
                            if inp.source_asset_id:
                                asset = await source_asset_repo.get_by_id(
                                    inp.source_asset_id,
                                )
                                if not asset:
                                    raise ValueError(
                                        f"Source asset {inp.source_asset_id} not found."
                                    )
                                source_assets.append(
                                    SourceAssetLink(
                                        asset_id=asset.id, role=role
                                    )
                                )
                                gcs_uri = asset.gcs_uri
                            elif inp.source_media_item:
                                media_item_link = inp.source_media_item
                                parent_item = await media_repo.get_by_id(
                                    media_item_link.media_item_id,
                                )
                                if (
                                    not parent_item
                                    or not parent_item.gcs_uris
                                    or not (
                                        0
                                        <= media_item_link.media_index
                                        < len(parent_item.gcs_uris)
                                    )
                                ):
                                    raise ValueError(
                                        f"Source media item {media_item_link.media_item_id} "
                                        f"not found or index is invalid."
                                    )
                                source_media_items.append(
                                    SourceMediaItemLink(
                                        media_item_id=media_item_link.media_item_id,
                                        media_index=media_item_link.media_index,
                                        role=role,
                                    )
                                )
                                gcs_uri = parent_item.gcs_uris[
                                    media_item_link.media_index
                                ]
                            else:
                                raise ValueError("Invalid VTO input provided.")

                            gcs_uris_with_roles.append((gcs_uri, role))

                        # --- Download & Optimize all inputs in parallel ---
                        async def download_and_optimize_uri(
                            gcs_uri: str, role: AssetRoleEnum
                        ) -> bytes:
                            img_bytes = await asyncio.to_thread(
                                gcs_service.download_bytes_from_gcs, gcs_uri
                            )
                            if not img_bytes:
                                raise ValueError(
                                    f"Failed to download bytes from GCS: {gcs_uri}"
                                )
                            return await asyncio.to_thread(
                                optimize_image_bytes, img_bytes
                            )

                        tasks = []
                        for gcs_uri, role in gcs_uris_with_roles:
                            tasks.append(
                                download_and_optimize_uri(gcs_uri, role)
                            )

                        downloaded_results = await asyncio.gather(*tasks)

                        current_person_bytes = downloaded_results[0]
                        active_garments_bytes = downloaded_results[1:]

                        final_response = None

                        # --- Loop through each garment and apply it sequentially ---
                        for i, (garment_input, role) in enumerate(
                            active_garment_inputs
                        ):
                            garment_bytes = active_garments_bytes[i]
                            person_image_part = types.Image(
                                image_bytes=current_person_bytes,
                            )
                            product_image_part = types.ProductImage(
                                product_image=types.Image(
                                    image_bytes=garment_bytes
                                ),
                            )

                            worker_logger.info(
                                "Applying garment %s/%s with role %s",
                                i + 1,
                                len(active_garment_inputs),
                                role,
                            )

                            # Run sync API call in thread to avoid blocking
                            # the loop
                            response = await asyncio.to_thread(
                                client.models.recontext_image,
                                model=cfg.VTO_MODEL_ID,
                                source=types.RecontextImageSource(
                                    person_image=person_image_part,
                                    product_images=[product_image_part],
                                ),
                                config=types.RecontextImageConfig(
                                    number_of_images=(
                                        request_dto.number_of_media
                                    ),
                                ),
                            )

                            if i == len(active_garment_inputs) - 1:
                                final_response = response
                            elif (
                                response.generated_images
                                and response.generated_images[0].image
                                and response.generated_images[
                                    0
                                ].image.image_bytes
                            ):
                                current_person_bytes = (
                                    response.generated_images[
                                        0
                                    ].image.image_bytes
                                )

                        if not final_response:
                            raise ValueError(
                                "VTO generation failed to produce a final "
                                "result."
                            )

                        all_generated_images = (
                            final_response.generated_images or []
                        )

                        if not all_generated_images:
                            raise ValueError(
                                "No images generated from VTO process."
                            )

                        # Process results
                        valid_generated_images = [
                            img
                            for img in all_generated_images
                            if img.image and img.image.image_bytes
                        ]
                        if not valid_generated_images:
                            raise ValueError(
                                "No valid images returned from VTO process."
                            )

                        mime_type: MimeTypeEnum = (
                            MimeTypeEnum.IMAGE_PNG
                            if valid_generated_images[0].image.mime_type
                            == MimeTypeEnum.IMAGE_PNG
                            else MimeTypeEnum.IMAGE_JPEG
                        )

                        # Save final images to GCS and get permanent URIs
                        permanent_gcs_uris = []
                        for img in valid_generated_images:
                            import uuid

                            ext = (
                                "png"
                                if mime_type == MimeTypeEnum.IMAGE_PNG
                                else "jpg"
                            )
                            file_name = f"{str(uuid.uuid4())}.{ext}"
                            gcs_uri = gcs_service.store_to_gcs(
                                folder=f"images/{cfg.IMAGEN_RECONTEXT_SUBFOLDER}",
                                file_name=file_name,
                                mime_type=mime_type.value,
                                contents=img.image.image_bytes,
                                decode=False,
                                bucket_name=cfg.GENMEDIA_BUCKET,
                            )
                            if gcs_uri:
                                permanent_gcs_uris.append(gcs_uri)

                        if not permanent_gcs_uris:
                            raise ValueError(
                                "Failed to save generated images to GCS."
                            )

                        # Generate thumbnails
                        thumbnail_uris = []
                        for uri in permanent_gcs_uris:
                            thumb_uri = generate_image_thumbnail_from_gcs(
                                gcs_service,
                                uri,
                                mime_type.value,
                            )
                            if thumb_uri:
                                thumbnail_uris.append(thumb_uri)
                            else:
                                thumbnail_uris.append(uri)

                        end_time = time.monotonic()
                        generation_time = end_time - start_time

                        # Update the document with completed status
                        update_data = {
                            "status": JobStatusEnum.COMPLETED,
                            "gcs_uris": permanent_gcs_uris,
                            "thumbnail_uris": thumbnail_uris,
                            "generation_time": generation_time,
                            "num_media": len(permanent_gcs_uris),
                            "mime_type": mime_type,
                            "source_assets": (
                                [item.model_dump() for item in source_assets]
                                if source_assets
                                else None
                            ),
                            "source_media_items": (
                                [
                                    item.model_dump()
                                    for item in source_media_items
                                ]
                                if source_media_items
                                else None
                            ),
                        }
                        await media_repo.update(media_item_id, update_data)
                        worker_logger.info(
                            "Successfully processed VTO job.",
                            extra={
                                "json_fields": {
                                    "media_id": media_item_id,
                                    "generation_time_seconds": generation_time,
                                    "images_generated": len(permanent_gcs_uris),
                                },
                            },
                        )

                    except Exception as e:
                        worker_logger.error(
                            "VTO generation task failed.",
                            extra={
                                "json_fields": {
                                    "media_id": media_item_id,
                                    "error": str(e),
                                },
                            },
                            exc_info=True,
                        )
                        error_update_data = {
                            "status": JobStatusEnum.FAILED,
                            "error_message": str(e),
                        }
                        await media_repo.update(
                            media_item_id, error_update_data
                        )

        loop.run_until_complete(_async_worker())
        loop.close()

    except Exception as e:
        worker_logger.error(
            "VTO worker failed to initialize.",
            extra={"json_fields": {"media_id": media_item_id, "error": str(e)}},
            exc_info=True,
        )


def gemini_generate_image(
    gcs_service: GcsService,
    vertexai_client: Client,
    prompt: str,
    model: GenerationModelEnum,
    bucket_name: str,
    reference_images: list[types.Image] | None = None,
    reference_parts: list[types.Part] | None = None,
    aspect_ratio: str | None = None,
    google_search: bool = False,
    resolution: str | None = None,
    temperature: float | None = None,
) -> tuple[types.GeneratedImage | None, dict | None]:
    """Generates an image using the Gemini API for text-to-image or
    image-to-image.
    This is a blocking function.

    Returns:
        A (types.GeneratedImage | None, grounding_metadata | None) tuple.

    """
    if not model.is_gemini_image_model:
        raise ValueError(f"Model {model.value} is not a Gemini image model.")
    for attempt in range(3):
        try:
            parts = [types.Part.from_text(text=prompt)]
            if reference_images:
                for img in reference_images:
                    if img.gcs_uri:
                        parts.append(
                            types.Part.from_uri(
                                file_uri=img.gcs_uri,
                                mime_type=img.mime_type,
                            ),
                        )
                    elif img.image_bytes:
                        parts.append(
                            types.Part.from_bytes(
                                data=img.image_bytes,
                                mime_type=img.mime_type or "image/png",
                            ),
                        )
            if reference_parts:
                parts.extend(reference_parts)

            contents: list[types.ContentUnionDict] = [
                types.Content(role="user", parts=parts),
            ]

            aspect_ratio_val = getattr(aspect_ratio, "value", aspect_ratio)
            if aspect_ratio_val in ("auto", AspectRatioEnum.AUTO):
                aspect_ratio_val = None

            image_config = types.ImageConfig(
                aspect_ratio=aspect_ratio_val,
                image_size=resolution,
            )

            tools = []
            if google_search:
                tools.append(types.Tool(google_search=types.GoogleSearch()))

            generate_content_config = types.GenerateContentConfig(
                response_modalities=["Text", "Image"],
                image_config=image_config,
                tools=tools if tools else None,
                # Omitted when unset so the model's own default applies rather
                # than us pinning one.
                temperature=temperature,
            )
            client_to_use = (
                vertexai_client
                if vertexai_client is not None
                else GenAIModelSetup.get_global_client()
            )
            response: types.GenerateContentResponse = (
                client_to_use.models.generate_content(
                    model=getattr(model, "value", model),
                    contents=contents,
                    config=generate_content_config,
                )
            )

            grounding_metadata = None

            for candidate in response.candidates:
                if (
                    candidate.grounding_metadata
                    and candidate.grounding_metadata.grounding_chunks
                ):
                    grounding_metadata = (
                        candidate.grounding_metadata.model_dump()
                    )

                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if part.inline_data:
                            image_data_base64 = part.inline_data.data or ""
                            content_type = (
                                part.inline_data.mime_type or "image/png"
                            )

                            image_url = gcs_service.store_to_gcs(
                                folder="gemini_images",
                                file_name=str(uuid.uuid4()),
                                mime_type=content_type,
                                contents=image_data_base64,
                                bucket_name=bucket_name,
                            )
                            if not image_url:
                                logger.debug("Error: image url not generated ")
                                return None, None

                            image_object = types.Image(
                                gcs_uri=image_url,
                                mime_type=content_type,
                            )
                            return (
                                types.GeneratedImage(image=image_object),
                                grounding_metadata,
                            )

            logger.debug("No image data found in the API response stream.")
            return None, None
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(2**attempt + random.random())
                continue
            raise e
    return None, None


# --- STANDALONE WORKER FUNCTION ---
def _process_image_in_background(
    media_item_id: int,
    request_dto: CreateImagenDto,
    current_user: UserModel,
):
    """Background worker to handle image generation, GCS upload, and DB
    update."""
    worker_logger = logging.getLogger(f"image_worker.{media_item_id}")
    worker_logger.setLevel(logging.INFO)

    try:
        if worker_logger.hasHandlers():
            worker_logger.handlers.clear()

        if os.getenv("ENVIRONMENT") == "production":
            log_client = LoggerClient()
            handler = CloudLoggingHandler(
                log_client,
                name=f"image_worker.{media_item_id}",
            )
            worker_logger.addHandler(handler)
        else:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                "%(asctime)s - [IMAGE_WORKER] - %(levelname)s - %(message)s",
            )
            handler.setFormatter(formatter)
            worker_logger.addHandler(handler)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _async_worker():
            async with WorkerDatabase() as db_factory:
                async with db_factory() as db:
                    media_repo = MediaRepository(db)
                    source_asset_repo = SourceAssetRepository(db)
                    user_repo = UserRepository(db)
                    brand_guideline_repo = BrandGuidelineRepository(db)
                    gemini_service = GeminiService(
                        brand_guideline_repo=brand_guideline_repo,
                    )
                    workspace_repo = WorkspaceRepository(db)
                    target_project_id: str | None = None
                    target_bucket_name: str | None = None
                    if request_dto.workspace_id:
                        try:
                            ws = await workspace_repo.get_by_id(
                                request_dto.workspace_id
                            )
                            if ws:
                                if isinstance(
                                    getattr(ws, "gcp_project_id", None), str
                                ):
                                    target_project_id = ws.gcp_project_id
                                if isinstance(
                                    getattr(ws, "gcs_bucket_name", None), str
                                ):
                                    target_bucket_name = ws.gcs_bucket_name
                        except Exception as e:
                            worker_logger.warning(
                                "Could not fetch workspace config: %s", e
                            )

                    gcs_service = GcsService(
                        bucket_name=target_bucket_name,
                        project_id=target_project_id,
                    )
                    iam_signer_credentials = IamSignerCredentials()
                    cfg = config_service

                    if request_dto.generation_model.is_gemini_image_model:
                        try:
                            client = GenAIModelSetup.get_global_client(
                                project_id=target_project_id
                            )
                        except Exception as global_err:
                            worker_logger.warning(
                                "Falling back to regional GenAI client: %s",
                                global_err,
                            )
                            client = GenAIModelSetup.init(
                                project_id=target_project_id
                            )
                    else:
                        client = GenAIModelSetup.init(
                            project_id=target_project_id
                        )

                    start_time = time.monotonic()
                    effective_bucket = target_bucket_name or cfg.GENMEDIA_BUCKET
                    gcs_output_directory = f"gs://{effective_bucket}"

                    if request_dto.enhance_prompt:
                        rewritten_prompt = (
                            await gemini_service.enhance_prompt_from_dto(
                                dto=request_dto,
                                target_type=PromptTargetEnum.IMAGE,
                            )
                        )
                        request_dto.prompt = rewritten_prompt
                    else:
                        rewritten_prompt = request_dto.prompt

                    source_assets: list[SourceAssetLink] = []
                    source_media_items_list: list[SourceMediaItemLink] = []
                    reference_images_for_api: list[types.Image] = []
                    reference_parts_for_api: list[types.Part] = []
                    grounding_metadata = None

                    try:
                        if request_dto.source_asset_ids:
                            for asset_id in request_dto.source_asset_ids:
                                source_asset = (
                                    await source_asset_repo.get_by_id(asset_id)
                                )
                                if source_asset:
                                    ext_url = (
                                        source_asset.external_url
                                        if isinstance(
                                            getattr(
                                                source_asset,
                                                "external_url",
                                                None,
                                            ),
                                            str,
                                        )
                                        else None
                                    )
                                    is_youtube = getattr(
                                        source_asset, "asset_type", None
                                    ) == AssetTypeEnum.YOUTUBE_VIDEO or bool(
                                        ext_url
                                        and extract_youtube_video_id(ext_url)
                                    )
                                    if is_youtube and ext_url:
                                        source_assets.append(
                                            SourceAssetLink(
                                                asset_id=asset_id,
                                                role=AssetRoleEnum.YOUTUBE_VIDEO_REFERENCE,
                                            ),
                                        )
                                        reference_parts_for_api.append(
                                            types.Part.from_uri(
                                                file_uri=ext_url,
                                                mime_type="video/mp4",
                                            )
                                        )
                                    elif getattr(source_asset, "gcs_uri", None):
                                        source_assets.append(
                                            SourceAssetLink(
                                                asset_id=asset_id,
                                                role=AssetRoleEnum.INPUT,
                                            ),
                                        )
                                        reference_images_for_api.append(
                                            types.Image(
                                                gcs_uri=source_asset.gcs_uri,
                                                mime_type=source_asset.mime_type,
                                            ),
                                        )
                                    else:
                                        worker_logger.warning(
                                            "Source asset with ID %s has no "
                                            "GCS URI or external URL.",
                                            asset_id,
                                        )
                                else:
                                    worker_logger.warning(
                                        "Source asset with ID %s not found.",
                                        asset_id,
                                    )

                        if request_dto.source_media_items:
                            for gen_input in request_dto.source_media_items:
                                source_media_items_list.append(gen_input)
                                parent_item = await media_repo.get_by_id(
                                    gen_input.media_item_id,
                                )
                                if (
                                    parent_item
                                    and parent_item.gcs_uris
                                    and 0
                                    <= gen_input.media_index
                                    < len(parent_item.gcs_uris)
                                ):
                                    gcs_uri = parent_item.gcs_uris[
                                        gen_input.media_index
                                    ]
                                    is_youtube = isinstance(
                                        gcs_uri, str
                                    ) and bool(
                                        extract_youtube_video_id(gcs_uri)
                                    )
                                    if is_youtube:
                                        reference_parts_for_api.append(
                                            types.Part.from_uri(
                                                file_uri=gcs_uri,
                                                mime_type="video/mp4",
                                            )
                                        )
                                    else:
                                        reference_images_for_api.append(
                                            types.Image(
                                                gcs_uri=gcs_uri,
                                                mime_type=parent_item.mime_type,
                                            ),
                                        )
                                else:
                                    worker_logger.warning(
                                        "Could not find or use "
                                        "generated_input: %s at index %s",
                                        gen_input.media_item_id,
                                        gen_input.media_index,
                                    )

                        if request_dto.reference_video:
                            ref = request_dto.reference_video
                            if ref.type == "media_item":
                                parent_item = await media_repo.get_by_id(ref.id)
                                if parent_item and parent_item.gcs_uris:
                                    index = ref.index or 0
                                    if not (
                                        0 <= index < len(parent_item.gcs_uris)
                                    ):
                                        raise ValueError(
                                            f"Reference media item {ref.id} "
                                            f"index {index} is out of bounds."
                                        )
                                    ref_uri = parent_item.gcs_uris[index]
                                    is_youtube = isinstance(
                                        ref_uri, str
                                    ) and bool(
                                        extract_youtube_video_id(ref_uri)
                                    )
                                    if is_youtube:
                                        reference_parts_for_api.append(
                                            types.Part.from_uri(
                                                file_uri=ref_uri,
                                                mime_type="video/mp4",
                                            )
                                        )
                                        source_media_items_list.append(
                                            SourceMediaItemLink(
                                                media_item_id=ref.id,
                                                media_index=index,
                                                role=AssetRoleEnum.YOUTUBE_VIDEO_REFERENCE,
                                            )
                                        )
                                    else:
                                        mime_str = getattr(
                                            parent_item.mime_type,
                                            "value",
                                            parent_item.mime_type,
                                        )
                                        reference_parts_for_api.append(
                                            types.Part.from_uri(
                                                file_uri=ref_uri,
                                                mime_type=mime_str,
                                            )
                                        )
                                        source_media_items_list.append(
                                            SourceMediaItemLink(
                                                media_item_id=ref.id,
                                                media_index=index,
                                                role=AssetRoleEnum.VIDEO_REFERENCE,
                                            )
                                        )
                                else:
                                    raise ValueError(
                                        f"Reference media item {ref.id} not "
                                        "found or has no uris."
                                    )
                            else:
                                video_asset = await source_asset_repo.get_by_id(
                                    ref.id
                                )
                                if video_asset:
                                    ext_url = (
                                        video_asset.external_url
                                        if isinstance(
                                            getattr(
                                                video_asset,
                                                "external_url",
                                                None,
                                            ),
                                            str,
                                        )
                                        else None
                                    )
                                    is_youtube = getattr(
                                        video_asset, "asset_type", None
                                    ) == AssetTypeEnum.YOUTUBE_VIDEO or bool(
                                        ext_url
                                        and extract_youtube_video_id(ext_url)
                                    )
                                    if is_youtube and ext_url:
                                        reference_parts_for_api.append(
                                            types.Part.from_uri(
                                                file_uri=ext_url,
                                                mime_type="video/mp4",
                                            )
                                        )
                                        source_assets.append(
                                            SourceAssetLink(
                                                asset_id=ref.id,
                                                role=AssetRoleEnum.YOUTUBE_VIDEO_REFERENCE,
                                            )
                                        )
                                    elif getattr(video_asset, "gcs_uri", None):
                                        mime_str = getattr(
                                            video_asset.mime_type,
                                            "value",
                                            video_asset.mime_type,
                                        )
                                        reference_parts_for_api.append(
                                            types.Part.from_uri(
                                                file_uri=video_asset.gcs_uri,
                                                mime_type=mime_str,
                                            )
                                        )
                                        source_assets.append(
                                            SourceAssetLink(
                                                asset_id=ref.id,
                                                role=AssetRoleEnum.VIDEO_REFERENCE,
                                            )
                                        )
                                    else:
                                        raise ValueError(
                                            f"Reference video asset {ref.id} "
                                            "has no valid URI/URL."
                                        )
                                else:
                                    raise ValueError(
                                        f"Reference video asset {ref.id} "
                                        "not found."
                                    )

                        if request_dto.external_url:
                            youtube_url = request_dto.external_url
                            user_id = getattr(current_user, "id", None)
                            ws_id = request_dto.workspace_id

                            media_item_obj = await media_repo.get_by_id(
                                media_item_id
                            )
                            if media_item_obj is not None:
                                if isinstance(
                                    getattr(
                                        media_item_obj, "workspace_id", None
                                    ),
                                    int,
                                ):
                                    ws_id = media_item_obj.workspace_id
                                if isinstance(
                                    getattr(media_item_obj, "user_id", None),
                                    int,
                                ):
                                    user_id = media_item_obj.user_id
                                elif isinstance(
                                    getattr(media_item_obj, "user_email", None),
                                    str,
                                ):
                                    resolved_user = (
                                        await user_repo.get_by_email(
                                            media_item_obj.user_email
                                        )
                                    )
                                    if resolved_user and isinstance(
                                        getattr(resolved_user, "id", None), int
                                    ):
                                        user_id = resolved_user.id

                            if not user_id:
                                raise ValueError(
                                    "User ID could not be determined."
                                )

                            youtube_asset = (
                                await source_asset_repo.find_by_external_url(
                                    user_id=user_id,
                                    external_url=youtube_url,
                                )
                            )
                            if not youtube_asset:
                                new_asset_model = SourceAssetModel(
                                    workspace_id=ws_id,
                                    user_id=user_id,
                                    gcs_uri=None,
                                    original_filename="YouTube Video",
                                    mime_type=MimeTypeEnum.VIDEO_MP4,
                                    aspect_ratio=AspectRatioEnum(
                                        get_youtube_aspect_ratio(youtube_url)
                                    ),
                                    file_hash=None,
                                    scope=AssetScopeEnum.PRIVATE,
                                    asset_type=AssetTypeEnum.YOUTUBE_VIDEO,
                                    external_url=youtube_url,
                                )
                                youtube_asset = await source_asset_repo.create(
                                    new_asset_model
                                )

                            source_assets.append(
                                SourceAssetLink(
                                    asset_id=youtube_asset.id,
                                    role=AssetRoleEnum.YOUTUBE_VIDEO_REFERENCE,
                                )
                            )
                            reference_parts_for_api.append(
                                types.Part.from_uri(
                                    file_uri=youtube_url,
                                    mime_type="video/mp4",
                                )
                            )

                        all_generated_images: list[types.GeneratedImage] = []

                        # --- PATH 1: TEXT-TO-IMAGE GENERATION ---
                        if (
                            not reference_images_for_api
                            and not reference_parts_for_api
                        ):
                            model = request_dto.generation_model
                            if model.is_gemini_image_model:
                                tasks = [
                                    asyncio.to_thread(
                                        gemini_generate_image,
                                        gcs_service=gcs_service,
                                        vertexai_client=client,
                                        prompt=request_dto.prompt,
                                        model=request_dto.generation_model,
                                        bucket_name=gcs_service.bucket_name,
                                        aspect_ratio=request_dto.aspect_ratio,
                                        google_search=request_dto.google_search,
                                        resolution=request_dto.resolution,
                                        temperature=request_dto.temperature,
                                    )
                                    for _ in range(request_dto.number_of_media)
                                ]
                                gemini_images_response = await asyncio.gather(
                                    *tasks
                                )
                                all_generated_images = [
                                    img
                                    for img, _ in gemini_images_response
                                    if img
                                ]
                                if (
                                    gemini_images_response
                                    and gemini_images_response[0][1]
                                ):
                                    grounding_metadata = gemini_images_response[
                                        0
                                    ][1]
                            else:
                                for attempt in range(3):
                                    try:
                                        images_imagen_response = await asyncio.to_thread(
                                            client.models.generate_images,
                                            model=request_dto.generation_model,
                                            prompt=request_dto.prompt,
                                            config=types.GenerateImagesConfig(
                                                number_of_images=(
                                                    request_dto.number_of_media
                                                ),
                                                output_gcs_uri=(
                                                    gcs_output_directory
                                                ),
                                                aspect_ratio=getattr(
                                                    request_dto.aspect_ratio,
                                                    "value",
                                                    request_dto.aspect_ratio,
                                                ),
                                                negative_prompt=(
                                                    request_dto.negative_prompt
                                                ),
                                                add_watermark=(
                                                    request_dto.add_watermark
                                                ),
                                                image_size=request_dto.resolution,
                                            ),
                                        )
                                        break
                                    except Exception as e:
                                        if "429" in str(e) and attempt < 2:
                                            time.sleep(
                                                2**attempt + random.random()
                                            )
                                            continue
                                        raise e
                                all_generated_images = (
                                    images_imagen_response.generated_images
                                    or []
                                )
                        # --- PATH 2: IMAGE EDITING (IMAGE-TO-IMAGE / VIDEO REF) ---
                        elif request_dto.generation_model.is_gemini_image_model:
                            tasks = [
                                asyncio.to_thread(
                                    gemini_generate_image,
                                    gcs_service=gcs_service,
                                    vertexai_client=client,
                                    model=request_dto.generation_model,
                                    prompt=request_dto.prompt,
                                    bucket_name=gcs_service.bucket_name,
                                    reference_images=reference_images_for_api,
                                    reference_parts=reference_parts_for_api,
                                    aspect_ratio=request_dto.aspect_ratio,
                                    google_search=request_dto.google_search,
                                    resolution=request_dto.resolution,
                                    temperature=request_dto.temperature,
                                )
                                for _ in range(request_dto.number_of_media)
                            ]
                            gemini_images_response = await asyncio.gather(
                                *tasks
                            )
                            all_generated_images = [
                                img for img, _ in gemini_images_response if img
                            ]
                            if (
                                gemini_images_response
                                and gemini_images_response[0][1]
                            ):
                                grounding_metadata = gemini_images_response[0][
                                    1
                                ]
                        else:
                            raw_ref_image = types._ReferenceImageAPI(
                                reference_id=1,
                                reference_image=reference_images_for_api[0],
                            )
                            for attempt in range(3):
                                try:
                                    response = await asyncio.to_thread(
                                        client.models.edit_image,
                                        model=request_dto.generation_model,
                                        prompt=request_dto.prompt,
                                        reference_images=[raw_ref_image],
                                        config=types.EditImageConfig(
                                            edit_mode=(
                                                types.EditMode.EDIT_MODE_DEFAULT
                                            ),
                                            number_of_images=(
                                                request_dto.number_of_media
                                            ),
                                            output_gcs_uri=gcs_output_directory,
                                        ),
                                    )
                                    break
                                except Exception as e:
                                    if "429" in str(e) and attempt < 2:
                                        time.sleep(2**attempt + random.random())
                                        continue
                                    raise e
                            all_generated_images.extend(
                                response.generated_images or [],
                            )

                        if not all_generated_images:
                            await media_repo.update(
                                media_item_id,
                                {
                                    "status": JobStatusEnum.FAILED,
                                    "error_message": "No images generated",
                                },
                            )
                            return

                        # --- UNIFIED PROCESSING AND SAVING ---
                        valid_generated_images = [
                            img
                            for img in all_generated_images
                            if img.image and img.image.gcs_uri
                        ]
                        mime_type: MimeTypeEnum = (
                            MimeTypeEnum.IMAGE_PNG
                            if valid_generated_images[0].image
                            and valid_generated_images[0].image.mime_type
                            == MimeTypeEnum.IMAGE_PNG
                            else MimeTypeEnum.IMAGE_JPEG
                        )

                        # 1. Upscale images if needed
                        if request_dto.upscale_factor:
                            upscale_dtos: list[UpscaleImagenDto] = [
                                UpscaleImagenDto(
                                    generation_model=(
                                        GenerationModelEnum.GEMINI_3_1_FLASH_IMAGE
                                    ),
                                    user_image=img.image.gcs_uri or "",
                                    mime_type=(
                                        MimeTypeEnum.IMAGE_PNG
                                        if img.image.mime_type
                                        == MimeTypeEnum.IMAGE_PNG.value
                                        else MimeTypeEnum.IMAGE_JPEG
                                    ),
                                    upscale_factor=request_dto.upscale_factor,
                                )
                                for img in valid_generated_images
                                if img.image
                            ]
                            service = ImagenService(
                                media_repo=media_repo,
                                source_asset_repo=source_asset_repo,
                                gemini_service=gemini_service,
                                gcs_service=gcs_service,
                                iam_signer_credentials=iam_signer_credentials,
                            )
                            tasks = [
                                service.upscale_image(request_dto=dto)
                                for dto in upscale_dtos
                            ]
                            upscale_images = await asyncio.gather(*tasks)

                            permanent_gcs_uris = [
                                img.image.gcs_uri
                                for img in upscale_images
                                if img and img.image and img.image.gcs_uri
                            ]
                        else:
                            permanent_gcs_uris = [
                                img.image.gcs_uri
                                for img in valid_generated_images
                                if img.image and img.image.gcs_uri
                            ]

                        thumbnail_uris = []
                        for uri in permanent_gcs_uris:
                            thumb_uri = generate_image_thumbnail_from_gcs(
                                gcs_service,
                                uri,
                                mime_type.value,
                            )
                            if thumb_uri:
                                thumbnail_uris.append(thumb_uri)
                            else:
                                thumbnail_uris.append(uri)

                        end_time = time.monotonic()
                        generation_time = end_time - start_time

                        update_data = {
                            "status": JobStatusEnum.COMPLETED,
                            "prompt": rewritten_prompt,
                            "gcs_uris": permanent_gcs_uris,
                            "thumbnail_uris": thumbnail_uris,
                            "generation_time": generation_time,
                            "num_media": len(permanent_gcs_uris),
                            "grounding_metadata": grounding_metadata,
                            "source_assets": (
                                [sa.model_dump() for sa in source_assets]
                                if source_assets
                                else None
                            ),
                            "source_media_items": (
                                [
                                    smi.model_dump()
                                    for smi in source_media_items_list
                                ]
                                if source_media_items_list
                                else None
                            ),
                            "mime_type": mime_type,
                        }
                        await media_repo.update(media_item_id, update_data)
                        worker_logger.info(
                            "Successfully processed image job %s",
                            media_item_id,
                        )

                    except Exception as e:
                        worker_logger.error(
                            "Image generation API call failed: %s",
                            e,
                        )
                        await media_repo.update(
                            media_item_id,
                            {
                                "status": JobStatusEnum.FAILED,
                                "error_message": str(e),
                            },
                        )

        loop.run_until_complete(_async_worker())
        loop.close()

    except Exception as e:
        worker_logger.error(
            "Image generation task failed: %s", e, exc_info=True
        )
        # We can't easily update DB here if the loop failed or session failed,
        # but we can try to create a fresh one if needed, or just log.
        # For now, just log as we might not have a loop.


# --- STANDALONE WORKER FUNCTION ---
def _process_upload_upscale_in_background(
    media_item_id: int,
    workspace_id: int,
    user: UserModel,
    gcs_uri: str,
    file_bytes: bytes | None,
    filename: str | None,
    upscale_factor: str | None,
    # original_filename is unused
    aspect_ratio: AspectRatioEnum | None,
    asset_type: AssetTypeEnum | None = None,
    source_asset_id: str | None = None,
    media_item_id_existing: int | None = None,
    mime_type: str | None = None,
    scope: AssetScopeEnum | None = None,
    enhance_input_image: bool | None = None,
    image_preservation_factor: float | None = None,
):
    """Background worker to handle image upscale, GCS upload, and DB update."""
    # time is removed

    # google.cloud.logging moved to top level

    # Inner imports moved or removed
    # Remaining redundant inner imports removed
    # Redundant inner imports removed
    from src.source_assets.source_asset_service import SourceAssetService

    worker_logger = logging.getLogger(f"upscale_worker.{media_item_id}")
    worker_logger.setLevel(logging.INFO)

    try:
        # Configure logging for the worker process
        if worker_logger.hasHandlers():
            worker_logger.handlers.clear()

        if os.getenv("ENVIRONMENT") == "production":
            log_client = LoggerClient()
            handler = CloudLoggingHandler(
                log_client,
                name=f"upscale_worker.{media_item_id}",
            )
            worker_logger.addHandler(handler)
        else:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                "%(asctime)s - [UPSCLAE_WORKER] - %(levelname)s - %(message)s",
            )
            handler.setFormatter(formatter)
            worker_logger.addHandler(handler)

        # Create a new event loop for this process
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # WorkerDatabase moved to top level

        async def _async_worker():
            async with WorkerDatabase() as db_factory:
                async with db_factory() as db:
                    # Instantiate Repositories
                    media_repo = MediaRepository(db)
                    source_asset_repo = SourceAssetRepository(db)
                    brand_repo = BrandGuidelineRepository(db)
                    workspace_repo = WorkspaceRepository(db)

                    target_project_id: str | None = None
                    target_bucket_name: str | None = None
                    if workspace_id:
                        try:
                            ws = await workspace_repo.get_by_id(workspace_id)
                            if ws:
                                if isinstance(
                                    getattr(ws, "gcp_project_id", None), str
                                ):
                                    target_project_id = ws.gcp_project_id
                                if isinstance(
                                    getattr(ws, "gcs_bucket_name", None), str
                                ):
                                    target_bucket_name = ws.gcs_bucket_name
                        except Exception as e:
                            worker_logger.warning(
                                "Could not fetch workspace config: %s", e
                            )

                    # Instantiate Services
                    iam_signer = IamSignerCredentials()
                    gcs_service = GcsService(
                        bucket_name=target_bucket_name,
                        project_id=target_project_id,
                    )
                    gemini_service = GeminiService(
                        brand_guideline_repo=brand_repo
                    )

                    # Instantiate ImagenService
                    imagen_service = ImagenService(
                        iam_signer_credentials=iam_signer,
                        media_repo=media_repo,
                        gemini_service=gemini_service,
                        gcs_service=gcs_service,
                        source_asset_repo=source_asset_repo,
                    )

                    # Instantiate SourceAssetService for upload handling
                    source_asset_service = SourceAssetService(
                        repo=source_asset_repo,
                        gcs_service=gcs_service,
                        iam_signer=iam_signer,
                        imagen_service=imagen_service,
                    )

                    final_upscaled_uri = None
                    final_original_uri = gcs_uri
                    used_source_asset_id = source_asset_id

                    try:
                        start_time = time.monotonic()

                        # --- Case 1: New file upload ---
                        if file_bytes:
                            if not filename:
                                raise ValueError(
                                    "Filename is required for new file "
                                    "uploads.",
                                )

                            # Use SourceAssetService to handle upload and
                            # upscaling
                            asset_response = (
                                await source_asset_service.upload_asset(
                                    user=user,
                                    file_bytes=file_bytes,
                                    filename=filename,
                                    workspace_id=workspace_id,
                                    mime_type=mime_type,
                                    scope=scope,
                                    asset_type=asset_type,
                                    aspect_ratio=aspect_ratio,
                                    upscale_factor=upscale_factor,
                                    enhance_input_image=enhance_input_image,
                                    image_preservation_factor=(
                                        image_preservation_factor
                                    ),
                                )
                            )

                            final_upscaled_uri = asset_response.gcs_uri
                            final_original_uri = asset_response.original_gcs_uri
                            used_source_asset_id = asset_response.id

                        # --- Case 2: Existing SourceAsset ---
                        elif source_asset_id:
                            existing_asset = await source_asset_repo.get_by_id(
                                source_asset_id,
                            )
                            if not existing_asset:
                                raise ValueError(
                                    f"Source asset {source_asset_id} not found",
                                )
                            final_original_uri = existing_asset.gcs_uri
                            used_source_asset_id = existing_asset.id

                        # --- Case 3: Existing MediaItem ---
                        elif media_item_id_existing:
                            existing_media = await media_repo.get_by_id(
                                media_item_id_existing,
                            )
                            if not existing_media:
                                raise ValueError(
                                    f"Media item {media_item_id_existing} "
                                    f"not found",
                                )

                            if existing_media.original_gcs_uris:
                                final_original_uri = (
                                    existing_media.original_gcs_uris[0]
                                )
                            elif existing_media.gcs_uris:
                                final_original_uri = existing_media.gcs_uris[0]
                            else:
                                raise ValueError(
                                    f"Media item {media_item_id_existing} "
                                    f"has no usable URIs",
                                )

                        # --- Perform Upscaling ---
                        if not final_original_uri and not final_upscaled_uri:
                            raise ValueError(
                                "No valid source URI found for upscaling."
                            )

                        if not final_upscaled_uri:
                            if upscale_factor:
                                upscale_model = (
                                    GenerationModelEnum.GEMINI_3_1_FLASH_IMAGE
                                )
                                upscale_dto = UpscaleImagenDto(
                                    user_image=final_original_uri,
                                    upscale_factor=upscale_factor,
                                    mime_type=MimeTypeEnum.IMAGE_PNG,
                                    generation_model=upscale_model,
                                    enhance_input_image=enhance_input_image
                                    or False,
                                    image_preservation_factor=(
                                        image_preservation_factor
                                    ),
                                )

                                upscaled_result = (
                                    await imagen_service.upscale_image(
                                        upscale_dto,
                                    )
                                )

                                if (
                                    upscaled_result
                                    and upscaled_result.image
                                    and upscaled_result.image.gcs_uri
                                ):
                                    final_upscaled_uri = (
                                        upscaled_result.image.gcs_uri
                                    )
                                else:
                                    raise ValueError(
                                        "Upscaling returned no URI"
                                    )
                            else:
                                final_upscaled_uri = final_original_uri

                        # --- Finalize ---
                        source_assets_list = []
                        if used_source_asset_id:
                            source_assets_list.append(
                                SourceAssetLink(
                                    asset_id=int(used_source_asset_id),
                                    role=AssetRoleEnum.INPUT,
                                ).model_dump(),
                            )

                        # Generate thumbnail
                        thumbnail_uris = []
                        if final_upscaled_uri:
                            thumb_uri = generate_image_thumbnail_from_gcs(
                                gcs_service,
                                final_upscaled_uri,
                                MimeTypeEnum.IMAGE_PNG.value,
                            )
                            if thumb_uri:
                                thumbnail_uris.append(thumb_uri)
                            else:
                                thumbnail_uris.append(final_upscaled_uri)

                        end_time = time.monotonic()
                        generation_time = end_time - start_time

                        update_data = {
                            "status": JobStatusEnum.COMPLETED,
                            "gcs_uris": [final_upscaled_uri],
                            "original_gcs_uris": (
                                [final_original_uri]
                                if final_original_uri
                                else []
                            ),
                            "thumbnail_uris": thumbnail_uris,
                            "generation_time": generation_time,
                            "num_media": 1,
                            "mime_type": MimeTypeEnum.IMAGE_PNG,
                            "source_assets": (
                                source_assets_list
                                if source_assets_list
                                else None
                            ),
                        }
                        await media_repo.update(media_item_id, update_data)
                        worker_logger.info(
                            "Upscale job %s completed successfully.",
                            media_item_id,
                        )

                    except Exception as e:
                        worker_logger.error(
                            "Upscale job failure: %s",
                            e,
                            exc_info=True,
                        )
                        await media_repo.update(
                            media_item_id,
                            {
                                "status": JobStatusEnum.FAILED,
                                "error_message": str(e),
                            },
                        )

        loop.run_until_complete(_async_worker())
        loop.close()

    except Exception as e:
        worker_logger.error(
            "Image generation task failed: %s", e, exc_info=True
        )


class ImagenService:
    """Service for handling image generation and upscaling."""

    def __init__(
        self,
        iam_signer_credentials: IamSignerCredentials = Depends(),
        media_repo: MediaRepository = Depends(),
        gemini_service: GeminiService = Depends(),
        gcs_service: GcsService = Depends(),
        source_asset_repo: SourceAssetRepository = Depends(),
    ):
        """Initializes the service with its dependencies."""
        self.iam_signer_credentials = iam_signer_credentials
        self.media_repo = media_repo
        self.gemini_service = gemini_service
        self.gcs_service = gcs_service
        self.source_asset_repo = source_asset_repo
        self.cfg = config_service

    async def start_upload_upscale_job(
        self,
        user: UserModel,
        executor: ThreadPoolExecutor,
        workspace_id: int,
        gcs_uri: str,
        mime_type: str,
        scope: AssetScopeEnum | None = None,
        file_bytes: bytes | None = None,
        filename: str | None = None,
        source_asset_id: str | None = None,
        media_item_id_existing: int | None = None,
        upscale_factor: str | None = None,
        aspect_ratio: AspectRatioEnum | None = None,
        asset_type: AssetTypeEnum | None = None,
        enhance_input_image: bool | None = None,
        image_preservation_factor: float | None = None,
    ) -> MediaItemResponse:
        """Starts a background job to upload and/or upscale an image."""
        target_gcs_uri = None
        if not file_bytes:
            if source_asset_id:
                try:
                    asset = await self.source_asset_repo.get_by_id(
                        int(source_asset_id)
                    )
                    if not asset:
                        raise ValueError(
                            f"Source asset {source_asset_id} not found"
                        )
                    target_gcs_uri = asset.gcs_uri
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid source asset id: {source_asset_id}"
                    ) from exc

            elif media_item_id_existing:
                media = await self.media_repo.get_by_id(media_item_id_existing)
                if not media:
                    raise ValueError(
                        f"Media item {media_item_id_existing} not found"
                    )
                target_gcs_uri = media.gcs_uris[0] if media.gcs_uris else None

        if target_gcs_uri and not file_bytes:
            image_bytes = self.gcs_service.download_bytes_from_gcs(
                target_gcs_uri
            )
            if image_bytes:
                try:
                    pil_image = PILImage.open(io.BytesIO(image_bytes))
                    max_output_pixels = 40000000

                    current_pixels = pil_image.width * pil_image.height
                    factor_int = 2
                    if upscale_factor == "x4":
                        factor_int = 4
                    elif upscale_factor == "x3":
                        factor_int = 3

                    projected_pixels = current_pixels * (
                        factor_int * factor_int
                    )

                    if projected_pixels > max_output_pixels:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=(
                                f"Image is too large for upscaling to "
                                f"{upscale_factor} times, reduce the Upscale "
                                f"Factor and try again."
                            ),
                        )
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(
                        "Failed to validate existing image resolution for "
                        "user %s: %s",
                        user.email,
                        e,
                    )

        placeholder_item = MediaItemModel(
            workspace_id=workspace_id,
            user_email=user.email,
            user_id=user.id,
            mime_type=MimeTypeEnum.IMAGE_PNG,
            model=GenerationModelEnum.GEMINI_3_1_FLASH_IMAGE,
            status=JobStatusEnum.PROCESSING,
            aspect_ratio=aspect_ratio or AspectRatioEnum.RATIO_1_1,
            gcs_uris=[],
            original_gcs_uris=[],
            prompt="",
            original_prompt="Upscale Job",
        )

        created_item = await self.media_repo.create(placeholder_item)
        media_item_id = created_item.id

        executor.submit(
            _process_upload_upscale_in_background,
            media_item_id=media_item_id,
            workspace_id=workspace_id,
            user=user,
            gcs_uri=gcs_uri,
            file_bytes=file_bytes,
            filename=filename,
            upscale_factor=upscale_factor,
            aspect_ratio=aspect_ratio,
            asset_type=asset_type,
            source_asset_id=source_asset_id,
            media_item_id_existing=media_item_id_existing,
            mime_type=mime_type,
            scope=scope,
            enhance_input_image=enhance_input_image,
            image_preservation_factor=image_preservation_factor,
        )

        logger.info(
            "Image upscale job successfully queued.",
            extra={
                "json_fields": {
                    "media_id": placeholder_item.id,
                    "user_email": user.email,
                    "model": GenerationModelEnum.GEMINI_3_1_FLASH_IMAGE.value,
                },
            },
        )

        return MediaItemResponse(
            **created_item.model_dump(),
            presigned_urls=[],
            original_presigned_urls=[],
        )

    async def start_image_generation_job(
        self,
        request_dto: CreateImagenDto,
        user: UserModel,
        executor: ThreadPoolExecutor,
    ) -> MediaItemResponse:
        """Immediately creates a placeholder MediaItem and starts the image
        generation in the background.
        """
        # Create a placeholder document
        placeholder_item = MediaItemModel(
            workspace_id=request_dto.workspace_id,
            user_email=user.email,
            user_id=user.id,
            # Default to PNG, will update if needed
            mime_type=MimeTypeEnum.IMAGE_PNG,
            model=request_dto.generation_model,
            original_prompt=request_dto.prompt,
            status=JobStatusEnum.PROCESSING,
            aspect_ratio=request_dto.aspect_ratio,
            style=request_dto.style,
            lighting=request_dto.lighting,
            color_and_tone=request_dto.color_and_tone,
            composition=request_dto.composition,
            negative_prompt=request_dto.negative_prompt,
            google_search=request_dto.google_search,
            resolution=request_dto.resolution,
            gcs_uris=[],
        )

        # Save the placeholder to the database immediately
        placeholder_item = await self.media_repo.create(placeholder_item)

        # Submit the long-running function to the process pool
        executor.submit(
            _process_image_in_background,
            media_item_id=placeholder_item.id,
            request_dto=request_dto,
            current_user=user,
        )

        logger.info(
            "Image generation job successfully queued.",
            extra={
                "json_fields": {
                    "media_id": placeholder_item.id,
                    "user_email": user.email,
                    "model": request_dto.generation_model,
                },
            },
        )

        return MediaItemResponse(
            **placeholder_item.model_dump(),
            presigned_urls=[],
            presigned_thumbnail_urls=[],
        )

    async def start_vto_generation_job(
        self,
        request_dto: VtoDto,
        user: UserModel,
        executor: ThreadPoolExecutor,
    ) -> MediaItemResponse:
        """Immediately creates a placeholder MediaItem and starts the VTO
        generation in the background.

        Returns:
            The initial MediaItem with a 'processing' status and a
            pre-generated ID.

        """
        # 2. Create a placeholder document
        # Do not allow manually setting ID for auto-increment columns
        placeholder_item = MediaItemModel(
            workspace_id=request_dto.workspace_id,
            user_email=user.email,
            user_id=user.id,
            mime_type=MimeTypeEnum.IMAGE_PNG,
            model=GenerationModelEnum.VTO,
            aspect_ratio=AspectRatioEnum.RATIO_9_16,
            original_prompt="",
            prompt="",
            status=JobStatusEnum.PROCESSING,
            gcs_uris=[],
        )

        # 3. Save the placeholder to the database immediately
        created_item = await self.media_repo.create(placeholder_item)

        # 4. Submit the long-running function to the process pool
        executor.submit(
            _process_vto_in_background,
            media_item_id=created_item.id,
            request_dto=request_dto,
            current_user=user,
        )

        logger.info(
            "VTO generation job successfully queued.",
            extra={
                "json_fields": {
                    "message": "VTO generation job successfully queued.",
                    "media_id": created_item.id,
                    "user_email": user.email,
                    "user_id": user.id,
                },
            },
        )

        # 5. Return the placeholder to the frontend
        return MediaItemResponse(
            **created_item.model_dump(),
            presigned_urls=[],
        )

    async def get_media_item_with_presigned_urls(
        self,
        media_id: str,
    ) -> MediaItemResponse | None:
        """Fetches a MediaItem by its ID and enriches it with presigned URLs.

        Args:
            media_id: The unique ID of the media item.

        Returns:
            A MediaItemResponse object with presigned URLs, or None if
            not found.

        """
        # 1. Fetch the base document from Firestore
        media_item = await self.media_repo.get_by_id(media_id)
        if not media_item:
            return None

        # 2. Create tasks to generate all presigned URLs in parallel
        presigned_url_tasks = [
            asyncio.to_thread(
                self.iam_signer_credentials.generate_presigned_url, uri
            )
            for uri in media_item.gcs_uris
        ]

        # 3. Execute all URL generation tasks concurrently
        presigned_urls = await asyncio.gather(*presigned_url_tasks)

        # 4. Construct the final response DTO
        return MediaItemResponse(
            **media_item.model_dump(),
            presigned_urls=presigned_urls,
        )

    async def upscale_image(
        self,
        request_dto: UpscaleImagenDto,
    ) -> ImageGenerationResult | None:
        """Upscale an image using Nano Banana 2 (Gemini 3.1 Flash Image)."""
        import base64

        try:
            orig_bytes: bytes | None = None
            mime_type_str = (
                request_dto.mime_type.value
                if hasattr(request_dto.mime_type, "value")
                else request_dto.mime_type
            )

            if request_dto.user_image.startswith(
                "gs://"
            ) or request_dto.user_image.startswith("http"):
                image_for_api = types.Image(
                    gcs_uri=request_dto.user_image,
                    mime_type=mime_type_str,
                )
                if request_dto.user_image.startswith("gs://"):
                    downloaded = self.gcs_service.download_bytes_from_gcs(
                        request_dto.user_image,
                    )
                    if isinstance(downloaded, (bytes, bytearray)):
                        orig_bytes = bytes(downloaded)
            else:
                try:
                    orig_bytes = base64.b64decode(request_dto.user_image)
                    image_for_api = types.Image(
                        image_bytes=orig_bytes,
                        mime_type=mime_type_str,
                    )
                except Exception:
                    image_for_api = types.Image(
                        gcs_uri=request_dto.user_image,
                        mime_type=mime_type_str,
                    )

            orig_w, orig_h = None, None
            if orig_bytes:
                try:
                    with PILImage.open(io.BytesIO(orig_bytes)) as orig_pil:
                        orig_w, orig_h = orig_pil.size
                except Exception as dim_err:
                    logger.warning(
                        "Could not read input image dimensions: %s", dim_err
                    )

            factor_map = {"x2": 2, "x3": 3, "x4": 4}
            factor = factor_map.get(request_dto.upscale_factor or "x2", 2)

            target_w, target_h = None, None
            if orig_w and orig_h:
                raw_target_w = orig_w * factor
                raw_target_h = orig_h * factor
                max_4k_dim = 4096
                if max(raw_target_w, raw_target_h) > max_4k_dim:
                    scale = max_4k_dim / max(raw_target_w, raw_target_h)
                    target_w = max(1, round(raw_target_w * scale))
                    target_h = max(1, round(raw_target_h * scale))
                else:
                    target_w = raw_target_w
                    target_h = raw_target_h

            resolution = (
                "4K"
                if (target_w and target_h and max(target_w, target_h) > 2048)
                or request_dto.upscale_factor in ("x3", "x4")
                else "2K"
            )
            prompt_parts = [
                "Upscale and enhance this image to ultra-high resolution."
            ]

            if request_dto.enhance_input_image:
                prompt_parts.append(
                    "Apply active enhancement: aggressively suppress noise, "
                    "eliminate JPEG and compression artifacts, deblur, and "
                    "clean up compression distortions while restoring sharp "
                    "clarity."
                )

            if request_dto.image_preservation_factor is not None:
                preservation = request_dto.image_preservation_factor
                if preservation >= 0.75:
                    prompt_parts.append(
                        f"Image preservation factor is {preservation:.2f} "
                        "(strict preservation): Strictly and faithfully "
                        "preserve the exact pixel structure, composition, "
                        "colors, lighting, subjects, textures, background, "
                        "and text with minimal deviation from the original "
                        "image."
                    )
                elif preservation >= 0.4:
                    prompt_parts.append(
                        f"Image preservation factor is {preservation:.2f} "
                        "(balanced preservation): Maintain high fidelity to "
                        "the original composition, colors, and subject matter "
                        "while rendering enhanced high-frequency details and "
                        "clarity."
                    )
                else:
                    prompt_parts.append(
                        f"Image preservation factor is {preservation:.2f} "
                        "(creative detail generation): Prioritize "
                        "synthesizing rich, crisp, and high-frequency "
                        "textures and details while respecting the general "
                        "structure and subject of the original image."
                    )
            else:
                prompt_parts.append(
                    "Faithfully preserve the exact composition, colors, "
                    "lighting, subjects, background, and text from the "
                    "original image while increasing sharpness, removing "
                    "noise and compression artifacts, and rendering crisp "
                    "high-frequency details."
                )

            upscale_prompt = " ".join(prompt_parts)

            project_id = (
                self.gcs_service.project_id
                if isinstance(
                    getattr(self.gcs_service, "project_id", None), str
                )
                else None
            )
            global_client = GenAIModelSetup.get_global_client(
                project_id=project_id
            )
            generated_image_tuple = await asyncio.to_thread(
                gemini_generate_image,
                gcs_service=self.gcs_service,
                vertexai_client=global_client,
                prompt=upscale_prompt,
                model=request_dto.generation_model,
                bucket_name=self.gcs_service.bucket_name,
                reference_images=[image_for_api],
                resolution=resolution,
            )

            generated_image = (
                generated_image_tuple[0]
                if isinstance(generated_image_tuple, tuple)
                else generated_image_tuple
            )

            if (
                generated_image
                and generated_image.image
                and generated_image.image.gcs_uri
            ):
                final_gcs_uri = generated_image.image.gcs_uri

                if target_w and target_h:
                    gen_bytes = self.gcs_service.download_bytes_from_gcs(
                        final_gcs_uri,
                    )
                    if isinstance(gen_bytes, (bytes, bytearray)) and gen_bytes:
                        try:
                            with PILImage.open(
                                io.BytesIO(gen_bytes)
                            ) as gen_pil:
                                if gen_pil.size != (target_w, target_h):
                                    if gen_pil.mode not in ("RGB", "RGBA"):
                                        gen_pil = gen_pil.convert("RGB")
                                    resized_pil = gen_pil.resize(
                                        (target_w, target_h),
                                        PILImage.Resampling.LANCZOS,
                                    )
                                    out_io = io.BytesIO()
                                    resized_pil.save(out_io, format="PNG")
                                    file_name = (
                                        f"upscaled_"
                                        f"{request_dto.upscale_factor}_"
                                        f"{uuid.uuid4().hex[:8]}.png"
                                    )
                                    stored_uri = self.gcs_service.store_to_gcs(
                                        folder="upscaled_images",
                                        file_name=file_name,
                                        mime_type=MimeTypeEnum.IMAGE_PNG,
                                        contents=out_io.getvalue(),
                                        bucket_name=(
                                            self.gcs_service.bucket_name
                                        ),
                                        decode=False,
                                    )
                                    if stored_uri:
                                        final_gcs_uri = stored_uri
                        except Exception as resize_err:
                            logger.warning(
                                "Failed to resize upscaled image to target "
                                "dimensions: %s",
                                resize_err,
                            )

                return ImageGenerationResult(
                    enhanced_prompt=upscale_prompt,
                    rai_filtered_reason="",
                    image=CustomImagenResult(
                        gcs_uri=final_gcs_uri,
                        encoded_image="",
                        mime_type=MimeTypeEnum.IMAGE_PNG,
                        presigned_url="",
                    ),
                )
            raise ValueError(
                "Image upscaling generation failed or returned no data."
            )

        except Exception as e:
            logger.error("Image upscaling generation API call failed: %s", e)
            raise
