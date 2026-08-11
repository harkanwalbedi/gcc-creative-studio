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
"""Creative Studio's integration of Veo Pro Experimental.

``src/videos/vpe/`` is a pure library: it knows the API and nothing about
this application. This module is the other half - it owns the gallery row,
the buckets, the temporary files and the background thread, and it sits
beside ``veo_service`` rather than inside the package so the package stays
importable and inert on a deployment where VPE is switched off.

Only the upscaler is wired up. It is the capability with a live measured
result behind it and the one the customer asked for.

Three things about the shape of this worker are not obvious:

**It splits.** The upscaler takes 4 to 8 seconds and production shots are 9
or 10, so a full-length master only exists if the clip is cut up, upscaled
in pieces and rejoined. That the rejoin is invisible was measured on real
footage rather than assumed; see section 6g of the integration plan.

**It submits every segment before waiting for any.** Submission is cheap and
the wait is minutes, so submitting first lets the service run the pieces
concurrently, and it puts every operation name on the row before this worker
blocks on anything - which is what makes a job survive the process dying.

**It crosses projects.** VPE reads and writes its own bucket inside the
allowlisted project, which is not the project holding ``GENMEDIA_BUCKET``.
Video therefore comes down out of one bucket and goes up into another rather
than being handed over by URI.
"""

import asyncio
import logging
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, HTTPException
from google.cloud.logging import Client as LoggerClient
from google.cloud.logging.handlers import CloudLoggingHandler

from src.common.base_dto import GenerationModelEnum, MimeTypeEnum
from src.common.media_utils import generate_thumbnail
from src.common.schema.media_item_model import (
    AssetRoleEnum,
    JobStatusEnum,
    MediaItemModel,
    SourceMediaItemLink,
)
from src.common.storage_service import GcsService
from src.config.config_service import ConfigService, config_service
from src.galleries.dto.gallery_response_dto import MediaItemResponse
from src.images.repository.media_item_repository import MediaRepository
from src.users.user_model import UserModel
from src.videos.dto.upscale_video_dto import UpscaleVideoDto
from src.videos.veo_service import (
    build_measured_metadata,
    record_worker_progress,
)
from src.videos.vpe.capabilities import (
    VpeCapabilityId,
    VpeUpscaleResolution,
    get_capability,
)
from src.videos.vpe.client import VpeClient
from src.videos.vpe.gating import VpeScreeningResult, screen_stored_video
from src.videos.vpe.media_ops import (
    concat_segments,
    cut_segment,
    restore_audio,
)
from src.videos.vpe.payloads import VpeMediaRef, VpeRequest, build_payload
from src.videos.vpe.preflight import (
    VpeMediaProbe,
    VpeReasonCode,
    probe_media,
    validate,
)
from src.videos.vpe.segmentation import VpeSegment, plan_segments

logger = logging.getLogger(__name__)

_MP4 = MimeTypeEnum.VIDEO_MP4.value

# Per segment, not per job. A 4K upscale of an 8 second clip was measured at
# around 315 seconds, and the ceiling has to leave room for a queue in front
# of it, so this is deliberately far above the observed time: hitting it
# should mean something is wrong, not that the service was busy.
SEGMENT_TIMEOUT_SECONDS = 1800.0

# A clip long enough to need this many pieces is not a shot. Splitting it
# would mean dozens of paid jobs and dozens of seams, and the request is
# much more likely to be a whole edit submitted by mistake.
MAX_SEGMENTS_PER_JOB = 8

# The frame count being over the maximum is the one blocking finding this
# worker can answer, because splitting is exactly the remedy for it. Every
# other block stands: a 30 fps clip, a 4K source or a 960x540 frame is not
# something cutting the clip up will fix.
_SEGMENTABLE_CODES = frozenset({VpeReasonCode.FRAME_COUNT_TOO_HIGH})


class VpeJobError(RuntimeError):
    """Raised when an upscale cannot be run as requested."""


