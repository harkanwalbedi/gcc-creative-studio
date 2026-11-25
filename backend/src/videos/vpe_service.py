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
"""Creative Studio's integration of Generative Media Pro / Veo Pro Experimental.

``src/videos/vpe/`` is a pure library: it knows the API and nothing about
this application. This module owns the gallery row, the buckets, the temporary
files and the background workers for Super Resolution, Seamless Upscaling,
Segmented Upscaling, Dialogue-Driven generation, Video Transform, Performance
Estimation, Omni-Perf, Omni-Cine, and Video Textures.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from fastapi import Depends, HTTPException
from google.cloud.logging import Client as LoggerClient
from google.cloud.logging.handlers import CloudLoggingHandler

from src.auth.iam_signer_credentials_service import IamSignerCredentials
from src.common.base_dto import (
    AspectRatioEnum,
    GenerationModelEnum,
    MimeTypeEnum,
)
from src.common.media_utils import generate_thumbnail
from src.common.schema.media_item_model import (
    AssetRoleEnum,
    JobStatusEnum,
    MediaItemModel,
    SourceMediaItemLink,
)
from src.common.storage_service import GcsService
from src.config.config_service import ConfigService, config_service
from src.database import WorkerDatabase
from src.galleries.dto.gallery_response_dto import MediaItemResponse
from src.images.repository.media_item_repository import MediaRepository
from src.source_assets.repository.source_asset_repository import (
    SourceAssetRepository,
)
from src.users.user_model import UserModel
from src.videos.dto.create_veo_dto import CreateVeoDto
from src.videos.dto.upscale_video_dto import UpscaleVideoDto
from src.videos.veo_service import (
    build_measured_metadata,
    record_worker_progress,
)
from src.videos.vpe.capabilities import (
    MIME_JPEG,
    MIME_MP3,
    MIME_MP4,
    MIME_MPEG,
    MIME_MXF,
    MIME_PNG,
    MIME_QUICKTIME,
    MIME_WAV,
    MIME_WEBP,
    SUPER_RES_4K_INPUT_SIZES,
    VpeCapabilityId,
    VpeSuperResMode,
    VpeUpscaleResolution,
    get_capability,
)
from src.videos.vpe.client import (
    VpeClient,
    VpeOperation,
    VpeResult,
    parse_result,
)
from src.videos.vpe.gating import (
    VpeScreeningResult,
    is_seamless_raw_data,
    screen_stored_video,
    screen_upscale_candidate,
)
from src.videos.vpe.media_ops import (
    concat_segments,
    cut_segment,
    extract_audio_wav,
    has_audio_stream,
    normalize_video_720p_24fps,
    postprocess_dialogue_video,
    postprocess_transform_video,
    prepare_dialogue_audio,
    prepare_dialogue_frame,
    prepare_transform_video,
    restore_audio,
    transcode_to_h264_preview,
)
from src.videos.vpe.payloads import (
    VpeAudioControl,
    VpeConditioningFrame,
    VpeMediaRef,
    VpeOmniVideoTransform,
    VpeRequest,
    VpeSeamlessFlags,
    build_payload,
)
from src.videos.vpe.preflight import (
    VpeMediaProbe,
    VpeReasonCode,
    probe_media,
    validate,
)
from src.videos.vpe.segmentation import VpeSegment, plan_segments
from src.workspaces.repository.workspace_repository import WorkspaceRepository

logger = logging.getLogger(__name__)

_MP4 = MimeTypeEnum.VIDEO_MP4.value

SEGMENT_TIMEOUT_SECONDS = 1800.0
MAX_SEGMENTS_PER_JOB = 8

_SEGMENTABLE_CODES = frozenset(
    {
        VpeReasonCode.FRAME_COUNT_TOO_HIGH,
        VpeReasonCode.MIME_TYPE_UNSUPPORTED,
    },
)

_SUPER_RES_MODE_MAP: dict[str, str] = {
    "4k": VpeSuperResMode.MODE_4K.value,
    "4k_pro": VpeSuperResMode.MODE_4K_PRO.value,
    "8k": VpeSuperResMode.MODE_8K.value,
}


class VpeJobError(RuntimeError):
    """Raised when a VPE / GMP job cannot be run as requested."""


def _uri_bucket_name(gcs_uri: str) -> str | None:
    """Extracts the bucket name from a gs://<bucket>/<path> URI."""
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        return None
    without_scheme = gcs_uri.removeprefix("gs://")
    bucket, sep, _ = without_scheme.partition("/")
    return bucket if (bucket and sep) else None


def _blob_path(gcs_uri: str) -> str:
    """Strips a gs:// URI down to the object path within its bucket."""
    without_scheme = gcs_uri.removeprefix("gs://")
    _, _, path = without_scheme.partition("/")
    return path


async def _resolve_workspace_gcs_config(
    db: Any,
    workspace_id: int | None,
    worker_logger: logging.Logger,
) -> tuple[str | None, str | None]:
    """Resolves (gcp_project_id, gcs_bucket_name) for a workspace if configured."""
    target_project_id: str | None = None
    target_bucket_name: str | None = None
    if workspace_id:
        try:
            ws = await WorkspaceRepository(db).get_by_id(workspace_id)
            if ws:
                if isinstance(getattr(ws, "gcp_project_id", None), str):
                    target_project_id = ws.gcp_project_id
                if isinstance(getattr(ws, "gcs_bucket_name", None), str):
                    target_bucket_name = ws.gcs_bucket_name
        except Exception as ws_err:  # noqa: BLE001
            worker_logger.warning(
                "Could not fetch workspace %s config: %s",
                workspace_id,
                ws_err,
            )
    return target_project_id, target_bucket_name


async def _resolve_asset_ref_uri(
    ref: Any,
    media_repo: Any,
    source_asset_repo: Any,
) -> str | None:
    """Resolves a GCS URI from an AssetReferenceDto."""
    if not ref:
        return None
    if getattr(ref, "type", None) == "media_item":
        parent_item = await media_repo.get_by_id(ref.id)
        if parent_item and parent_item.gcs_uris:
            idx = getattr(ref, "index", 0) or 0
            if 0 <= idx < len(parent_item.gcs_uris):
                return parent_item.gcs_uris[idx]
        return None
    asset = await source_asset_repo.get_by_id(ref.id)
    if asset:
        return asset.gcs_uri
    return None


async def _download_asset_gcs(
    uri: str,
    target_path: Path,
    app_gcs: GcsService,
    vpe_gcs: GcsService,
    fallback_project_id: str | None = None,
) -> bool:
    """Downloads a gs:// URI from its explicit bucket, falling back to app_gcs/vpe_gcs."""
    blob_path = _blob_path(uri)
    uri_bucket = _uri_bucket_name(uri)
    app_bucket = (
        app_gcs.bucket_name
        if isinstance(getattr(app_gcs, "bucket_name", None), str)
        else None
    )
    vpe_bucket = (
        vpe_gcs.bucket_name
        if isinstance(getattr(vpe_gcs, "bucket_name", None), str)
        else None
    )

    if uri_bucket and uri_bucket not in {app_bucket, vpe_bucket}:
        try:
            explicit_gcs = GcsService(
                bucket_name=uri_bucket,
                project_id=fallback_project_id,
            )
            downloaded = await asyncio.to_thread(
                explicit_gcs.download_from_gcs,
                gcs_uri_path=blob_path,
                destination_file_path=str(target_path),
            )
            if downloaded:
                return True
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "Cross-bucket download from gs://%s failed (%s); falling back.",
                uri_bucket,
                err,
            )

    if uri_bucket and uri_bucket == vpe_bucket:
        downloaded = await asyncio.to_thread(
            vpe_gcs.download_from_gcs,
            gcs_uri_path=blob_path,
            destination_file_path=str(target_path),
        )
        if downloaded:
            return True

    downloaded = await asyncio.to_thread(
        app_gcs.download_from_gcs,
        gcs_uri_path=blob_path,
        destination_file_path=str(target_path),
    )
    if not downloaded:
        downloaded = await asyncio.to_thread(
            vpe_gcs.download_from_gcs,
            gcs_uri_path=blob_path,
            destination_file_path=str(target_path),
        )
    return bool(downloaded)


def aspect_ratio_for(probe: VpeMediaProbe) -> str:
    """Returns the aspect ratio ('9:16' or '16:9') for a measured clip."""
    return "9:16" if probe.is_portrait else "16:9"


def check_upscalable(probe: VpeMediaProbe) -> None:
    """Refuses a source the segmented upscaler cannot be made to accept."""
    verdict = validate(probe, VpeCapabilityId.UPSCALE)
    fatal = [
        finding
        for finding in verdict.blocking
        if finding.code not in _SEGMENTABLE_CODES
    ]
    if fatal:
        raise VpeJobError(" ".join(finding.message for finding in fatal))


def check_super_res_upscalable(
    probe: VpeMediaProbe,
    super_res_mode: str,
) -> None:
    """Validates a probed source clip against GMP Super Resolution rules."""
    verdict = validate(probe, VpeCapabilityId.SUPER_RES)
    if verdict.blocking:
        raise VpeJobError(
            " ".join(finding.message for finding in verdict.blocking)
        )
    if (
        probe.frame_size in SUPER_RES_4K_INPUT_SIZES
        and super_res_mode != VpeSuperResMode.MODE_8K.value
    ):
        raise VpeJobError(
            "4K input videos are supported only with MODE_8K (8K output)."
        )


def plan_upscale(probe: VpeMediaProbe) -> tuple[VpeSegment, ...]:
    """Decides how a measured clip is divided for segmented upscaling."""
    if not probe.frame_count:
        raise VpeJobError(
            "The source clip's frames could not be counted, and the"
            " upscaler's limits are frame counts.",
        )
    constraints = get_capability(VpeCapabilityId.UPSCALE).input_video
    segments = plan_segments(probe.frame_count, constraints)
    if len(segments) > MAX_SEGMENTS_PER_JOB:
        raise VpeJobError(
            f"Upscaling this clip would take {len(segments)} separate jobs,"
            f" over the limit of {MAX_SEGMENTS_PER_JOB}. It is"
            f" {probe.frame_seconds:.0f} seconds long, which is an edit"
            " rather than a shot; upscale the shots before assembling them.",
        )
    return segments


def build_segment_request(
    *,
    segment_uri: str,
    storage_uri: str,
    aspect_ratio: str,
    resolution: VpeUpscaleResolution,
    sharpness: int | None,
    codec: str | None = None,
    compression_quality: str | None = None,
) -> VpeRequest:
    """Builds the upscale request for one already-uploaded segment."""
    return VpeRequest(
        capability_id=VpeCapabilityId.UPSCALE,
        storage_uri=storage_uri,
        video=VpeMediaRef(gcs_uri=segment_uri, mime_type=_MP4),
        resolution=resolution.value,
        aspect_ratio=aspect_ratio,
        sharpness=sharpness,
        codec=codec,
        compression_quality=compression_quality,
    )


def require_vpe_configured(cfg: ConfigService) -> None:
    """Refuses a job on a deployment that cannot run one."""
    if not cfg.VPE_ENABLED:
        raise VpeJobError("VPE is not enabled on this deployment.")
    if not cfg.VPE_BUCKET:
        raise VpeJobError(
            "VPE_BUCKET is not set. VPE reads and writes Cloud Storage only,"
            " and its bucket must sit in the allowlisted project.",
        )
    if not cfg.VPE_PROJECT_ID:
        raise VpeJobError(
            "VPE_PROJECT_ID is not set. The publisher endpoint is"
            " allowlisted per calling project, which is not necessarily the"
            " project this app is deployed into.",
        )