def aspect_ratio_for(probe: VpeMediaProbe) -> str:
    """Returns the aspect ratio to declare for a measured clip.

    The upscaler accepts ``16:9`` and ``9:16`` only, and rejects a declared
    ratio that disagrees with the pixels. Anything that is neither has
    already been refused by the frame-size check, so orientation is enough
    to choose between the two.

    Args:
        probe: A measurement of the source clip.

    Returns:
        The aspect ratio string to send.
    """
    return "9:16" if probe.is_portrait else "16:9"


def check_upscalable(probe: VpeMediaProbe) -> None:
    """Refuses a source the upscaler cannot be made to accept.

    Args:
        probe: A measurement of the source clip.

    Raises:
        VpeJobError: If the clip breaks a rule splitting cannot fix.
    """
    verdict = validate(probe, VpeCapabilityId.UPSCALE)
    fatal = [
        finding
        for finding in verdict.blocking
        if finding.code not in _SEGMENTABLE_CODES
    ]
    if fatal:
        raise VpeJobError(" ".join(finding.message for finding in fatal))


def plan_upscale(probe: VpeMediaProbe) -> tuple[VpeSegment, ...]:
    """Decides how a measured clip is divided for upscaling.

    Args:
        probe: A measurement of the source clip.

    Returns:
        The segments to upscale, one of them the whole clip if it already
        fits the window.

    Raises:
        VpeJobError: If the clip cannot be measured or would need more
            segments than a single shot plausibly should.
    """
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
) -> VpeRequest:
    """Builds the upscale request for one already-uploaded segment.

    Args:
        segment_uri: Where the segment was uploaded, in VPE's bucket.
        storage_uri: The folder VPE should write this segment's output into.
        aspect_ratio: The measured ratio, which must match the pixels.
        resolution: Target resolution.
        sharpness: Sharpening strength, or None to leave it to the model.

    Returns:
        The request to hand to ``build_payload``.
    """
    return VpeRequest(
        capability_id=VpeCapabilityId.UPSCALE,
        storage_uri=storage_uri,
        video=VpeMediaRef(gcs_uri=segment_uri, mime_type=_MP4),
        resolution=resolution.value,
        aspect_ratio=aspect_ratio,
        sharpness=sharpness,
    )


def require_vpe_configured(cfg: ConfigService) -> None:
    """Refuses a job on a deployment that cannot run one.

    Failing here costs nothing. The same misconfiguration found later fails
    several minutes into a job, after a download and a cut.

    Args:
        cfg: The settings to check.

    Raises:
        VpeJobError: If VPE is disabled, or has no bucket or project of its
            own.
    """
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


def _worker_logger(media_item_id: int) -> logging.Logger:
    """Builds the per-job logger, matching the other video workers.

    Args:
        media_item_id: The job whose logger this is.

    Returns:
        A configured logger writing to Cloud Logging in production.
    """
    worker_logger = logging.getLogger(f"vpe_upscale_worker.{media_item_id}")
    worker_logger.setLevel(logging.INFO)
    if worker_logger.hasHandlers():
        worker_logger.handlers.clear()

    if os.getenv("ENVIRONMENT") == "production":
        handler = CloudLoggingHandler(
            LoggerClient(),
            name=f"vpe_upscale_worker.{media_item_id}",
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - [VPE_UPSCALE_WORKER] - %(levelname)s -"
                " %(message)s",
            ),
        )
    worker_logger.addHandler(handler)
    return worker_logger


def _blob_path(gcs_uri: str) -> str:
    """Strips a gs:// URI down to the object path within its bucket.

    Args:
        gcs_uri: A full ``gs://bucket/path`` URI.

    Returns:
        The object path, without the bucket.
    """
    without_scheme = gcs_uri.removeprefix("gs://")
    _, _, path = without_scheme.partition("/")
    return path