def _worker_logger(
    media_item_id: int,
    worker_tag: str = "upscale",
) -> logging.Logger:
    """Builds the per-job logger, matching the other video workers."""
    name = f"vpe_{worker_tag}_worker.{media_item_id}"
    worker_logger = logging.getLogger(name)
    worker_logger.setLevel(logging.INFO)
    if worker_logger.hasHandlers():
        worker_logger.handlers.clear()

    if os.getenv("ENVIRONMENT") == "production":
        handler = CloudLoggingHandler(LoggerClient(), name=name)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                f"%(asctime)s - [VPE_{worker_tag.upper()}_WORKER] -"
                " %(levelname)s - %(message)s",
            ),
        )
    worker_logger.addHandler(handler)
    return worker_logger


def _discard_segments(
    vpe_gcs: GcsService,
    uris: list[str],
    worker_logger: logging.Logger,
) -> None:
    """Deletes intermediate files uploaded for one job on a best-effort basis."""
    for uri in uris:
        try:
            vpe_gcs.delete_blob_from_uri(uri)
        except Exception as error:  # noqa: BLE001
            worker_logger.warning("Could not delete %s: %s", uri, error)


def _is_retryable_vpe_error(err: Exception) -> bool:
    """Returns True if an exception indicates transient capacity/overload."""
    text = str(err).lower()
    return bool(getattr(err, "retryable", False)) or any(
        tok in text
        for tok in (
            "unavailable",
            "overloaded",
            "resource exhausted",
            "high load",
            "capacity",
            "429",
            "503",
        )
    )


async def _poll_operation_until_done(
    client: VpeClient,
    operation: VpeOperation,
    media_repo: MediaRepository,
    media_item_id: int,
    worker_logger: logging.Logger,
    *,
    max_wait: float = SEGMENT_TIMEOUT_SECONDS,
    poll_interval: float = 10.0,
) -> VpeResult:
    """Polls a VPE long-running operation until completion."""
    if operation.done:
        if operation.has_error:
            err_info = operation.raw.get("error") or {}
            raise VpeJobError(
                f"VPE operation failed: {err_info.get('message', err_info)}"
            )
        return parse_result(operation)

    poll_res = operation
    total_wait = 0.0
    transient_failures = 0
    max_transient = 6

    while not poll_res.done and total_wait < max_wait:
        await asyncio.sleep(poll_interval)
        total_wait += poll_interval
        try:
            poll_res = await asyncio.to_thread(client.poll, operation.name)
            transient_failures = 0
        except Exception as poll_err:  # noqa: BLE001
            if _is_retryable_vpe_error(poll_err):
                transient_failures += 1
                worker_logger.warning(
                    "Transient poll error on %s (%d/%d): %s",
                    operation.name,
                    transient_failures,
                    max_transient,
                    poll_err,
                )
                if transient_failures > max_transient:
                    raise
                continue
            raise

        await record_worker_progress(media_repo, media_item_id, worker_logger)

    if not poll_res.done:
        raise VpeJobError(
            f"VPE operation {operation.name} timed out after {total_wait:.1f}s."
        )
    if poll_res.has_error:
        err_info = poll_res.raw.get("error") or {}
        raise VpeJobError(
            f"VPE operation failed: {err_info.get('message', err_info)}"
        )
    return parse_result(poll_res)