def _process_vpe_upscale_in_background(  # noqa: PLR0915
    media_item_id: int,
    source_gcs_uri: str,
    resolution: VpeUpscaleResolution,
    sharpness: int | None,
):
    """Background worker that upscales one gallery video.

    Args:
        media_item_id: The placeholder row to fill in.
        source_gcs_uri: The clip to upscale, in the app's own bucket.
        resolution: Target resolution.
        sharpness: Sharpening strength, or None for the model's default.
    """
    from src.database import WorkerDatabase

    worker_logger = _worker_logger(media_item_id)
    temp_dir = f"temp/vpe_{media_item_id}"

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _async_worker():
            async with WorkerDatabase() as db_factory:
                async with db_factory() as db:
                    media_repo = MediaRepository(db)
                    cfg = config_service
                    # Two buckets, deliberately. The app's own bucket holds
                    # the source and takes the master; VPE's sits in the
                    # allowlisted project and is the only place the API will
                    # read from or write to.
                    app_gcs = GcsService()
                    vpe_gcs = GcsService(bucket_name=cfg.VPE_BUCKET)
                    # Same reason as the bucket above: the allowlisted
                    # project is not necessarily this app's own, and a
                    # client built without the override would silently call
                    # under a project that was never allowlisted.
                    client = VpeClient(project_id=cfg.VPE_PROJECT_ID)
                    uploaded_segment_uris: list[str] = []

                    try:
                        # A row queued behind other jobs is otherwise
                        # indistinguishable from one whose worker never ran.
                        await record_worker_progress(
                            media_repo,
                            media_item_id,
                            worker_logger,
                        )
                        require_vpe_configured(cfg)

                        start_time = time.monotonic()
                        work = Path(temp_dir)
                        work.mkdir(parents=True, exist_ok=True)

                        # 1. Bring the source down. VPE cannot read the app's
                        # bucket, and every measurement below needs the file
                        # anyway.
                        source_path = str(work / "source.mp4")
                        downloaded = await asyncio.to_thread(
                            app_gcs.download_from_gcs,
                            gcs_uri_path=_blob_path(source_gcs_uri),
                            destination_file_path=source_path,
                        )
                        if not downloaded:
                            raise VpeJobError(
                                f"Could not download {source_gcs_uri}.",
                            )

                        # 2. Measure it. The row's stored metadata was only
                        # ever enough to rule the job out; the upscaler
                        # validates a frame rate and an exact frame size that
                        # the row does not carry, so the file is the only
                        # thing that can clear a job to run.
                        probe = await asyncio.to_thread(
                            probe_media, source_path
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

                        # 3. Cut and upload every piece, then submit them all
                        # before waiting on any. The wait is minutes and the
                        # submission is a round trip, so this is what lets the
                        # segments run concurrently.
                        prefix = f"vpe/upscale/{media_item_id}"
                        operations = []
                        for segment in segments:
                            piece = await asyncio.to_thread(
                                cut_segment,
                                source_path,
                                work / f"in_{segment.index:02d}.mp4",
                                first_frame=segment.first_frame,
                                frames=segment.frames,
                                fps=int(probe.fps),
                            )
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
                            request = build_segment_request(
                                segment_uri=segment_uri,
                                storage_uri=(
                                    f"gs://{cfg.VPE_BUCKET}/{prefix}"
                                    f"/out_{segment.index:02d}"
                                ),
                                aspect_ratio=aspect_ratio,
                                resolution=resolution,
                                sharpness=sharpness,
                            )
                            operations.append(
                                await asyncio.to_thread(
                                    client.submit,
                                    build_payload(request),
                                ),
                            )

                        # 4. Record the operation names before blocking on
                        # them. This is the whole recoverability story: the
                        # pool is shut down without waiting, so an
                        # interrupted worker loses its polling loop and
                        # nothing else. Without these names on the row there
                        # is no way to ask the service what became of a job
                        # that was already paid for.
                        await record_worker_progress(
                            media_repo,
                            media_item_id,
                            worker_logger,
                            {
                                "raw_data": {
                                    "vpe_capability": (
                                        VpeCapabilityId.UPSCALE.value
                                    ),
                                    "vpe_operation_names": [
                                        operation.name
                                        for operation in operations
                                    ],
                                    "vpe_segments": [
                                        {
                                            "index": segment.index,
                                            "first_frame": segment.first_frame,
                                            "frames": segment.frames,
                                        }
                                        for segment in segments
                                    ],
                                },
                            },
                        )

                        # 5. Wait for each in turn. They are already running
                        # side by side, so the cost is the slowest of them
                        # rather than the sum.
                        upscaled: list[Path] = []
                        for segment, operation in zip(segments, operations):
                            result = await asyncio.to_thread(
                                client.wait_for_completion_sync,
                                operation,
                                timeout_seconds=SEGMENT_TIMEOUT_SECONDS,
                            )
                            local = work / f"out_{segment.index:02d}.mp4"
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
                                    " output could not be downloaded from"
                                    f" {result.primary_video.gcs_uri}.",
                                )
                            upscaled.append(local)
                            await record_worker_progress(
                                media_repo,
                                media_item_id,
                                worker_logger,
                            )

                        # 6. Rejoin, then lay the original sound back over
                        # the result. A single segment skips the join: there
                        # is no seam to make and a stream copy would only
                        # cost a rewrite.
                        if len(upscaled) == 1:
                            joined = upscaled[0]
                        else:
                            joined = await asyncio.to_thread(
                                concat_segments,
                                list(upscaled),
                                work / "joined.mp4",
                            )
                        final_path = await asyncio.to_thread(
                            restore_audio,
                            joined,
                            source_path,
                            work / "master.mp4",
                        )

                        # 7. Back into the app's bucket, where the gallery
                        # can serve it.
                        destination = (
                            f"upscaled_videos/{media_item_id}_"
                            f"{resolution.value}.mp4"
                        )
                        final_gcs_uri = await asyncio.to_thread(
                            app_gcs.upload_file_to_gcs,
                            local_path=str(final_path),
                            destination_blob_name=destination,
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
                                    f"upscaled_videos/{media_item_id}"
                                    "_thumb.png"
                                ),
                                mime_type="image/png",
                            )

                        # 8. Measure the delivered master rather than
                        # assuming it. The request asked for 4K; what the row
                        # should say is what came back.
                        measured_metadata = await asyncio.to_thread(
                            build_measured_metadata,
                            str(final_path),
                        )

                        await media_repo.update(
                            media_item_id,
                            {
                                "status": JobStatusEnum.COMPLETED,
                                "gcs_uris": [final_gcs_uri],
                                "thumbnail_uris": (
                                    [thumbnail_gcs_uri]
                                    if thumbnail_gcs_uri
                                    else []
                                ),
                                "generation_time": (
                                    time.monotonic() - start_time
                                ),
                                "num_media": 1,
                                **measured_metadata,
                            },
                        )
                        # Only on success. A failed job's inputs are the
                        # reproduction case, and they are the first thing
                        # anyone debugging it will ask for.
                        await asyncio.to_thread(
                            _discard_segments,
                            vpe_gcs,
                            uploaded_segment_uris,
                            worker_logger,
                        )
                        worker_logger.info(
                            "Upscaled media item %s to %s",
                            media_item_id,
                            resolution.value,
                        )

                    except Exception as error:  # noqa: BLE001
                        worker_logger.error(
                            "VPE upscale failed: %s",
                            error,
                            exc_info=True,
                        )
                        try:
                            # Without the rollback a database failure takes
                            # the status write down with it and the job never
                            # leaves `processing`.
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


def _discard_segments(
    vpe_gcs: GcsService,
    uris: list[str],
    worker_logger: logging.Logger,
) -> None:
    """Deletes the intermediate segments uploaded for one job.

    Best effort. The master is already saved by the time this runs, so a
    failure here is litter in a bucket and not a lost job.

    Args:
        vpe_gcs: Storage service pointed at VPE's bucket.
        uris: The uploaded segment URIs to remove.
        worker_logger: The job's logger.
    """
    for uri in uris:
        try:
            vpe_gcs.delete_blob_from_uri(uri)
        except Exception as error:  # noqa: BLE001
            worker_logger.warning("Could not delete %s: %s", uri, error)


class VpeService:
    """Application-facing entry points for VPE capabilities."""

    def __init__(
        self,
        media_repo: MediaRepository = Depends(),
        gcs_service: GcsService = Depends(),
    ):
        """Initializes the service with its dependencies.

        Args:
            media_repo: Repository owning gallery rows.
            gcs_service: Storage service for the application's own bucket.
        """
        self.media_repo = media_repo
        self.gcs_service = gcs_service

    async def screen_media_item(
        self,
        media_item_id: int,
        capability_id: VpeCapabilityId = VpeCapabilityId.UPSCALE,
    ) -> VpeScreeningResult:
        """Judges a gallery row against a capability without downloading it.

        Cheap enough to call while drawing a menu. It can only ever rule a
        capability out: the row stores duration and a resolution *name*, and
        the upscaler validates a frame rate the row does not carry and an
        exact pixel size the name cannot confirm.

        Args:
            media_item_id: The row to screen.
            capability_id: The capability to screen against.

        Returns:
            The screening outcome and the findings behind it.

        Raises:
            HTTPException: If the row does not exist or is not a video.
        """
        item = await self.media_repo.get_by_id(media_item_id)
        if not item or item.mime_type != MimeTypeEnum.VIDEO_MP4:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"MediaItem '{media_item_id}' not found or is not a"
                    " video."
                ),
            )
        return screen_stored_video(
            get_capability(capability_id),
            duration_seconds=item.duration_seconds,
            resolution=item.resolution,
        )

    async def start_upscale_job(
        self,
        request_dto: UpscaleVideoDto,
        user: UserModel,
        executor: ThreadPoolExecutor,
    ) -> MediaItemResponse:
        """Creates a placeholder row and queues the upscale behind it.

        Args:
            request_dto: The validated request.
            user: The user the new row belongs to.
            executor: VPE's own thread pool.

        Returns:
            The placeholder row, already visible in the gallery as
            processing.

        Raises:
            HTTPException: If VPE is unavailable, or the source row is
                missing, not a video, or already ruled out.
        """
        try:
            require_vpe_configured(config_service)
        except VpeJobError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

        item = await self.media_repo.get_by_id(request_dto.media_item_id)
        if not item or item.mime_type != MimeTypeEnum.VIDEO_MP4:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"MediaItem '{request_dto.media_item_id}' not found or is"
                    " not a video."
                ),
            )
        if not item.gcs_uris or request_dto.media_index >= len(item.gcs_uris):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"MediaItem '{request_dto.media_item_id}' has no clip at"
                    f" index {request_dto.media_index}."
                ),
            )
        source_gcs_uri = item.gcs_uris[request_dto.media_index]

        # The screening here is the same one the gallery used to decide
        # whether to offer the action. Repeating it costs a comparison and
        # catches the case where the row changed, or the caller never asked.
        # It cannot replace the worker's own preflight: the row does not
        # record a frame rate.
        screening = screen_stored_video(
            get_capability(VpeCapabilityId.UPSCALE),
            duration_seconds=item.duration_seconds,
            resolution=item.resolution,
        )
        if not screening.offer:
            raise HTTPException(
                status_code=400,
                detail=" ".join(
                    finding.message for finding in screening.findings
                ),
            )

        placeholder_item = MediaItemModel(
            workspace_id=request_dto.workspace_id,
            user_email=user.email,
            user_id=user.id,
            mime_type=MimeTypeEnum.VIDEO_MP4,
            model=GenerationModelEnum.VEO_3_1_UPSCALE,
            original_prompt=(
                f"Upscale to {request_dto.resolution.value.upper()}"
            ),
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
            aspect_ratio=item.aspect_ratio,
            duration_seconds=item.duration_seconds,
        )
        placeholder_item = await self.media_repo.create(placeholder_item)

        executor.submit(
            _process_vpe_upscale_in_background,
            media_item_id=placeholder_item.id,
            source_gcs_uri=source_gcs_uri,
            resolution=request_dto.resolution,
            sharpness=request_dto.sharpness,
        )
        logger.info("VPE upscale job queued: %s", placeholder_item.id)

        return MediaItemResponse(
            **placeholder_item.model_dump(),
            presigned_urls=[],
            presigned_thumbnail_urls=[],
        )