async def _submit_and_wait_with_retry(
    client: VpeClient,
    request: VpeRequest,
    media_repo: MediaRepository,
    media_item_id: int,
    worker_logger: logging.Logger,
    *,
    base_raw_data: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> tuple[VpeResult, str]:
    """Submits a VpeRequest and polls with automatic retry on capacity errors."""
    payload = build_payload(request)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            operation = await asyncio.to_thread(client.submit, payload)
            raw_record = {
                **(base_raw_data or {}),
                "vpe_capability": request.capability_id.value,
                "vpe_operation": operation.name,
            }
            await record_worker_progress(
                media_repo,
                media_item_id,
                worker_logger,
                {"raw_data": raw_record},
            )
            result = await _poll_operation_until_done(
                client,
                operation,
                media_repo,
                media_item_id,
                worker_logger,
            )
            return result, operation.name
        except Exception as err:  # noqa: BLE001
            last_err = err
            if _is_retryable_vpe_error(err) and attempt < max_retries - 1:
                backoff = (attempt + 1) * 15
                worker_logger.warning(
                    "VPE job %s failed with retryable error (%s). Retrying in"
                    " %ds (attempt %d/%d)...",
                    media_item_id,
                    err,
                    backoff,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(backoff)
                continue
            raise
    raise VpeJobError(f"VPE job failed: {last_err}")  # pragma: no cover


def _detect_gmp_delivery_format(
    primary_uri: str,
    primary_mime: str,
    requested_codec: str | None,
) -> tuple[str, str, str]:
    """Returns (file_ext, mime_type, codec) for a GMP output video."""
    lower_uri = (primary_uri or "").lower()
    lower_mime = (primary_mime or "").lower()
    req_codec = (requested_codec or "h264").lower()

    if (
        lower_uri.endswith(".mov")
        or lower_mime == MIME_QUICKTIME
        or req_codec == "prores"
    ):
        return "mov", MIME_QUICKTIME, "prores"
    if (
        lower_uri.endswith(".mxf")
        or lower_mime == MIME_MXF
        or req_codec == "dnxhr"
    ):
        return "mxf", MIME_MXF, "dnxhr"
    return "mp4", MIME_MP4, "h264"


async def _deliver_gmp_output(
    *,
    result: VpeResult,
    output_codec: str | None,
    compression_quality: str | None,
    dest_prefix: str,
    work: Path,
    vpe_gcs: GcsService,
    app_gcs: GcsService,
    base_raw_data: dict[str, Any] | None = None,
    audio_restore_source: Path | None = None,
    preview_postprocess_fn: Callable[[Path, Path], Path] | None = None,
) -> dict[str, Any]:
    """Downloads GMP output and handles Dual-Asset ProRes/DNxHR Master delivery.

    Whenever the output is ``.mov`` (``prores``) or ``.mxf`` (``dnxhr``), the
    master file is uploaded to ``app_gcs`` and recorded under
    ``raw_data["master_gcs_uris"]``, while a web-playable H.264 ``.mp4`` is
    transcoded via ``transcode_to_h264_preview`` for ``gcs_uris``.
    """
    if not result.primary_video:
        raise VpeJobError("GMP operation finished without an output video.")

    primary_uri = result.primary_video.gcs_uri
    primary_mime = result.primary_video.mime_type
    ext, master_mime, resolved_codec = _detect_gmp_delivery_format(
        primary_uri,
        primary_mime,
        output_codec,
    )
    resolved_quality = compression_quality or "optimized"

    local_raw = work / f"gmp_output.{ext}"
    fetched = await asyncio.to_thread(
        vpe_gcs.download_from_gcs,
        gcs_uri_path=_blob_path(primary_uri),
        destination_file_path=str(local_raw),
    )
    if not fetched:
        raise VpeJobError(f"Could not download GMP output from {primary_uri}.")

    raw_data: dict[str, Any] = {
        **(base_raw_data or {}),
        "output_codec": resolved_codec,
        "compression_quality": resolved_quality,
    }

    if resolved_codec in ("prores", "dnxhr"):
        master_blob = f"{dest_prefix}_master.{ext}"
        master_gcs_uri = await asyncio.to_thread(
            app_gcs.upload_file_to_gcs,
            local_path=str(local_raw),
            destination_blob_name=master_blob,
            mime_type=master_mime,
        )
        if not master_gcs_uri:
            raise VpeJobError("Could not upload professional master asset.")
        raw_data["master_gcs_uris"] = [master_gcs_uri]

        local_preview = work / "preview_h264.mp4"
        await asyncio.to_thread(
            transcode_to_h264_preview,
            local_raw,
            local_preview,
        )
        candidate_mp4 = local_preview
    else:
        candidate_mp4 = local_raw

    if preview_postprocess_fn is not None:
        post_mp4 = work / "preview_post.mp4"
        candidate_mp4 = await asyncio.to_thread(
            preview_postprocess_fn,
            candidate_mp4,
            post_mp4,
        )

    if audio_restore_source is not None:
        has_out_audio = await asyncio.to_thread(has_audio_stream, candidate_mp4)
        if not has_out_audio:
            restored_mp4 = work / "preview_with_audio.mp4"
            candidate_mp4 = await asyncio.to_thread(
                restore_audio,
                candidate_mp4,
                audio_restore_source,
                restored_mp4,
            )

    preview_blob = f"{dest_prefix}.mp4"
    preview_gcs_uri = await asyncio.to_thread(
        app_gcs.upload_file_to_gcs,
        local_path=str(candidate_mp4),
        destination_blob_name=preview_blob,
        mime_type=_MP4,
    )
    if not preview_gcs_uri:
        raise VpeJobError("Could not upload preview MP4 asset.")

    thumb_path = await asyncio.to_thread(
        generate_thumbnail,
        str(candidate_mp4),
    )
    thumb_gcs_uri = None
    if thumb_path:
        thumb_gcs_uri = await asyncio.to_thread(
            app_gcs.upload_file_to_gcs,
            local_path=thumb_path,
            destination_blob_name=f"{dest_prefix}_thumb.png",
            mime_type="image/png",
        )

    measured_target = (
        local_raw
        if resolved_codec in ("prores", "dnxhr")
        and preview_postprocess_fn is None
        else candidate_mp4
    )
    measured_metadata = await asyncio.to_thread(
        build_measured_metadata,
        str(measured_target),
    )

    return {
        "gcs_uris": [preview_gcs_uri],
        "thumbnail_uris": [thumb_gcs_uri] if thumb_gcs_uri else [],
        "raw_data": raw_data,
        **measured_metadata,
    }


def _guess_image_mime_and_ext(uri: str) -> tuple[str, str]:
    """Guesses wire mime type and file extension for an image URI."""
    lower = uri.lower()
    if lower.endswith(".webp"):
        return MIME_WEBP, "webp"
    if lower.endswith((".jpg", ".jpeg")):
        return MIME_JPEG, "jpg"
    return MIME_PNG, "png"


def _guess_audio_mime_and_ext(uri: str) -> tuple[str, str]:
    """Guesses wire mime type and file extension for an audio URI."""
    lower = uri.lower()
    if lower.endswith(".mp3"):
        return MIME_MP3, "mp3"
    if lower.endswith(".mpeg"):
        return MIME_MPEG, "mpeg"
    return MIME_WAV, "wav"


async def _stage_single_image(
    *,
    uri: str,
    slot_name: str,
    prefix: str,
    work: Path,
    app_gcs: GcsService,
    vpe_gcs: GcsService,
    fallback_project_id: str | None,
) -> VpeMediaRef:
    """Downloads an image URI and stages it into the VPE bucket."""
    mime_type, ext = _guess_image_mime_and_ext(uri)
    local_img = work / f"{slot_name}.{ext}"
    downloaded = await _download_asset_gcs(
        uri,
        local_img,
        app_gcs,
        vpe_gcs,
        fallback_project_id=fallback_project_id,
    )
    if not downloaded:
        raise VpeJobError(f"Could not download image {uri}.")
    staged_uri = await asyncio.to_thread(
        vpe_gcs.upload_file_to_gcs,
        local_path=str(local_img),
        destination_blob_name=f"{prefix}/{slot_name}.{ext}",
        mime_type=mime_type,
    )
    if not staged_uri:
        raise VpeJobError(f"Could not stage image {slot_name} to VPE bucket.")
    return VpeMediaRef(gcs_uri=staged_uri, mime_type=mime_type)


async def _collect_and_stage_reference_images(
    *,
    request_dto: CreateVeoDto,
    prefix: str,
    work: Path,
    media_repo: MediaRepository,
    source_asset_repo: SourceAssetRepository,
    app_gcs: GcsService,
    vpe_gcs: GcsService,
    fallback_project_id: str | None,
    max_items: int,
) -> tuple[VpeMediaRef, ...]:
    """Resolves and stages reference_images and image source_media_items."""
    uris: list[str] = []
    if request_dto.reference_images:
        for ref_img in request_dto.reference_images:
            asset = await source_asset_repo.get_by_id(ref_img.asset_id)
            if asset and asset.gcs_uri:
                uris.append(asset.gcs_uri)
    if request_dto.source_media_items:
        for smi in request_dto.source_media_items:
            if smi.role in {
                AssetRoleEnum.IMAGE_REFERENCE_ASSET,
                AssetRoleEnum.IMAGE_REFERENCE_STYLE,
            }:
                parent_item = await media_repo.get_by_id(smi.media_item_id)
                if (
                    parent_item
                    and parent_item.gcs_uris
                    and 0 <= smi.media_index < len(parent_item.gcs_uris)
                ):
                    uris.append(parent_item.gcs_uris[smi.media_index])

    uris = uris[:max_items]
    staged: list[VpeMediaRef] = []
    for idx, uri in enumerate(uris):
        staged.append(
            await _stage_single_image(
                uri=uri,
                slot_name=f"ref_img_{idx:02d}",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=fallback_project_id,
            )
        )
    return tuple(staged)


async def _resolve_audio_uris(
    request_dto: CreateVeoDto,
    media_repo: MediaRepository,
    source_asset_repo: SourceAssetRepository,
    max_items: int = 3,
) -> list[str]:
    """Resolves audio GCS URIs from reference_audio and source_media_items."""
    uris: list[str] = []
    if request_dto.reference_audio:
        uri = await _resolve_asset_ref_uri(
            request_dto.reference_audio,
            media_repo,
            source_asset_repo,
        )
        if uri:
            uris.append(uri)
    if request_dto.source_media_items:
        for smi in request_dto.source_media_items:
            if smi.role == AssetRoleEnum.AUDIO_REFERENCE:
                parent_item = await media_repo.get_by_id(smi.media_item_id)
                if (
                    parent_item
                    and parent_item.gcs_uris
                    and 0 <= smi.media_index < len(parent_item.gcs_uris)
                ):
                    uris.append(parent_item.gcs_uris[smi.media_index])
    return uris[:max_items]


async def _stage_audio_file(
    *,
    uri: str,
    slot_name: str,
    prefix: str,
    work: Path,
    app_gcs: GcsService,
    vpe_gcs: GcsService,
    fallback_project_id: str | None,
) -> VpeMediaRef:
    """Downloads an audio file and stages it into VPE's bucket."""
    mime_type, ext = _guess_audio_mime_and_ext(uri)
    local_audio = work / f"{slot_name}_raw.{ext}"
    downloaded = await _download_asset_gcs(
        uri,
        local_audio,
        app_gcs,
        vpe_gcs,
        fallback_project_id=fallback_project_id,
    )
    if not downloaded:
        raise VpeJobError(f"Could not download audio {uri}.")
    staged_uri = await asyncio.to_thread(
        vpe_gcs.upload_file_to_gcs,
        local_path=str(local_audio),
        destination_blob_name=f"{prefix}/{slot_name}.{ext}",
        mime_type=mime_type,
    )
    if not staged_uri:
        raise VpeJobError(f"Could not stage audio {slot_name} to VPE bucket.")
    return VpeMediaRef(gcs_uri=staged_uri, mime_type=mime_type)


async def _resolve_video_input_uri(
    request_dto: CreateVeoDto,
    media_repo: MediaRepository,
    source_asset_repo: SourceAssetRepository,
) -> tuple[str | None, bool]:
    """Resolves a driving/edit video URI and whether its parent is a blue mesh."""
    candidates = []
    if request_dto.reference_video:
        candidates.append(request_dto.reference_video)
    if request_dto.reference_videos:
        candidates.extend(request_dto.reference_videos)
    if request_dto.edit_source:
        candidates.append(request_dto.edit_source)
    if request_dto.source_video_asset_id:
        candidates.append(request_dto.source_video_asset_id)

    for ref in candidates:
        uri = await _resolve_asset_ref_uri(ref, media_repo, source_asset_repo)
        if uri:
            is_mesh = bool(request_dto.is_blue_mesh_input)
            if getattr(ref, "type", None) == "media_item":
                parent = await media_repo.get_by_id(ref.id)
                raw = getattr(parent, "raw_data", None)
                if isinstance(raw, dict) and raw.get("is_blue_mesh"):
                    is_mesh = True
            return uri, is_mesh

    if request_dto.source_media_items:
        for smi in request_dto.source_media_items:
            if smi.role in {
                AssetRoleEnum.EDIT_SOURCE,
                AssetRoleEnum.VIDEO_REFERENCE,
                AssetRoleEnum.VIDEO_EXTENSION_SOURCE,
            }:
                parent = await media_repo.get_by_id(smi.media_item_id)
                if (
                    parent
                    and parent.gcs_uris
                    and 0 <= smi.media_index < len(parent.gcs_uris)
                ):
                    raw = getattr(parent, "raw_data", None)
                    is_mesh = bool(request_dto.is_blue_mesh_input) or (
                        isinstance(raw, dict) and bool(raw.get("is_blue_mesh"))
                    )
                    return parent.gcs_uris[smi.media_index], is_mesh

    return None, bool(request_dto.is_blue_mesh_input)


def _process_vpe_upscale_in_background(  # noqa: PLR0915
    media_item_id: int,
    source_gcs_uri: str,
    resolution: VpeUpscaleResolution = VpeUpscaleResolution.UHD_4K,
    sharpness: int | None = None,
    workspace_id: int | None = None,
    *,
    upscale_mode: str | None = None,
    steps: int | None = None,
    seed: int | None = None,
    output_codec: str = "h264",
    compression_quality: str = "optimized",
    parent_raw_data: dict[str, Any] | None = None,
):
    """Background worker that upscales one gallery video via GMP or Veo Upscaler."""
    worker_logger = _worker_logger(media_item_id, "upscale")
    temp_dir = f"temp/vpe_{media_item_id}"
    resolved_mode = upscale_mode or (
        "1080p" if resolution == VpeUpscaleResolution.HD_1080P else "4k"
    )

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _async_worker():
            async with WorkerDatabase() as db_factory:
                async with db_factory() as db:
                    media_repo = MediaRepository(db)
                    cfg = config_service
                    uploaded_segment_uris: list[str] = []

                    try:
                        await record_worker_progress(
                            media_repo,
                            media_item_id,
                            worker_logger,
                        )
                        require_vpe_configured(cfg)

                        effective_ws_id = workspace_id
                        if effective_ws_id is None:
                            try:
                                row = await media_repo.get_by_id(media_item_id)
                                if row and isinstance(
                                    getattr(row, "workspace_id", None), int
                                ):
                                    effective_ws_id = row.workspace_id
                            except Exception:  # noqa: BLE001
                                pass

                        target_project_id, target_bucket_name = (
                            await _resolve_workspace_gcs_config(
                                db, effective_ws_id, worker_logger
                            )
                        )
                        app_gcs = GcsService(
                            bucket_name=target_bucket_name,
                            project_id=target_project_id,
                        )
                        vpe_gcs = GcsService(
                            bucket_name=cfg.VPE_BUCKET,
                            project_id=cfg.VPE_PROJECT_ID,
                        )
                        client = VpeClient(project_id=cfg.VPE_PROJECT_ID)

                        start_time = time.monotonic()
                        work = Path(temp_dir)
                        work.mkdir(parents=True, exist_ok=True)

                        source_path = str(work / "source.mp4")
                        downloaded = await _download_asset_gcs(
                            source_gcs_uri,
                            Path(source_path),
                            app_gcs,
                            vpe_gcs,
                            fallback_project_id=target_project_id,
                        )
                        if not downloaded:
                            raise VpeJobError(
                                f"Could not download {source_gcs_uri}.",
                            )

                        probe = await asyncio.to_thread(
                            probe_media, source_path
                        )
                        prefix = f"vpe/upscale/{media_item_id}"

                        # Branch 1: Seamless Video Texture upscaling (4K only,
                        # ProRes or DNxHR required by API when seamless=True).
                        if is_seamless_raw_data(parent_raw_data):
                            s_raw = (
                                parent_raw_data.get("seamless")
                                if isinstance(parent_raw_data, dict)
                                else {}
                            )
                            seamless_flags = (
                                VpeSeamlessFlags(
                                    loop=bool(s_raw.get("loop")),
                                    tessellate_horizontal=bool(
                                        s_raw.get("tessellate_horizontal")
                                        or s_raw.get("tessellateHorizontal")
                                    ),
                                    tessellate_vertical=bool(
                                        s_raw.get("tessellate_vertical")
                                        or s_raw.get("tessellateVertical")
                                    ),
                                )
                                if isinstance(s_raw, dict)
                                else VpeSeamlessFlags(loop=True)
                            )
                            effective_codec = (
                                output_codec
                                if output_codec in ("prores", "dnxhr")
                                else "prores"
                            )
                            src_mime = probe.mime_type or _MP4
                            staged_src_uri = await asyncio.to_thread(
                                vpe_gcs.upload_file_to_gcs,
                                local_path=source_path,
                                destination_blob_name=f"{prefix}/in_seamless.mp4",
                                mime_type=src_mime,
                            )
                            if not staged_src_uri:
                                raise VpeJobError(
                                    "Could not stage seamless source video."
                                )
                            uploaded_segment_uris.append(staged_src_uri)

                            seamless_dict = {
                                "loop": seamless_flags.loop,
                                "tessellate_horizontal": (
                                    seamless_flags.tessellate_horizontal
                                ),
                                "tessellate_vertical": (
                                    seamless_flags.tessellate_vertical
                                ),
                            }
                            vpe_req = VpeRequest(
                                capability_id=VpeCapabilityId.UPSCALE_SEAMLESS,
                                storage_uri=f"gs://{cfg.VPE_BUCKET}/{prefix}/out",
                                video=VpeMediaRef(
                                    gcs_uri=staged_src_uri,
                                    mime_type=src_mime,
                                ),
                                resolution="4k",
                                aspect_ratio="16:9",
                                codec=effective_codec,
                                compression_quality=compression_quality,
                                sharpness=sharpness,
                                seamless=seamless_flags,
                            )
                            result, _ = await _submit_and_wait_with_retry(
                                client,
                                vpe_req,
                                media_repo,
                                media_item_id,
                                worker_logger,
                                base_raw_data={"seamless": seamless_dict},
                            )
                            delivered = await _deliver_gmp_output(
                                result=result,
                                output_codec=effective_codec,
                                compression_quality=compression_quality,
                                dest_prefix=(
                                    f"upscaled_videos/{media_item_id}_4k"
                                ),
                                work=work,
                                vpe_gcs=vpe_gcs,
                                app_gcs=app_gcs,
                                base_raw_data={
                                    "vpe_capability": (
                                        VpeCapabilityId.UPSCALE_SEAMLESS.value
                                    ),
                                    "seamless": seamless_dict,
                                },
                                audio_restore_source=Path(source_path),
                            )
                            await media_repo.update(
                                media_item_id,
                                {
                                    "status": JobStatusEnum.COMPLETED,
                                    "model": GenerationModelEnum.VEO_3_1_UPSCALE,
                                    "generation_time": (
                                        time.monotonic() - start_time
                                    ),
                                    "num_media": 1,
                                    **delivered,
                                },
                            )
                            await asyncio.to_thread(
                                _discard_segments,
                                vpe_gcs,
                                uploaded_segment_uris,
                                worker_logger,
                            )
                            return

                        # Branch 2: Unsplit GMP Super Resolution (4K / 4K Pro /
                        # 8K) for clips <= 240 frames, preserving native FPS,
                        # Cinema/Consumer aspect ratio, and 48 kHz audio.
                        if (
                            resolved_mode in _SUPER_RES_MODE_MAP
                            and probe.frame_count is not None
                            and probe.frame_count <= 240
                        ):
                            super_res_mode = _SUPER_RES_MODE_MAP[resolved_mode]
                            check_super_res_upscalable(probe, super_res_mode)
                            src_mime = probe.mime_type or _MP4
                            src_ext = {
                                MIME_QUICKTIME: "mov",
                                MIME_MXF: "mxf",
                            }.get(src_mime, "mp4")
                            staged_src_uri = await asyncio.to_thread(
                                vpe_gcs.upload_file_to_gcs,
                                local_path=source_path,
                                destination_blob_name=(
                                    f"{prefix}/in_source.{src_ext}"
                                ),
                                mime_type=src_mime,
                            )
                            if not staged_src_uri:
                                raise VpeJobError(
                                    "Could not upload source clip to VPE bucket."
                                )
                            uploaded_segment_uris.append(staged_src_uri)

                            vpe_req = VpeRequest(
                                capability_id=VpeCapabilityId.SUPER_RES,
                                storage_uri=f"gs://{cfg.VPE_BUCKET}/{prefix}/out",
                                video=VpeMediaRef(
                                    gcs_uri=staged_src_uri,
                                    mime_type=src_mime,
                                ),
                                super_res_mode=super_res_mode,
                                super_res_steps=steps,
                                sharpness=sharpness,
                                seed=seed,
                                codec=output_codec,
                                compression_quality=compression_quality,
                            )
                            result, op_name = await _submit_and_wait_with_retry(
                                client,
                                vpe_req,
                                media_repo,
                                media_item_id,
                                worker_logger,
                                base_raw_data={
                                    "super_res_mode": super_res_mode,
                                },
                            )
                            delivered = await _deliver_gmp_output(
                                result=result,
                                output_codec=output_codec,
                                compression_quality=compression_quality,
                                dest_prefix=(
                                    f"upscaled_videos/{media_item_id}_{resolved_mode}"
                                ),
                                work=work,
                                vpe_gcs=vpe_gcs,
                                app_gcs=app_gcs,
                                base_raw_data={
                                    "vpe_capability": (
                                        VpeCapabilityId.SUPER_RES.value
                                    ),
                                    "vpe_operation_names": [op_name],
                                    "super_res_mode": super_res_mode,
                                },
                                audio_restore_source=Path(source_path),
                            )
                            await media_repo.update(
                                media_item_id,
                                {
                                    "status": JobStatusEnum.COMPLETED,
                                    "model": (
                                        GenerationModelEnum.VEO_EXP_SUPER_RES
                                    ),
                                    "generation_time": (
                                        time.monotonic() - start_time
                                    ),
                                    "num_media": 1,
                                    **delivered,
                                },
                            )
                            await asyncio.to_thread(
                                _discard_segments,
                                vpe_gcs,
                                uploaded_segment_uris,
                                worker_logger,
                            )
                            return

                        # Branch 3: Segmented veo3p1_upscale pipeline (1080p or
                        # clips > 240 frames).
                        if resolved_mode in ("4k_pro", "8k"):
                            raise VpeJobError(
                                f"Upscale mode '{resolved_mode}' supports up"
                                " to 240 frames (10s at 24fps)."
                            )

                        target_res = (
                            VpeUpscaleResolution.HD_1080P
                            if resolved_mode == "1080p"
                            else VpeUpscaleResolution.UHD_4K
                        )
                        check_upscalable(probe)
                        segments = plan_upscale(probe)
                        aspect_ratio = aspect_ratio_for(probe)
                        worker_logger.info(
                            "Upscaling %d frames as %d segment(s) at %s",
                            probe.frame_count,
                            len(segments),
                            aspect_ratio,
                        )

                        pieces = []
                        for segment in segments:
                            pieces.append(
                                await asyncio.to_thread(
                                    cut_segment,
                                    source_path,
                                    work / f"in_{segment.index:02d}.mp4",
                                    first_frame=segment.first_frame,
                                    frames=segment.frames,
                                    fps=int(probe.fps),
                                ),
                            )

                        seg_codec = (
                            output_codec
                            if target_res == VpeUpscaleResolution.UHD_4K
                            and output_codec != "h264"
                            else None
                        )
                        seg_quality = (
                            compression_quality
                            if compression_quality != "optimized"
                            else None
                        )

                        requests = []
                        operations = []
                        for segment, piece in zip(segments, pieces):
                            segment_uri = await asyncio.to_thread(
                                vpe_gcs.upload_file_to_gcs,
                                local_path=str(piece),
                                destination_blob_name=(
                                    f"{prefix}/in_{segment.index:02d}.mp4"
                                ),
                                mime_type=_MP4,
                            )
                            if not segment_uri:
                                raise VpeJobError(
                                    "Could not upload segment"
                                    f" {segment.index} to"
                                    f" gs://{cfg.VPE_BUCKET}.",
                                )
                            uploaded_segment_uris.append(segment_uri)
                            req = build_segment_request(
                                segment_uri=segment_uri,
                                storage_uri=(
                                    f"gs://{cfg.VPE_BUCKET}/{prefix}"
                                    f"/out_{segment.index:02d}"
                                ),
                                aspect_ratio=aspect_ratio,
                                resolution=target_res,
                                sharpness=sharpness,
                                codec=seg_codec,
                                compression_quality=seg_quality,
                            )
                            requests.append(req)
                            operations.append(
                                await asyncio.to_thread(
                                    client.submit,
                                    build_payload(req),
                                ),
                            )

                        seg_raw_data: dict[str, Any] = {
                            "vpe_capability": VpeCapabilityId.UPSCALE.value,
                            "vpe_operation_names": [
                                operation.name for operation in operations
                            ],
                            "vpe_segments": [
                                {
                                    "index": segment.index,
                                    "first_frame": segment.first_frame,
                                    "frames": segment.frames,
                                }
                                for segment in segments
                            ],
                        }
                        await record_worker_progress(
                            media_repo,
                            media_item_id,
                            worker_logger,
                            {"raw_data": seg_raw_data},
                        )

                        out_ext, out_mime, out_resolved_codec = (
                            _detect_gmp_delivery_format(
                                "",
                                "",
                                seg_codec,
                            )
                        )
                        upscaled: list[Path] = []
                        for segment, operation, req in zip(
                            segments, operations, requests
                        ):
                            max_retries = 3
                            current_op = operation
                            for attempt in range(max_retries):
                                try:
                                    result = await asyncio.to_thread(
                                        client.wait_for_completion_sync,
                                        current_op,
                                        timeout_seconds=SEGMENT_TIMEOUT_SECONDS,
                                    )
                                    local = (
                                        work
                                        / f"out_{segment.index:02d}.{out_ext}"
                                    )
                                    fetched = await asyncio.to_thread(
                                        vpe_gcs.download_from_gcs,
                                        gcs_uri_path=_blob_path(
                                            result.primary_video.gcs_uri,
                                        ),
                                        destination_file_path=str(local),
                                    )
                                    if not fetched:
                                        raise VpeJobError(
                                            "Segment"
                                            f" {segment.index} finished but its"
                                            " output could not be downloaded"
                                            " from"
                                            f" {result.primary_video.gcs_uri}.",
                                        )
                                    upscaled.append(local)
                                    break
                                except Exception as err:  # noqa: BLE001
                                    if (
                                        _is_retryable_vpe_error(err)
                                        and attempt < max_retries - 1
                                    ):
                                        backoff = (attempt + 1) * 15
                                        worker_logger.warning(
                                            "Segment %d retry in %ds (%s)",
                                            segment.index,
                                            backoff,
                                            err,
                                        )
                                        await asyncio.sleep(backoff)
                                        current_op = await asyncio.to_thread(
                                            client.submit,
                                            build_payload(req),
                                        )
                                        continue
                                    raise
                            await record_worker_progress(
                                media_repo,
                                media_item_id,
                                worker_logger,
                            )

                        if len(upscaled) == 1:
                            joined = upscaled[0]
                        else:
                            joined = await asyncio.to_thread(
                                concat_segments,
                                list(upscaled),
                                work / f"joined.{out_ext}",
                            )

                        dest_prefix = (
                            f"upscaled_videos/{media_item_id}_"
                            f"{target_res.value}"
                        )
                        if out_resolved_codec in ("prores", "dnxhr"):
                            master_gcs_uri = await asyncio.to_thread(
                                app_gcs.upload_file_to_gcs,
                                local_path=str(joined),
                                destination_blob_name=(
                                    f"{dest_prefix}_master.{out_ext}"
                                ),
                                mime_type=out_mime,
                            )
                            if not master_gcs_uri:
                                raise VpeJobError(
                                    "Could not upload joined professional"
                                    " master."
                                )
                            seg_raw_data["master_gcs_uris"] = [master_gcs_uri]
                            seg_raw_data["output_codec"] = out_resolved_codec
                            seg_raw_data["compression_quality"] = (
                                compression_quality
                            )
                            preview_h264 = work / "preview_h264.mp4"
                            await asyncio.to_thread(
                                transcode_to_h264_preview,
                                joined,
                                preview_h264,
                            )
                            final_path = await asyncio.to_thread(
                                restore_audio,
                                preview_h264,
                                source_path,
                                work / "master.mp4",
                            )
                        else:
                            final_path = await asyncio.to_thread(
                                restore_audio,
                                joined,
                                source_path,
                                work / "master.mp4",
                            )

                        final_gcs_uri = await asyncio.to_thread(
                            app_gcs.upload_file_to_gcs,
                            local_path=str(final_path),
                            destination_blob_name=f"{dest_prefix}.mp4",
                            mime_type=_MP4,
                        )
                        if not final_gcs_uri:
                            raise VpeJobError(
                                "Could not upload the finished master.",
                            )

                        thumbnail_path = await asyncio.to_thread(
                            generate_thumbnail,
                            str(final_path),
                        )
                        thumbnail_gcs_uri = None
                        if thumbnail_path:
                            thumbnail_gcs_uri = await asyncio.to_thread(
                                app_gcs.upload_file_to_gcs,
                                local_path=thumbnail_path,
                                destination_blob_name=(
                                    f"upscaled_videos/{media_item_id}_thumb.png"
                                ),
                                mime_type="image/png",
                            )

                        measured_metadata = await asyncio.to_thread(
                            build_measured_metadata,
                            str(final_path),
                        )

                        await media_repo.update(
                            media_item_id,
                            {
                                "status": JobStatusEnum.COMPLETED,
                                "model": GenerationModelEnum.VEO_3_1_UPSCALE,
                                "gcs_uris": [final_gcs_uri],
                                "thumbnail_uris": (
                                    [thumbnail_gcs_uri]
                                    if thumbnail_gcs_uri
                                    else []
                                ),
                                "raw_data": seg_raw_data,
                                "generation_time": (
                                    time.monotonic() - start_time
                                ),
                                "num_media": 1,
                                **measured_metadata,
                            },
                        )
                        await asyncio.to_thread(
                            _discard_segments,
                            vpe_gcs,
                            uploaded_segment_uris,
                            worker_logger,
                        )
                        worker_logger.info(
                            "Upscaled media item %s to %s",
                            media_item_id,
                            target_res.value,
                        )

                    except Exception as error:  # noqa: BLE001
                        worker_logger.error(
                            "VPE upscale failed: %s",
                            error,
                            exc_info=True,
                        )
                        try:
                            await db.rollback()
                            await media_repo.update(
                                media_item_id,
                                {
                                    "status": JobStatusEnum.FAILED,
                                    "error_message": str(error),
                                },
                            )
                        except Exception as update_error:  # noqa: BLE001
                            worker_logger.error(
                                "Could not record the failure on media item"
                                " %s: %s",
                                media_item_id,
                                update_error,
                                exc_info=True,
                            )
                    finally:
                        if os.path.exists(temp_dir):
                            shutil.rmtree(temp_dir)

        loop.run_until_complete(_async_worker())
        loop.close()

    except Exception as error:  # noqa: BLE001
        worker_logger.error(
            "VPE upscale worker failed to initialize: %s",
            error,
            exc_info=True,
        )


def _run_gmp_generation_worker(
    media_item_id: int,
    worker_tag: str,
    coro_fn: Callable[
        [
            MediaRepository,
            SourceAssetRepository,
            GcsService,
            GcsService,
            VpeClient,
            Path,
            str | None,
            logging.Logger,
        ],
        Any,
    ],
    workspace_id: int | None,
) -> None:
    """Shared lifecycle runner for GMP background generation workers."""
    worker_logger = _worker_logger(media_item_id, worker_tag)
    temp_dir = tempfile.mkdtemp(prefix=f"vpe_{worker_tag}_{media_item_id}_")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _async_worker():
            async with WorkerDatabase() as db_factory:
                async with db_factory() as db:
                    media_repo = MediaRepository(db)
                    source_asset_repo = SourceAssetRepository(db)
                    cfg = config_service

                    try:
                        await record_worker_progress(
                            media_repo,
                            media_item_id,
                            worker_logger,
                        )
                        require_vpe_configured(cfg)

                        target_project_id, target_bucket_name = (
                            await _resolve_workspace_gcs_config(
                                db, workspace_id, worker_logger
                            )
                        )
                        app_gcs = GcsService(
                            bucket_name=target_bucket_name,
                            project_id=target_project_id,
                        )
                        vpe_gcs = GcsService(
                            bucket_name=cfg.VPE_BUCKET,
                            project_id=cfg.VPE_PROJECT_ID,
                        )
                        client = VpeClient(project_id=cfg.VPE_PROJECT_ID)
                        start_time = time.monotonic()
                        work = Path(temp_dir)
                        work.mkdir(parents=True, exist_ok=True)

                        delivered = await coro_fn(
                            media_repo,
                            source_asset_repo,
                            app_gcs,
                            vpe_gcs,
                            client,
                            work,
                            target_project_id,
                            worker_logger,
                        )
                        await media_repo.update(
                            media_item_id,
                            {
                                "status": JobStatusEnum.COMPLETED,
                                "generation_time": (
                                    time.monotonic() - start_time
                                ),
                                "num_media": 1,
                                **delivered,
                            },
                        )
                        worker_logger.info(
                            "VPE %s completed for media item %s",
                            worker_tag,
                            media_item_id,
                        )
                    except Exception as error:  # noqa: BLE001
                        worker_logger.error(
                            "VPE %s failed: %s",
                            worker_tag,
                            error,
                            exc_info=True,
                        )
                        try:
                            await db.rollback()
                            await media_repo.update(
                                media_item_id,
                                {
                                    "status": JobStatusEnum.FAILED,
                                    "error_message": str(error),
                                },
                            )
                        except Exception as update_error:  # noqa: BLE001
                            worker_logger.error(
                                "Could not record failure on media item %s: %s",
                                media_item_id,
                                update_error,
                                exc_info=True,
                            )
                    finally:
                        if os.path.exists(temp_dir):
                            shutil.rmtree(temp_dir)

        loop.run_until_complete(_async_worker())
        loop.close()

    except Exception as error:  # noqa: BLE001
        worker_logger.error(
            "VPE %s worker failed to initialize: %s",
            worker_tag,
            error,
            exc_info=True,
        )


def _process_vpe_dialogue_in_background(
    media_item_id: int,
    request_dto: CreateVeoDto,
    user_email: str,
) -> None:
    """Background worker that generates a dialogue-driven lip-sync video."""
    _ = user_email

    async def _job(
        media_repo: MediaRepository,
        source_asset_repo: SourceAssetRepository,
        app_gcs: GcsService,
        vpe_gcs: GcsService,
        client: VpeClient,
        work: Path,
        target_project_id: str | None,
        worker_logger: logging.Logger,
    ) -> dict[str, Any]:
        image_gcs_uri = None
        if request_dto.start_image_asset_id:
            image_gcs_uri = await _resolve_asset_ref_uri(
                request_dto.start_image_asset_id,
                media_repo,
                source_asset_repo,
            )
        elif request_dto.reference_images:
            asset = await source_asset_repo.get_by_id(
                request_dto.reference_images[0].asset_id
            )
            if asset:
                image_gcs_uri = asset.gcs_uri
        elif request_dto.source_media_items:
            for smi in request_dto.source_media_items:
                if smi.role in {
                    AssetRoleEnum.START_FRAME,
                    AssetRoleEnum.IMAGE_REFERENCE_ASSET,
                }:
                    parent = await media_repo.get_by_id(smi.media_item_id)
                    if parent and parent.gcs_uris:
                        image_gcs_uri = parent.gcs_uris[smi.media_index]
                        break

        if not image_gcs_uri:
            raise VpeJobError(
                "No input image could be resolved for dialogue generation."
            )

        local_raw_image = work / "input_image_raw.png"
        if not await _download_asset_gcs(
            image_gcs_uri,
            local_raw_image,
            app_gcs,
            vpe_gcs,
            fallback_project_id=target_project_id,
        ):
            raise VpeJobError(
                f"Could not download input image {image_gcs_uri}."
            )

        audio_gcs_uri = await _resolve_asset_ref_uri(
            request_dto.reference_audio,
            media_repo,
            source_asset_repo,
        )
        if not audio_gcs_uri:
            raise VpeJobError(
                "No audio track could be resolved for dialogue generation."
            )

        local_raw_audio = work / "input_audio_raw"
        if not await _download_asset_gcs(
            audio_gcs_uri,
            local_raw_audio,
            app_gcs,
            vpe_gcs,
            fallback_project_id=target_project_id,
        ):
            raise VpeJobError(
                f"Could not download reference audio {audio_gcs_uri}."
            )

        is_portrait = request_dto.aspect_ratio == AspectRatioEnum.RATIO_9_16
        local_norm_audio = work / "audio_8s.wav"
        local_norm_frame = work / "frame_1280x720.png"
        await asyncio.to_thread(
            prepare_dialogue_audio,
            local_raw_audio,
            local_norm_audio,
        )
        await asyncio.to_thread(
            prepare_dialogue_frame,
            local_raw_image,
            local_norm_frame,
            is_portrait=is_portrait,
        )

        prefix = f"vpe/dialogue/{media_item_id}"
        staged_frame_uri = await asyncio.to_thread(
            vpe_gcs.upload_file_to_gcs,
            local_path=str(local_norm_frame),
            destination_blob_name=f"{prefix}/frame.png",
            mime_type=MIME_PNG,
        )
        staged_audio_uri = await asyncio.to_thread(
            vpe_gcs.upload_file_to_gcs,
            local_path=str(local_norm_audio),
            destination_blob_name=f"{prefix}/audio_8s.wav",
            mime_type=MIME_WAV,
        )

        vpe_req = VpeRequest(
            capability_id=VpeCapabilityId.DIALOGUE_DRIVEN,
            storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
            prompt=request_dto.prompt,
            image=VpeMediaRef(gcs_uri=staged_frame_uri, mime_type=MIME_PNG),
            reference_audios=(
                VpeMediaRef(gcs_uri=staged_audio_uri, mime_type=MIME_WAV),
            ),
            seed=request_dto.seed,
            codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
        )
        result, op_name = await _submit_and_wait_with_retry(
            client,
            vpe_req,
            media_repo,
            media_item_id,
            worker_logger,
        )
        return await _deliver_gmp_output(
            result=result,
            output_codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            dest_prefix=f"generated_videos/dialogue_{media_item_id}",
            work=work,
            vpe_gcs=vpe_gcs,
            app_gcs=app_gcs,
            base_raw_data={
                "vpe_capability": VpeCapabilityId.DIALOGUE_DRIVEN.value,
                "operation": op_name,
            },
            preview_postprocess_fn=lambda src, dst: postprocess_dialogue_video(
                src, dst, is_portrait=is_portrait
            ),
        )

    _run_gmp_generation_worker(
        media_item_id,
        "dialogue",
        _job,
        request_dto.workspace_id,
    )


def _process_vpe_transform_in_background(
    media_item_id: int,
    request_dto: CreateVeoDto,
    user_email: str,
) -> None:
    """Background worker for veo-exp-video-transform requests."""
    _ = user_email

    async def _job(
        media_repo: MediaRepository,
        source_asset_repo: SourceAssetRepository,
        app_gcs: GcsService,
        vpe_gcs: GcsService,
        client: VpeClient,
        work: Path,
        target_project_id: str | None,
        worker_logger: logging.Logger,
    ) -> dict[str, Any]:
        is_keyframe_i2v = bool(
            request_dto.start_image_asset_id
            and not request_dto.source_video_asset_id
            and not request_dto.edit_source
            and not (
                request_dto.source_media_items
                and any(
                    item.role == AssetRoleEnum.VIDEO_EXTENSION_SOURCE
                    for item in request_dto.source_media_items
                )
            )
        )
        prefix = f"vpe/transform/{media_item_id}"
        staged_mask_uri = None
        local_norm_mask = work / "mask_norm.mp4"
        local_norm_video = work / "norm_video_1280x720.mp4"
        is_portrait = request_dto.aspect_ratio == AspectRatioEnum.RATIO_9_16

        if is_keyframe_i2v:
            first_uri = await _resolve_asset_ref_uri(
                request_dto.start_image_asset_id,
                media_repo,
                source_asset_repo,
            )
            if not first_uri:
                raise VpeJobError(
                    "No first frame image could be resolved for keyframe video"
                    " transform."
                )
            image_ref = await _stage_single_image(
                uri=first_uri,
                slot_name="first_frame",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            last_frame_ref = None
            if request_dto.end_image_asset_id:
                last_uri = await _resolve_asset_ref_uri(
                    request_dto.end_image_asset_id,
                    media_repo,
                    source_asset_repo,
                )
                if last_uri:
                    last_frame_ref = await _stage_single_image(
                        uri=last_uri,
                        slot_name="last_frame",
                        prefix=prefix,
                        work=work,
                        app_gcs=app_gcs,
                        vpe_gcs=vpe_gcs,
                        fallback_project_id=target_project_id,
                    )

            conditioning_frames_list: list[VpeConditioningFrame] = []
            if request_dto.conditioning_frames:
                for idx, cf in enumerate(request_dto.conditioning_frames):
                    cf_uri = await _resolve_asset_ref_uri(
                        cf.image_asset_id,
                        media_repo,
                        source_asset_repo,
                    )
                    if cf_uri:
                        cf_ref = await _stage_single_image(
                            uri=cf_uri,
                            slot_name=f"cf_{cf.frame_number}_{idx}",
                            prefix=prefix,
                            work=work,
                            app_gcs=app_gcs,
                            vpe_gcs=vpe_gcs,
                            fallback_project_id=target_project_id,
                        )
                        conditioning_frames_list.append(
                            VpeConditioningFrame(
                                frame_num=cf.frame_number,
                                image=cf_ref,
                            )
                        )

            vpe_req = VpeRequest(
                capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
                storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
                prompt=request_dto.prompt,
                image=image_ref,
                last_frame=last_frame_ref,
                conditioning_frames=tuple(conditioning_frames_list),
                video_transform_strength=None,
                num_diffusion_steps=request_dto.num_diffusion_steps or 20,
                seed=request_dto.seed,
                codec=request_dto.output_codec,
                compression_quality=request_dto.compression_quality,
            )
        else:
            video_gcs_uri, _ = await _resolve_video_input_uri(
                request_dto,
                media_repo,
                source_asset_repo,
            )
            if not video_gcs_uri:
                raise VpeJobError(
                    "No input video could be resolved for video transform."
                )
            local_raw_video = work / "input_video_raw.mp4"
            if not await _download_asset_gcs(
                video_gcs_uri,
                local_raw_video,
                app_gcs,
                vpe_gcs,
                fallback_project_id=target_project_id,
            ):
                raise VpeJobError(
                    f"Could not download input video {video_gcs_uri}."
                )

            try:
                raw_probe = await asyncio.to_thread(
                    probe_media, str(local_raw_video)
                )
                if raw_probe and raw_probe.width and raw_probe.height:
                    is_portrait = bool(raw_probe.is_portrait)
            except Exception as probe_err:  # noqa: BLE001
                worker_logger.warning(
                    "Could not probe transform input orientation: %s",
                    probe_err,
                )

            if request_dto.video_transform_mask_asset_id:
                mask_gcs_uri = await _resolve_asset_ref_uri(
                    request_dto.video_transform_mask_asset_id,
                    media_repo,
                    source_asset_repo,
                )
                if mask_gcs_uri:
                    local_raw_mask = work / "mask_raw.mp4"
                    if not await _download_asset_gcs(
                        mask_gcs_uri,
                        local_raw_mask,
                        app_gcs,
                        vpe_gcs,
                        fallback_project_id=target_project_id,
                    ):
                        raise VpeJobError(
                            f"Could not download mask video {mask_gcs_uri}."
                        )
                    await asyncio.to_thread(
                        prepare_transform_video,
                        local_raw_mask,
                        local_norm_mask,
                        is_portrait,
                    )
                    staged_mask_uri = await asyncio.to_thread(
                        vpe_gcs.upload_file_to_gcs,
                        local_path=str(local_norm_mask),
                        destination_blob_name=f"{prefix}/mask.mp4",
                        mime_type=_MP4,
                    )

            await asyncio.to_thread(
                prepare_transform_video,
                local_raw_video,
                local_norm_video,
                is_portrait,
            )
            staged_video_uri = await asyncio.to_thread(
                vpe_gcs.upload_file_to_gcs,
                local_path=str(local_norm_video),
                destination_blob_name=f"{prefix}/input.mp4",
                mime_type=_MP4,
            )
            vpe_req = VpeRequest(
                capability_id=VpeCapabilityId.VIDEO_TRANSFORM,
                storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
                prompt=request_dto.prompt,
                video=VpeMediaRef(gcs_uri=staged_video_uri, mime_type=_MP4),
                video_transform_strength=(
                    request_dto.video_transform_strength
                    if request_dto.video_transform_strength is not None
                    else 0.5
                ),
                num_diffusion_steps=request_dto.num_diffusion_steps or 20,
                video_transform_mask_gcs_uri=staged_mask_uri,
                seed=request_dto.seed,
                codec=request_dto.output_codec,
                compression_quality=request_dto.compression_quality,
            )

        result, op_name = await _submit_and_wait_with_retry(
            client,
            vpe_req,
            media_repo,
            media_item_id,
            worker_logger,
        )

        def _postprocess(src: Path, dst: Path) -> Path:
            to_crop = src
            if (
                staged_mask_uri
                and local_norm_mask.exists()
                and local_norm_video.exists()
            ):
                composited = work / "composited_masked.mp4"
                try:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(local_norm_video),
                            "-i",
                            str(src),
                            "-i",
                            str(local_norm_mask),
                            "-filter_complex",
                            "[0:v][1:v][2:v]maskedmerge[outv]",
                            "-map",
                            "[outv]",
                            "-map",
                            "0:a?",
                            "-c:v",
                            "libx264",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "copy",
                            str(composited),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    to_crop = composited
                except Exception as comp_err:  # noqa: BLE001
                    worker_logger.warning(
                        "Masked compositing fallback: %s", comp_err
                    )
            return postprocess_transform_video(to_crop, dst, is_portrait)

        return await _deliver_gmp_output(
            result=result,
            output_codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            dest_prefix=f"generated_videos/transform_{media_item_id}",
            work=work,
            vpe_gcs=vpe_gcs,
            app_gcs=app_gcs,
            base_raw_data={
                "vpe_capability": VpeCapabilityId.VIDEO_TRANSFORM.value,
                "vpe_operation": op_name,
            },
            preview_postprocess_fn=_postprocess,
        )

    _run_gmp_generation_worker(
        media_item_id,
        "transform",
        _job,
        request_dto.workspace_id,
    )


def _process_vpe_perf_estimation_in_background(
    media_item_id: int,
    request_dto: CreateVeoDto,
    user_email: str,
) -> None:
    """Background worker for veo-exp-perf-estimation (blue mesh extraction)."""
    _ = user_email

    async def _job(
        media_repo: MediaRepository,
        source_asset_repo: SourceAssetRepository,
        app_gcs: GcsService,
        vpe_gcs: GcsService,
        client: VpeClient,
        work: Path,
        target_project_id: str | None,
        worker_logger: logging.Logger,
    ) -> dict[str, Any]:
        video_uri, _ = await _resolve_video_input_uri(
            request_dto,
            media_repo,
            source_asset_repo,
        )
        if not video_uri:
            raise VpeJobError(
                "No input video could be resolved for performance estimation."
            )

        local_raw = work / "actor_raw.mp4"
        if not await _download_asset_gcs(
            video_uri,
            local_raw,
            app_gcs,
            vpe_gcs,
            fallback_project_id=target_project_id,
        ):
            raise VpeJobError(f"Could not download actor video {video_uri}.")

        max_frames = min(240, int((request_dto.duration_seconds or 10) * 24))
        local_norm = work / "actor_720p_24fps.mp4"
        await asyncio.to_thread(
            normalize_video_720p_24fps,
            local_raw,
            local_norm,
            max_frames,
        )

        prefix = f"vpe/perf_estimation/{media_item_id}"
        staged_uri = await asyncio.to_thread(
            vpe_gcs.upload_file_to_gcs,
            local_path=str(local_norm),
            destination_blob_name=f"{prefix}/actor_720p.mp4",
            mime_type=_MP4,
        )
        if not staged_uri:
            raise VpeJobError(
                "Could not stage normalized actor video to VPE bucket."
            )

        vpe_req = VpeRequest(
            capability_id=VpeCapabilityId.PERF_ESTIMATION,
            storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
            video=VpeMediaRef(gcs_uri=staged_uri, mime_type=_MP4),
            seed=request_dto.seed,
        )
        result, op_name = await _submit_and_wait_with_retry(
            client,
            vpe_req,
            media_repo,
            media_item_id,
            worker_logger,
            base_raw_data={"is_blue_mesh": True},
        )
        return await _deliver_gmp_output(
            result=result,
            output_codec="h264",
            compression_quality="optimized",
            dest_prefix=f"generated_videos/perf_mesh_{media_item_id}",
            work=work,
            vpe_gcs=vpe_gcs,
            app_gcs=app_gcs,
            base_raw_data={
                "vpe_capability": VpeCapabilityId.PERF_ESTIMATION.value,
                "vpe_operation": op_name,
                "is_blue_mesh": True,
            },
        )

    _run_gmp_generation_worker(
        media_item_id,
        "perf_estimation",
        _job,
        request_dto.workspace_id,
    )


def _process_vpe_omni_perf_in_background(
    media_item_id: int,
    request_dto: CreateVeoDto,
    user_email: str,
) -> None:
    """Background worker for omni-perf (1-step or 2-step via perf-estimation)."""
    _ = user_email

    async def _job(
        media_repo: MediaRepository,
        source_asset_repo: SourceAssetRepository,
        app_gcs: GcsService,
        vpe_gcs: GcsService,
        client: VpeClient,
        work: Path,
        target_project_id: str | None,
        worker_logger: logging.Logger,
    ) -> dict[str, Any]:
        driving_uri, is_blue_mesh = await _resolve_video_input_uri(
            request_dto,
            media_repo,
            source_asset_repo,
        )
        if not driving_uri:
            raise VpeJobError(
                "No driving performance video could be resolved for Omni-Perf."
            )

        local_raw_driving = work / "driving_raw.mp4"
        if not await _download_asset_gcs(
            driving_uri,
            local_raw_driving,
            app_gcs,
            vpe_gcs,
            fallback_project_id=target_project_id,
        ):
            raise VpeJobError(
                f"Could not download driving video {driving_uri}."
            )

        max_frames = min(240, int((request_dto.duration_seconds or 10) * 24))
        local_norm_driving = work / "driving_720p_24fps.mp4"
        await asyncio.to_thread(
            normalize_video_720p_24fps,
            local_raw_driving,
            local_norm_driving,
            max_frames,
        )

        prefix = f"vpe/omni_perf/{media_item_id}"
        staged_driving_uri = await asyncio.to_thread(
            vpe_gcs.upload_file_to_gcs,
            local_path=str(local_norm_driving),
            destination_blob_name=f"{prefix}/driving_720p.mp4",
            mime_type=_MP4,
        )
        if not staged_driving_uri:
            raise VpeJobError("Could not stage driving video to VPE bucket.")

        step1_op_name: str | None = None
        if not is_blue_mesh:
            mesh_req = VpeRequest(
                capability_id=VpeCapabilityId.PERF_ESTIMATION,
                storage_uri=(
                    f"gs://{config_service.VPE_BUCKET}/{prefix}/mesh_out"
                ),
                video=VpeMediaRef(gcs_uri=staged_driving_uri, mime_type=_MP4),
                seed=request_dto.seed,
            )
            mesh_res, step1_op_name = await _submit_and_wait_with_retry(
                client,
                mesh_req,
                media_repo,
                media_item_id,
                worker_logger,
                base_raw_data={"stage": "perf_estimation"},
            )
            mesh_uri = mesh_res.primary_video.gcs_uri
            mesh_mime = mesh_res.primary_video.mime_type or _MP4
        else:
            mesh_uri = staged_driving_uri
            mesh_mime = _MP4

        staged_refs = await _collect_and_stage_reference_images(
            request_dto=request_dto,
            prefix=prefix,
            work=work,
            media_repo=media_repo,
            source_asset_repo=source_asset_repo,
            app_gcs=app_gcs,
            vpe_gcs=vpe_gcs,
            fallback_project_id=target_project_id,
            max_items=5,
        )
        if not staged_refs:
            raise VpeJobError(
                "Omni-Perf requires at least one character reference image."
            )

        audio_control: VpeAudioControl | None = None
        if request_dto.audio_control_mode == "target_audio":
            audio_uris = await _resolve_audio_uris(
                request_dto, media_repo, source_asset_repo, max_items=1
            )
            if not audio_uris:
                raise VpeJobError(
                    "audio_control_mode 'target_audio' requires a reference"
                    " audio track."
                )
            audio_ref = await _stage_audio_file(
                uri=audio_uris[0],
                slot_name="target_audio",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            audio_control = VpeAudioControl(target_audio=audio_ref)
        elif request_dto.audio_control_mode == "preserve_video_audio":
            if not is_blue_mesh:
                extracted_wav = await asyncio.to_thread(
                    extract_audio_wav,
                    local_raw_driving,
                    work / "actor_extracted.wav",
                )
                if extracted_wav is not None:
                    staged_wav_uri = await asyncio.to_thread(
                        vpe_gcs.upload_file_to_gcs,
                        local_path=str(extracted_wav),
                        destination_blob_name=f"{prefix}/actor_audio.wav",
                        mime_type=MIME_WAV,
                    )
                    if staged_wav_uri:
                        audio_control = VpeAudioControl(
                            target_audio=VpeMediaRef(
                                gcs_uri=staged_wav_uri,
                                mime_type=MIME_WAV,
                            )
                        )
            else:
                audio_control = VpeAudioControl(
                    use_target_audio_from_video=True
                )

        vpe_req = VpeRequest(
            capability_id=VpeCapabilityId.OMNI_PERF,
            storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
            prompt=request_dto.prompt,
            video=VpeMediaRef(gcs_uri=mesh_uri, mime_type=mesh_mime),
            reference_images=staged_refs,
            codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            audio_control=audio_control,
        )
        base_raw: dict[str, Any] = {"blue_mesh_gcs_uri": mesh_uri}
        if step1_op_name:
            base_raw["perf_estimation_operation"] = step1_op_name

        result, op_name = await _submit_and_wait_with_retry(
            client,
            vpe_req,
            media_repo,
            media_item_id,
            worker_logger,
            base_raw_data=base_raw,
        )
        return await _deliver_gmp_output(
            result=result,
            output_codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            dest_prefix=f"generated_videos/omni_perf_{media_item_id}",
            work=work,
            vpe_gcs=vpe_gcs,
            app_gcs=app_gcs,
            base_raw_data={
                **base_raw,
                "vpe_capability": VpeCapabilityId.OMNI_PERF.value,
                "vpe_operation": op_name,
            },
        )

    _run_gmp_generation_worker(
        media_item_id,
        "omni_perf",
        _job,
        request_dto.workspace_id,
    )


def _process_vpe_omni_cine_in_background(
    media_item_id: int,
    request_dto: CreateVeoDto,
    user_email: str,
) -> None:
    """Background worker for omni-cine (T2V, I2V, R2V, videoTransform, audio)."""
    _ = user_email

    async def _job(
        media_repo: MediaRepository,
        source_asset_repo: SourceAssetRepository,
        app_gcs: GcsService,
        vpe_gcs: GcsService,
        client: VpeClient,
        work: Path,
        target_project_id: str | None,
        worker_logger: logging.Logger,
    ) -> dict[str, Any]:
        prefix = f"vpe/omni_cine/{media_item_id}"
        max_frames = min(240, int((request_dto.duration_seconds or 10) * 24))

        # 1. Optional start & end frames
        start_uri = await _resolve_asset_ref_uri(
            request_dto.start_image_asset_id,
            media_repo,
            source_asset_repo,
        )
        end_uri = await _resolve_asset_ref_uri(
            request_dto.end_image_asset_id,
            media_repo,
            source_asset_repo,
        )
        if not start_uri and request_dto.source_media_items:
            for smi in request_dto.source_media_items:
                if smi.role == AssetRoleEnum.START_FRAME:
                    parent = await media_repo.get_by_id(smi.media_item_id)
                    if parent and parent.gcs_uris:
                        start_uri = parent.gcs_uris[smi.media_index]
                        break
        if not end_uri and request_dto.source_media_items:
            for smi in request_dto.source_media_items:
                if smi.role == AssetRoleEnum.END_FRAME:
                    parent = await media_repo.get_by_id(smi.media_item_id)
                    if parent and parent.gcs_uris:
                        end_uri = parent.gcs_uris[smi.media_index]
                        break

        image_ref = (
            await _stage_single_image(
                uri=start_uri,
                slot_name="first_frame",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            if start_uri
            else None
        )
        last_frame_ref = (
            await _stage_single_image(
                uri=end_uri,
                slot_name="last_frame",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            if end_uri
            else None
        )

        # 2. Optional reference images (up to 7)
        staged_refs = await _collect_and_stage_reference_images(
            request_dto=request_dto,
            prefix=prefix,
            work=work,
            media_repo=media_repo,
            source_asset_repo=source_asset_repo,
            app_gcs=app_gcs,
            vpe_gcs=vpe_gcs,
            fallback_project_id=target_project_id,
            max_items=7,
        )

        # 3. Optional videoTransform (edit_source) or input video
        omni_video_transform: VpeOmniVideoTransform | None = None
        video_ref: VpeMediaRef | None = None
        local_raw_source_video: Path | None = None

        edit_uri = await _resolve_asset_ref_uri(
            request_dto.edit_source,
            media_repo,
            source_asset_repo,
        )
        if not edit_uri and request_dto.source_media_items:
            for smi in request_dto.source_media_items:
                if smi.role == AssetRoleEnum.EDIT_SOURCE:
                    parent = await media_repo.get_by_id(smi.media_item_id)
                    if parent and parent.gcs_uris:
                        edit_uri = parent.gcs_uris[smi.media_index]
                        break

        if edit_uri:
            local_raw_source_video = work / "edit_raw.mp4"
            if not await _download_asset_gcs(
                edit_uri,
                local_raw_source_video,
                app_gcs,
                vpe_gcs,
                fallback_project_id=target_project_id,
            ):
                raise VpeJobError(
                    f"Could not download edit_source video {edit_uri}."
                )
            local_norm_edit = work / "edit_720p_24fps.mp4"
            await asyncio.to_thread(
                normalize_video_720p_24fps,
                local_raw_source_video,
                local_norm_edit,
                max_frames,
            )
            staged_edit_uri = await asyncio.to_thread(
                vpe_gcs.upload_file_to_gcs,
                local_path=str(local_norm_edit),
                destination_blob_name=f"{prefix}/init_video.mp4",
                mime_type=_MP4,
            )
            if not staged_edit_uri:
                raise VpeJobError(
                    "Could not stage initializationVideo to VPE bucket."
                )

            mask_ref: VpeMediaRef | None = None
            if request_dto.video_transform_mask_asset_id:
                mask_uri = await _resolve_asset_ref_uri(
                    request_dto.video_transform_mask_asset_id,
                    media_repo,
                    source_asset_repo,
                )
                if mask_uri:
                    local_raw_mask = work / "mask_raw.mp4"
                    if not await _download_asset_gcs(
                        mask_uri,
                        local_raw_mask,
                        app_gcs,
                        vpe_gcs,
                        fallback_project_id=target_project_id,
                    ):
                        raise VpeJobError(
                            f"Could not download mask video {mask_uri}."
                        )
                    local_norm_mask = work / "mask_720p_24fps.mp4"
                    await asyncio.to_thread(
                        normalize_video_720p_24fps,
                        local_raw_mask,
                        local_norm_mask,
                        max_frames,
                    )
                    staged_mask_uri = await asyncio.to_thread(
                        vpe_gcs.upload_file_to_gcs,
                        local_path=str(local_norm_mask),
                        destination_blob_name=f"{prefix}/mask_video.mp4",
                        mime_type=_MP4,
                    )
                    if staged_mask_uri:
                        mask_ref = VpeMediaRef(
                            gcs_uri=staged_mask_uri,
                            mime_type=_MP4,
                        )

            noise_strength = (
                request_dto.video_transform_strength
                if request_dto.video_transform_strength is not None
                else 0.95
            )
            omni_video_transform = VpeOmniVideoTransform(
                initialization_video=VpeMediaRef(
                    gcs_uri=staged_edit_uri,
                    mime_type=_MP4,
                ),
                noise_strength=noise_strength,
                mask=mask_ref,
            )
        else:
            other_video_uri, _ = await _resolve_video_input_uri(
                request_dto,
                media_repo,
                source_asset_repo,
            )
            if other_video_uri:
                local_raw_source_video = work / "input_raw.mp4"
                if not await _download_asset_gcs(
                    other_video_uri,
                    local_raw_source_video,
                    app_gcs,
                    vpe_gcs,
                    fallback_project_id=target_project_id,
                ):
                    raise VpeJobError(
                        f"Could not download input video {other_video_uri}."
                    )
                local_norm_in = work / "input_720p_24fps.mp4"
                await asyncio.to_thread(
                    normalize_video_720p_24fps,
                    local_raw_source_video,
                    local_norm_in,
                    max_frames,
                )
                staged_in_uri = await asyncio.to_thread(
                    vpe_gcs.upload_file_to_gcs,
                    local_path=str(local_norm_in),
                    destination_blob_name=f"{prefix}/input_video.mp4",
                    mime_type=_MP4,
                )
                if staged_in_uri:
                    video_ref = VpeMediaRef(
                        gcs_uri=staged_in_uri,
                        mime_type=_MP4,
                    )

        # 4. Audio steering (referenceAudios vs audioControl)
        reference_audios: tuple[VpeMediaRef, ...] = ()
        audio_control: VpeAudioControl | None = None
        audio_uris = await _resolve_audio_uris(
            request_dto,
            media_repo,
            source_asset_repo,
            max_items=3,
        )

        if request_dto.audio_control_mode == "target_audio":
            if not audio_uris:
                raise VpeJobError(
                    "Omni-Cine audio_control_mode 'target_audio' requires"
                    " reference_audio."
                )
            staged_target = await _stage_audio_file(
                uri=audio_uris[0],
                slot_name="target_audio",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            audio_control = VpeAudioControl(target_audio=staged_target)
        elif request_dto.audio_control_mode == "preserve_video_audio":
            if video_ref is not None:
                audio_control = VpeAudioControl(
                    use_target_audio_from_video=True
                )
            elif local_raw_source_video is not None:
                extracted_wav = await asyncio.to_thread(
                    extract_audio_wav,
                    local_raw_source_video,
                    work / "source_audio.wav",
                )
                if extracted_wav is not None:
                    staged_wav_uri = await asyncio.to_thread(
                        vpe_gcs.upload_file_to_gcs,
                        local_path=str(extracted_wav),
                        destination_blob_name=f"{prefix}/source_audio.wav",
                        mime_type=MIME_WAV,
                    )
                    if staged_wav_uri:
                        audio_control = VpeAudioControl(
                            target_audio=VpeMediaRef(
                                gcs_uri=staged_wav_uri,
                                mime_type=MIME_WAV,
                            )
                        )
        elif request_dto.audio_control_mode == "reference_audio" or (
            audio_uris and request_dto.audio_control_mode is None
        ):
            staged_audio_list: list[VpeMediaRef] = []
            for idx, a_uri in enumerate(audio_uris[:3]):
                staged_audio_list.append(
                    await _stage_audio_file(
                        uri=a_uri,
                        slot_name=f"ref_audio_{idx:02d}",
                        prefix=prefix,
                        work=work,
                        app_gcs=app_gcs,
                        vpe_gcs=vpe_gcs,
                        fallback_project_id=target_project_id,
                    )
                )
            reference_audios = tuple(staged_audio_list)

        vpe_req = VpeRequest(
            capability_id=VpeCapabilityId.OMNI_CINE,
            storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
            prompt=request_dto.prompt,
            video=video_ref,
            image=image_ref,
            last_frame=last_frame_ref,
            reference_images=staged_refs,
            reference_audios=reference_audios,
            codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            omni_video_transform=omni_video_transform,
            audio_control=audio_control,
        )
        result, op_name = await _submit_and_wait_with_retry(
            client,
            vpe_req,
            media_repo,
            media_item_id,
            worker_logger,
        )
        return await _deliver_gmp_output(
            result=result,
            output_codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            dest_prefix=f"generated_videos/omni_cine_{media_item_id}",
            work=work,
            vpe_gcs=vpe_gcs,
            app_gcs=app_gcs,
            base_raw_data={
                "vpe_capability": VpeCapabilityId.OMNI_CINE.value,
                "vpe_operation": op_name,
            },
        )

    _run_gmp_generation_worker(
        media_item_id,
        "omni_cine",
        _job,
        request_dto.workspace_id,
    )


def _process_vpe_video_textures_in_background(
    media_item_id: int,
    request_dto: CreateVeoDto,
    user_email: str,
) -> None:
    """Background worker for veo-exp-video-textures (seamless loop & tiling)."""
    _ = user_email

    async def _job(
        media_repo: MediaRepository,
        source_asset_repo: SourceAssetRepository,
        app_gcs: GcsService,
        vpe_gcs: GcsService,
        client: VpeClient,
        work: Path,
        target_project_id: str | None,
        worker_logger: logging.Logger,
    ) -> dict[str, Any]:
        prefix = f"vpe/video_textures/{media_item_id}"

        start_uri = await _resolve_asset_ref_uri(
            request_dto.start_image_asset_id,
            media_repo,
            source_asset_repo,
        )
        end_uri = await _resolve_asset_ref_uri(
            request_dto.end_image_asset_id,
            media_repo,
            source_asset_repo,
        )
        if not start_uri and request_dto.source_media_items:
            for smi in request_dto.source_media_items:
                if smi.role == AssetRoleEnum.START_FRAME:
                    parent = await media_repo.get_by_id(smi.media_item_id)
                    if parent and parent.gcs_uris:
                        start_uri = parent.gcs_uris[smi.media_index]
                        break
        if not end_uri and request_dto.source_media_items:
            for smi in request_dto.source_media_items:
                if smi.role == AssetRoleEnum.END_FRAME:
                    parent = await media_repo.get_by_id(smi.media_item_id)
                    if parent and parent.gcs_uris:
                        end_uri = parent.gcs_uris[smi.media_index]
                        break

        image_ref = (
            await _stage_single_image(
                uri=start_uri,
                slot_name="start_frame",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            if start_uri
            else None
        )
        last_frame_ref = (
            await _stage_single_image(
                uri=end_uri,
                slot_name="last_frame",
                prefix=prefix,
                work=work,
                app_gcs=app_gcs,
                vpe_gcs=vpe_gcs,
                fallback_project_id=target_project_id,
            )
            if end_uri
            else None
        )

        seamless_flags = VpeSeamlessFlags(
            loop=request_dto.seamless_loop,
            tessellate_horizontal=request_dto.tessellate_horizontal,
            tessellate_vertical=request_dto.tessellate_vertical,
        )
        seamless_dict = {
            "loop": request_dto.seamless_loop,
            "tessellate_horizontal": request_dto.tessellate_horizontal,
            "tessellate_vertical": request_dto.tessellate_vertical,
        }

        vpe_req = VpeRequest(
            capability_id=VpeCapabilityId.VIDEO_TEXTURES,
            storage_uri=f"gs://{config_service.VPE_BUCKET}/{prefix}/out",
            prompt=(
                request_dto.prompt
                if request_dto.prompt and request_dto.prompt.strip()
                else None
            ),
            image=image_ref,
            last_frame=last_frame_ref,
            seamless=seamless_flags,
            seed=request_dto.seed,
            codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
        )
        result, op_name = await _submit_and_wait_with_retry(
            client,
            vpe_req,
            media_repo,
            media_item_id,
            worker_logger,
            base_raw_data={"seamless": seamless_dict},
        )
        return await _deliver_gmp_output(
            result=result,
            output_codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            dest_prefix=f"generated_videos/textures_{media_item_id}",
            work=work,
            vpe_gcs=vpe_gcs,
            app_gcs=app_gcs,
            base_raw_data={
                "vpe_capability": VpeCapabilityId.VIDEO_TEXTURES.value,
                "vpe_operation": op_name,
                "seamless": seamless_dict,
            },
        )

    _run_gmp_generation_worker(
        media_item_id,
        "video_textures",
        _job,
        request_dto.workspace_id,
    )


class VpeService:
    """Application-facing entry points for VPE / GMP capabilities."""

    def __init__(
        self,
        media_repo: MediaRepository = Depends(),
        source_asset_repo: SourceAssetRepository = Depends(),
        gcs_service: GcsService = Depends(),
        iam_signer: IamSignerCredentials = Depends(),
    ):
        """Initializes the service with its dependencies."""
        self.media_repo = media_repo
        self.source_asset_repo = source_asset_repo
        self.gcs_service = gcs_service
        self.iam_signer = iam_signer

    async def screen_media_item(
        self,
        media_item_id: int,
        capability_id: VpeCapabilityId = VpeCapabilityId.UPSCALE,
    ) -> VpeScreeningResult:
        """Judges a gallery row against a capability without downloading it."""
        item = await self.media_repo.get_by_id(media_item_id)
        raw_data: dict[str, Any] | None = None
        if item and item.mime_type == MimeTypeEnum.VIDEO_MP4:
            duration_seconds = item.duration_seconds
            resolution = item.resolution
            candidate_raw = getattr(item, "raw_data", None)
            if isinstance(candidate_raw, dict):
                raw_data = candidate_raw
        else:
            source_asset = await self.source_asset_repo.get_by_id(media_item_id)
            if (
                source_asset
                and source_asset.mime_type == MimeTypeEnum.VIDEO_MP4
            ):
                duration_seconds = None
                resolution = None
            else:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Video '{media_item_id}' not found or is not a video."
                    ),
                )

        if capability_id in (
            VpeCapabilityId.UPSCALE,
            VpeCapabilityId.SUPER_RES,
        ):
            return screen_upscale_candidate(
                duration_seconds=duration_seconds,
                resolution=resolution,
                raw_data=raw_data,
                max_segments=MAX_SEGMENTS_PER_JOB,
                capability_id=capability_id,
            )

        return screen_stored_video(
            get_capability(capability_id),
            duration_seconds=duration_seconds,
            resolution=resolution,
            max_segments=1,
        )

    async def start_upscale_job(
        self,
        request_dto: UpscaleVideoDto,
        user: UserModel,
        executor: ThreadPoolExecutor,
    ) -> MediaItemResponse:
        """Creates a placeholder row and queues the upscale behind it."""
        try:
            require_vpe_configured(config_service)
        except VpeJobError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        item = await self.media_repo.get_by_id(request_dto.media_item_id)
        parent_raw_data: dict[str, Any] | None = None
        if item and item.mime_type == MimeTypeEnum.VIDEO_MP4:
            if not item.gcs_uris or request_dto.media_index >= len(
                item.gcs_uris
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"MediaItem '{request_dto.media_item_id}' has no clip at"
                        f" index {request_dto.media_index}."
                    ),
                )
            source_gcs_uri = item.gcs_uris[request_dto.media_index]
            duration_seconds = item.duration_seconds
            resolution = item.resolution
            aspect_ratio = item.aspect_ratio
            candidate_raw = getattr(item, "raw_data", None)
            if isinstance(candidate_raw, dict):
                parent_raw_data = candidate_raw
        else:
            source_asset = await self.source_asset_repo.get_by_id(
                request_dto.media_item_id
            )
            if (
                not source_asset
                or source_asset.mime_type != MimeTypeEnum.VIDEO_MP4
            ):
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Video asset '{request_dto.media_item_id}' not found"
                        " or is not a video."
                    ),
                )
            source_gcs_uri = source_asset.gcs_uri
            duration_seconds = None
            resolution = None
            aspect_ratio = source_asset.aspect_ratio

        screening = screen_upscale_candidate(
            duration_seconds=duration_seconds,
            resolution=resolution,
            raw_data=parent_raw_data,
            max_segments=MAX_SEGMENTS_PER_JOB,
            capability_id=VpeCapabilityId.UPSCALE,
        )
        if not screening.offer:
            raise HTTPException(
                status_code=400,
                detail=" ".join(
                    finding.message for finding in screening.findings
                ),
            )

        resolved_mode = request_dto.resolved_mode
        if (
            screening.supported_modes
            and resolved_mode not in screening.supported_modes
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Upscale mode '{resolved_mode}' is not supported for this"
                    f" video. Supported modes: {list(screening.supported_modes)}."
                ),
            )

        uses_super_res = (
            not is_seamless_raw_data(parent_raw_data)
            and resolved_mode in _SUPER_RES_MODE_MAP
            and (duration_seconds is None or duration_seconds <= 10.05)
        )
        placeholder_model = (
            GenerationModelEnum.VEO_EXP_SUPER_RES
            if uses_super_res
            else GenerationModelEnum.VEO_3_1_UPSCALE
        )

        placeholder_item = MediaItemModel(
            workspace_id=request_dto.workspace_id,
            user_email=user.email,
            user_id=user.id,
            mime_type=MimeTypeEnum.VIDEO_MP4,
            model=placeholder_model,
            original_prompt=f"Upscale to {resolved_mode.upper()}",
            status=JobStatusEnum.PROCESSING,
            source_media_items=[
                SourceMediaItemLink(
                    media_item_id=request_dto.media_item_id,
                    media_index=request_dto.media_index,
                    role=AssetRoleEnum.UPSCALE_SOURCE,
                ),
            ],
            gcs_uris=[],
            thumbnail_uris=[],
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
        )
        placeholder_item = await self.media_repo.create(placeholder_item)

        executor.submit(
            _process_vpe_upscale_in_background,
            media_item_id=placeholder_item.id,
            source_gcs_uri=source_gcs_uri,
            resolution=request_dto.resolution,
            sharpness=request_dto.sharpness,
            workspace_id=request_dto.workspace_id,
            upscale_mode=resolved_mode,
            steps=request_dto.steps,
            seed=request_dto.seed,
            output_codec=request_dto.output_codec,
            compression_quality=request_dto.compression_quality,
            parent_raw_data=parent_raw_data,
        )
        logger.info("VPE upscale job queued: %s", placeholder_item.id)

        return MediaItemResponse(
            **placeholder_item.model_dump(),
            presigned_urls=[],
            presigned_thumbnail_urls=[],
        )
