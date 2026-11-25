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
"""Tests for the decisions an upscale job makes before it spends money.

The worker itself needs a database, two buckets and an allowlisted project,
so what is tested here is everything it decides on the way: whether a clip
can be upscaled at all, how it is divided, what ratio is declared and what
payload comes out. Those are the failures that would otherwise surface five
minutes into a paid job.
"""

from fractions import Fraction
import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.common.base_dto import GenerationModelEnum, MimeTypeEnum
from src.config.config_service import config_service
from src.users.user_model import UserModel, UserRoleEnum
from src.videos.dto.upscale_video_dto import UpscaleVideoDto
from src.videos.vpe.capabilities import VpeUpscaleResolution
from src.videos.vpe.payloads import build_payload
from src.videos.vpe.preflight import VpeMediaKind, VpeMediaProbe
from src.videos.vpe.segmentation import VpeSegmentationError
from src.videos.vpe_service import (
    MAX_SEGMENTS_PER_JOB,
    VpeJobError,
    VpeService,
    _blob_path,
    _resolve_asset_ref_uri,
    _resolve_workspace_gcs_config,
    _uri_bucket_name,
    aspect_ratio_for,
    build_segment_request,
    check_upscalable,
    plan_upscale,
    require_vpe_configured,
)


def _probe(
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int | Fraction = 24,
    frame_count: int = 240,
    **overrides,
) -> VpeMediaProbe:
    """Builds a probe of a plausible source clip.

    Defaults to the customer's real case: ten seconds of 1080p at 24 fps,
    which is legal in every respect except being too long for one job.

    Args:
        width: Frame width in pixels.
        height: Frame height in pixels.
        fps: Measured frame rate.
        frame_count: Measured frames.
        **overrides: Any other probe field.

    Returns:
        The probe.
    """
    fields = {
        "path": "/tmp/source.mp4",
        "kind": VpeMediaKind.VIDEO,
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "size_bytes": 12_000_000,
        "width": width,
        "height": height,
        "fps": Fraction(fps),
        "nominal_fps": Fraction(fps),
        "frame_count": frame_count,
        "container_seconds": (
            frame_count / float(fps) if frame_count else None
        ),
        "has_audio": True,
        "video_codec": "h264",
        "audio_codec": "aac",
        "mime_type": "video/mp4",
    }
    fields.update(overrides)
    return VpeMediaProbe(**fields)


class TestAspectRatioFor:
    """The upscaler rejects a ratio that disagrees with the pixels."""

    def test_landscape_is_sixteen_nine(self):
        """1920x1080 is declared 16:9."""
        assert aspect_ratio_for(_probe()) == "16:9"

    def test_portrait_is_nine_sixteen(self):
        """A vertical clip must not be declared landscape."""
        assert aspect_ratio_for(_probe(width=1080, height=1920)) == "9:16"

    def test_720p_portrait_is_nine_sixteen(self):
        """The other accepted portrait size answers the same way."""
        assert aspect_ratio_for(_probe(width=720, height=1280)) == "9:16"


class TestCheckUpscalable:
    """What splitting can fix, and what it cannot."""

    def test_a_ten_second_clip_passes(self):
        """Too long is the one block the worker answers by splitting."""
        check_upscalable(_probe())

    def test_a_clip_inside_the_window_passes(self):
        """Nothing to forgive here at all."""
        check_upscalable(_probe(frame_count=144))

    def test_the_wrong_frame_rate_is_refused(self):
        """No division of a 30 fps clip is 24 fps."""
        with pytest.raises(VpeJobError, match="24"):
            check_upscalable(_probe(fps=30, frame_count=300))

    def test_a_frame_size_below_720p_is_refused(self):
        """Cutting a 640x360 clip up leaves every piece 640x360."""
        with pytest.raises(VpeJobError):
            check_upscalable(_probe(width=640, height=360))

    def test_a_4k_source_is_refused(self):
        """The upscaler takes 720p and 1080p in; 4K is already past it."""
        with pytest.raises(VpeJobError):
            check_upscalable(_probe(width=3840, height=2160))

    def test_a_clip_below_the_minimum_is_refused(self):
        """Two seconds cannot be padded up to four."""
        with pytest.raises(VpeJobError):
            check_upscalable(_probe(frame_count=48))

    def test_the_message_explains_itself(self):
        """A refusal reaches the user as the row's error message."""
        with pytest.raises(VpeJobError) as caught:
            check_upscalable(_probe(fps=25, frame_count=250))
        assert str(caught.value).strip()

    def test_a_prores_source_passes(self):
        """The other block the worker answers for itself.

        Every segment is re-encoded to H.264 on the way out, so the API is
        never shown the codec the source arrived in. Refusing here would
        turn a clip this pipeline handles correctly into an error.
        """
        check_upscalable(
            _probe(video_codec="prores", mime_type="video/quicktime"),
        )

    def test_a_prores_source_is_still_held_to_every_other_rule(self):
        """Tolerating the codec must not tolerate anything alongside it.

        A re-encode changes the codec and nothing else, so a 30 fps ProRes
        clip is exactly as unusable as a 30 fps H.264 one.
        """
        with pytest.raises(VpeJobError, match="24"):
            check_upscalable(
                _probe(
                    fps=30,
                    frame_count=300,
                    video_codec="prores",
                    mime_type="video/quicktime",
                ),
            )

    def test_a_still_image_is_refused(self):
        """A file that is not a video is not a codec problem.

        Both arrive as a mime type the capability does not accept, so this
        is the case that proves the tolerance keys on the reason code rather
        than on the mime check having fired at all.
        """
        # Built directly rather than through ``_probe``, which fills in the
        # frame rate and sound a still does not have.
        still = VpeMediaProbe(
            path="/tmp/frame.png",
            kind=VpeMediaKind.IMAGE,
            container="png_pipe",
            size_bytes=400_000,
            width=1920,
            height=1080,
            frame_count=1,
            mime_type="image/png",
            video_codec="png",
        )
        with pytest.raises(VpeJobError, match="image"):
            check_upscalable(still)


class TestPlanUpscale:
    """How many jobs a clip turns into."""

    def test_ten_seconds_becomes_two_jobs(self):
        """The customer's real case."""
        segments = plan_upscale(_probe())
        assert [s.frames for s in segments] == [120, 120]

    def test_a_short_clip_stays_one_job(self):
        """An already-legal clip must not be split for no reason."""
        assert len(plan_upscale(_probe(frame_count=144))) == 1

    def test_an_unmeasurable_clip_is_refused(self):
        """The upscaler's limits are frame counts, so a guess is not enough."""
        with pytest.raises(VpeJobError, match="frames"):
            plan_upscale(_probe(frame_count=None))

    def test_a_whole_edit_is_refused_rather_than_split(self):
        """A five minute timeline is dozens of paid jobs and dozens of seams."""
        with pytest.raises(VpeJobError, match=str(MAX_SEGMENTS_PER_JOB)):
            plan_upscale(_probe(frame_count=24 * 300))

    def test_the_limit_itself_is_allowed(self):
        """The refusal is over the limit, not at it."""
        frames = MAX_SEGMENTS_PER_JOB * 192
        assert len(plan_upscale(_probe(frame_count=frames))) == (
            MAX_SEGMENTS_PER_JOB
        )

    def test_a_clip_below_the_minimum_raises_from_the_planner(self):
        """Refused either way, but the planner's message is the specific one."""
        with pytest.raises(VpeSegmentationError):
            plan_upscale(_probe(frame_count=48))


class TestBuildSegmentRequest:
    """The payload is what the API actually judges."""

    def _payload(self, **overrides) -> dict:
        """Builds the wire payload for one segment.

        Args:
            **overrides: Fields to change from the defaults.

        Returns:
            The rendered payload.
        """
        fields = {
            "segment_uri": "gs://vpe-bucket/vpe/upscale/7/in_00.mp4",
            "storage_uri": "gs://vpe-bucket/vpe/upscale/7/out_00",
            "aspect_ratio": "16:9",
            "resolution": VpeUpscaleResolution.UHD_4K,
            "sharpness": None,
        }
        fields.update(overrides)
        return build_payload(build_segment_request(**fields))

    def test_it_names_the_upscale_checkpoint(self):
        """One endpoint serves eight capabilities; the payload picks one."""
        experiments = self._payload()["parameters"]["experiments"]
        assert experiments["modelName"] == "veo3p1_upscale"

    def test_the_resolution_is_sent_explicitly(self):
        """The documented default is 720p, which would upscale nothing."""
        assert self._payload()["parameters"]["resolution"] == "4k"

    def test_the_aspect_ratio_is_sent(self):
        """A 1080p input errors without it."""
        assert self._payload()["parameters"]["aspectRatio"] == "16:9"

    def test_the_segment_is_the_input_video(self):
        """The piece is what goes up, never the whole clip."""
        video = self._payload()["instances"][0]["video"]
        assert video["gcsUri"].endswith("in_00.mp4")

    def test_sharpness_is_omitted_when_unset(self):
        """Not sending it leaves the model on its own default."""
        assert "sharpness" not in self._payload()["parameters"]

    def test_sharpness_is_sent_when_asked_for(self):
        """And present when the user did choose."""
        assert self._payload(sharpness=3)["parameters"]["sharpness"] == 3

    def test_each_segment_writes_to_its_own_folder(self):
        """A shared folder would leave two jobs racing for one output name."""
        first = self._payload(storage_uri="gs://b/j/out_00")
        second = self._payload(storage_uri="gs://b/j/out_01")
        assert (
            first["parameters"]["storageUri"]
            != second["parameters"]["storageUri"]
        )


class TestRequireVpeConfigured:
    """Failing here costs nothing; failing later costs a download and a cut."""

    def test_a_disabled_deployment_is_refused(self, monkeypatch):
        """The feature ships dark."""
        cfg = _settings(monkeypatch, enabled=False, bucket="b")
        with pytest.raises(VpeJobError, match="not enabled"):
            require_vpe_configured(cfg)

    def test_a_missing_bucket_is_refused(self, monkeypatch):
        """VPE reads and writes Cloud Storage only."""
        cfg = _settings(monkeypatch, enabled=True, bucket="", project_id="p")
        with pytest.raises(VpeJobError, match="VPE_BUCKET"):
            require_vpe_configured(cfg)

    def test_a_missing_project_is_refused(self, monkeypatch):
        """The deployment project is not necessarily the allowlisted one."""
        cfg = _settings(monkeypatch, enabled=True, bucket="b", project_id="")
        with pytest.raises(VpeJobError, match="VPE_PROJECT_ID"):
            require_vpe_configured(cfg)

    def test_a_configured_deployment_passes(self, monkeypatch):
        """All three set is the only combination that runs."""
        require_vpe_configured(
            _settings(monkeypatch, enabled=True, bucket="b", project_id="p"),
        )


def _settings(
    monkeypatch, *, enabled: bool, bucket: str, project_id: str = "p"
):
    """Returns the app settings with VPE's switches overridden.

    Args:
        monkeypatch: pytest's attribute patcher.
        enabled: Value for VPE_ENABLED.
        bucket: Value for VPE_BUCKET.
        project_id: Value for VPE_PROJECT_ID.

    Returns:
        The patched settings object.
    """
    monkeypatch.setattr(config_service, "VPE_ENABLED", enabled)
    monkeypatch.setattr(config_service, "VPE_BUCKET", bucket)
    monkeypatch.setattr(config_service, "VPE_PROJECT_ID", project_id)
    return config_service


class TestBlobPath:
    """Downloading needs the object path, not the whole URI."""

    def test_the_bucket_is_stripped(self):
        """GcsService is already pointed at the bucket."""
        assert _blob_path("gs://a-bucket/out/7/sample_0.mp4") == (
            "out/7/sample_0.mp4"
        )

    def test_a_nested_path_survives_whole(self):
        """Only the first segment is the bucket."""
        assert _blob_path("gs://b/a/b/c/d.mp4") == "a/b/c/d.mp4"


def _row(*, duration_seconds: float, resolution: str = "1K"):
    """A stand-in for the row screening reads, needing only three fields."""
    row = Mock()
    row.mime_type = MimeTypeEnum.VIDEO_MP4
    row.duration_seconds = duration_seconds
    row.resolution = resolution
    row.workspace_id = 1
    row.gcs_uris = ["gs://bucket/source.mp4"]
    row.aspect_ratio = "16:9"
    return row


class TestScreenMediaItem:
    """The service method a click on a gallery row actually calls.

    This is the layer gating.py's own tests cannot reach: it is what would
    have caught a screening call built without max_segments, since that bug
    lived entirely in what this method passed through, not in
    screen_stored_video itself.
    """

    @pytest.mark.anyio
    async def test_a_ten_second_clip_is_offered(self):
        """The customer's real case: 10s, split-and-rejoin covers it.

        Regression test: screen_media_item once called screen_stored_video
        with no max_segments, so this exact row was ruled out and the
        gallery button never appeared - even though the worker behind it
        has been proven live to handle this duration correctly.
        """
        media_repo = AsyncMock()
        media_repo.get_by_id = AsyncMock(
            return_value=_row(duration_seconds=10.0),
        )
        service = VpeService(media_repo=media_repo, gcs_service=AsyncMock())

        result = await service.screen_media_item(7)

        assert result.offer is True

    @pytest.mark.anyio
    async def test_a_clip_beyond_every_segment_is_still_ruled_out(self):
        """Segmentation credit is not unlimited."""
        too_long = 8.0 * (MAX_SEGMENTS_PER_JOB + 1)
        media_repo = AsyncMock()
        media_repo.get_by_id = AsyncMock(
            return_value=_row(duration_seconds=too_long),
        )
        service = VpeService(media_repo=media_repo, gcs_service=AsyncMock())

        result = await service.screen_media_item(7)

        assert result.offer is False


class TestStartUpscaleJob:
    """The endpoint's own entry point, screening included."""

    @pytest.mark.anyio
    async def test_a_ten_second_clip_is_queued_not_refused(self, monkeypatch):
        """Regression test for the same gap, at the actual submit path.

        Before max_segments was threaded through, this call raised a 400
        for every real production shot longer than 8 seconds - the exact
        majority case split-and-rejoin exists to cover - because screening
        ran ahead of the worker's own, correct forgiveness of a long clip.
        """
        monkeypatch.setattr(config_service, "VPE_ENABLED", True)
        monkeypatch.setattr(config_service, "VPE_BUCKET", "vpe-bucket")
        monkeypatch.setattr(config_service, "VPE_PROJECT_ID", "vpe-project")
        media_repo = AsyncMock()
        media_repo.get_by_id = AsyncMock(
            return_value=_row(duration_seconds=10.0),
        )
        media_repo.create = AsyncMock(side_effect=lambda item: item)
        service = VpeService(media_repo=media_repo, gcs_service=AsyncMock())
        user = UserModel(
            id=1,
            email="user@example.com",
            name="Test User",
            roles=[UserRoleEnum.USER],
        )
        request_dto = UpscaleVideoDto(workspace_id=1, media_item_id=7)

        placeholder = await service.start_upscale_job(
            request_dto=request_dto,
            user=user,
            executor=Mock(),
        )

        assert placeholder.status == "processing"


class TestProcessVpeDialogueInBackground:
    """Tests for the dialogue background worker."""

    def test_worker_fails_gracefully_when_unconfigured(self, monkeypatch):
        from src.videos.dto.create_veo_dto import CreateVeoDto
        from src.videos.vpe_service import _process_vpe_dialogue_in_background

        monkeypatch.setattr(config_service, "VPE_ENABLED", False)

        dto = CreateVeoDto(
            prompt="Dialogue test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_A2V_GENERATION,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            reference_audio={"id": 2, "type": "source_asset"},
            duration_seconds=8,
            resolution="1K",
        )

        # Worker catches errors, logs, and handles DB updates
        _process_vpe_dialogue_in_background(
            media_item_id=999,
            request_dto=dto,
            user_email="test@example.com",
        )


class TestProcessVpeTransformInBackground:
    """Tests for the video transform background worker."""

    def test_worker_fails_gracefully_when_unconfigured(self, monkeypatch):
        from src.videos.dto.create_veo_dto import CreateVeoDto
        from src.videos.vpe_service import _process_vpe_transform_in_background

        monkeypatch.setattr(config_service, "VPE_ENABLED", False)

        dto = CreateVeoDto(
            prompt="Transform test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TRANSFORM,
            edit_source={"id": 1, "type": "source_asset"},
            video_transform_strength=0.5,
            num_diffusion_steps=20,
            duration_seconds=8,
            resolution="1K",
        )

        # Worker catches errors, logs, and handles DB updates
        _process_vpe_transform_in_background(
            media_item_id=999,
            request_dto=dto,
            user_email="test@example.com",
        )

    def test_masked_transform_dto_and_worker(self, monkeypatch):
        from src.videos.dto.create_veo_dto import CreateVeoDto
        from src.videos.vpe_service import _process_vpe_transform_in_background

        monkeypatch.setattr(config_service, "VPE_ENABLED", False)

        dto = CreateVeoDto(
            prompt="Masked transform test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TRANSFORM,
            edit_source={"id": 1, "type": "source_asset"},
            video_transform_mask_asset_id={"id": 2, "type": "source_asset"},
            video_transform_strength=0.8,
            num_diffusion_steps=30,
            seed=42,
            duration_seconds=8,
            resolution="1K",
        )
        assert dto.video_transform_mask_asset_id.id == 2
        assert dto.video_transform_strength == 0.8
        assert dto.seed == 42

        _process_vpe_transform_in_background(
            media_item_id=999,
            request_dto=dto,
            user_email="test@example.com",
        )

    def test_keyframe_transform_dto_and_worker(self, monkeypatch):
        from src.videos.dto.create_veo_dto import (
            ConditioningFrameDto,
            CreateVeoDto,
        )
        from src.videos.vpe_service import _process_vpe_transform_in_background

        monkeypatch.setattr(config_service, "VPE_ENABLED", False)

        dto = CreateVeoDto(
            prompt="Keyframe interpolation test",
            workspace_id=1,
            generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TRANSFORM,
            start_image_asset_id={"id": 1, "type": "source_asset"},
            end_image_asset_id={"id": 3, "type": "source_asset"},
            conditioning_frames=[
                ConditioningFrameDto(
                    frame_number=24,
                    image_asset_id={"id": 2, "type": "source_asset"},
                ),
                ConditioningFrameDto(
                    frame_number=72,
                    image_asset_id={"id": 4, "type": "source_asset"},
                ),
            ],
            video_transform_strength=0.6,
            num_diffusion_steps=25,
            seed=12345,
            duration_seconds=8,
            resolution="1K",
        )
        assert dto.start_image_asset_id.id == 1
        assert dto.end_image_asset_id.id == 3
        assert len(dto.conditioning_frames) == 2
        assert dto.conditioning_frames[0].frame_number == 24
        assert dto.conditioning_frames[1].frame_number == 72

        _process_vpe_transform_in_background(
            media_item_id=999,
            request_dto=dto,
            user_email="test@example.com",
        )


class TestWorkspaceGcsHelpers:
    """Tests for multi-project workspace GCS routing helpers in vpe_service."""

    def test_uri_bucket_name(self):
        assert _uri_bucket_name("gs://my-ws-bucket/path/clip.mp4") == (
            "my-ws-bucket"
        )
        assert _uri_bucket_name("gs://bucket-only") is None
        assert _uri_bucket_name("https://example.com/a.mp4") is None
        assert _uri_bucket_name("") is None

    @pytest.mark.anyio
    async def test_resolve_workspace_gcs_config_and_asset_ref(self):
        mock_ws = MagicMock()
        mock_ws.gcp_project_id = "custom-proj"
        mock_ws.gcs_bucket_name = "custom-bucket"

        with patch("src.videos.vpe_service.WorkspaceRepository") as mock_repo:
            mock_repo.return_value.get_by_id = AsyncMock(return_value=mock_ws)
            proj, bucket = await _resolve_workspace_gcs_config(
                MagicMock(), 42, logging.getLogger("test")
            )
            assert proj == "custom-proj"
            assert bucket == "custom-bucket"

        media_repo = MagicMock()
        parent = MagicMock()
        parent.gcs_uris = [
            "gs://custom-bucket/v0.mp4",
            "gs://custom-bucket/v1.mp4",
        ]
        media_repo.get_by_id = AsyncMock(return_value=parent)
        source_repo = MagicMock()

        ref_media = MagicMock(type="media_item", id=10, index=1)
        assert (
            await _resolve_asset_ref_uri(ref_media, media_repo, source_repo)
            == "gs://custom-bucket/v1.mp4"
        )


class TestGmpScreeningAndDualDelivery:
    """Tests for GMP Super-Res screening, ProRes/DNxHR dual delivery, and workers."""

    def test_check_super_res_upscalable_rules(self):
        from src.videos.vpe_service import check_super_res_upscalable

        # 4K input allowed ONLY with MODE_8K
        check_super_res_upscalable(
            _probe(width=3840, height=2160, frame_count=192), "MODE_8K"
        )
        with pytest.raises(VpeJobError, match="MODE_8K"):
            check_super_res_upscalable(
                _probe(width=3840, height=2160, frame_count=192), "MODE_4K"
            )

        # Cinema DCI 2048x1080 at 30fps passes MODE_4K_PRO
        check_super_res_upscalable(
            _probe(width=2048, height=1080, fps=30, frame_count=150),
            "MODE_4K_PRO",
        )

    @pytest.mark.anyio
    async def test_screen_and_start_upscale_job_for_4k_and_seamless(
        self, monkeypatch
    ):
        from fastapi import HTTPException

        _settings(
            monkeypatch,
            enabled=True,
            bucket="vpe-bucket",
            project_id="vpe-project",
        )
        media_repo = AsyncMock()
        row_4k = _row(duration_seconds=8.0, resolution="4K")
        row_4k.raw_data = {}
        media_repo.get_by_id = AsyncMock(return_value=row_4k)

        def _create_side_effect(item):
            item.id = 501
            return item

        media_repo.create = AsyncMock(side_effect=_create_side_effect)
        service = VpeService(
            media_repo=media_repo,
            source_asset_repo=AsyncMock(),
            gcs_service=AsyncMock(),
            iam_signer=MagicMock(),
        )
        screen_res = await service.screen_media_item(7)
        assert screen_res.offer is True
        assert screen_res.supported_modes == ("8k",)

        user = UserModel(
            id=1,
            email="user@example.com",
            name="Test User",
            roles=[UserRoleEnum.USER],
        )

        # Asking for 4k on a 4K clip is rejected with 400
        with pytest.raises(HTTPException) as exc_400:
            await service.start_upscale_job(
                request_dto=UpscaleVideoDto(
                    workspace_id=1, media_item_id=7, upscale_mode="4k"
                ),
                user=user,
                executor=Mock(),
            )
        assert exc_400.value.status_code == 400

        # Asking for 8k queues VEO_EXP_SUPER_RES
        executor = Mock()
        resp = await service.start_upscale_job(
            request_dto=UpscaleVideoDto(
                workspace_id=1,
                media_item_id=7,
                upscale_mode="8k",
                output_codec="prores",
                compression_quality="lossless",
            ),
            user=user,
            executor=executor,
        )
        assert resp.model == GenerationModelEnum.VEO_EXP_SUPER_RES
        assert executor.submit.called

    @pytest.mark.anyio
    async def test_deliver_gmp_output_dual_asset_prores_and_dnxhr(
        self, tmp_path
    ):
        from src.videos.vpe.client import VpeResult, VpeVideoOutput
        from src.videos.vpe_service import _deliver_gmp_output

        vpe_gcs = MagicMock()
        app_gcs = MagicMock()

        def _fake_download(gcs_uri_path, destination_file_path):
            _ = gcs_uri_path
            with open(destination_file_path, "wb") as handle:
                handle.write(b"fake-prores-bytes")
            return destination_file_path

        vpe_gcs.download_from_gcs.side_effect = _fake_download
        app_gcs.upload_file_to_gcs.side_effect = (
            lambda local_path, destination_blob_name, mime_type=None: (
                f"gs://ws-bucket/{destination_blob_name}"
            )
        )

        fake_result = VpeResult(
            operation_name="op-prores-1",
            videos=(
                VpeVideoOutput(
                    gcs_uri="gs://vpe-bucket/out/sample_0.mov",
                    mime_type="video/quicktime",
                ),
            ),
            rai_media_filtered_count=0,
            raw={},
        )

        def _fake_transcode(src, dst):
            _ = src
            dst.write_bytes(b"fake-h264-preview")
            return dst

        with (
            patch(
                "src.videos.vpe_service.transcode_to_h264_preview",
                side_effect=_fake_transcode,
            ) as mock_transcode,
            patch(
                "src.videos.vpe_service.generate_thumbnail",
                return_value=str(tmp_path / "thumb.png"),
            ),
            patch(
                "src.videos.vpe_service.build_measured_metadata",
                return_value={"resolution": "4K", "duration_seconds": 8.0},
            ),
        ):
            delivered = await _deliver_gmp_output(
                result=fake_result,
                output_codec="prores",
                compression_quality="lossless",
                dest_prefix="generated_videos/omni_cine_88",
                work=tmp_path,
                vpe_gcs=vpe_gcs,
                app_gcs=app_gcs,
                base_raw_data={"vpe_capability": "omni_cine"},
            )

        assert mock_transcode.called
        assert delivered["gcs_uris"] == [
            "gs://ws-bucket/generated_videos/omni_cine_88.mp4"
        ]
        assert delivered["raw_data"]["master_gcs_uris"] == [
            "gs://ws-bucket/generated_videos/omni_cine_88_master.mov"
        ]
        assert delivered["raw_data"]["output_codec"] == "prores"
        assert delivered["raw_data"]["compression_quality"] == "lossless"

    def test_gmp_workers_execute_end_to_end_with_mocked_infra(
        self, monkeypatch, tmp_path
    ):
        from contextlib import asynccontextmanager

        from src.videos.dto.create_veo_dto import CreateVeoDto
        from src.videos.vpe.client import (
            VpeOperation,
            VpeResult,
            VpeVideoOutput,
        )
        from src.videos.vpe_service import (
            _process_vpe_omni_cine_in_background,
            _process_vpe_omni_perf_in_background,
            _process_vpe_perf_estimation_in_background,
            _process_vpe_upscale_in_background,
            _process_vpe_video_textures_in_background,
        )

        _ = tmp_path
        _settings(
            monkeypatch,
            enabled=True,
            bucket="vpe-bucket",
            project_id="vpe-project",
        )

        updates: list[tuple[int, dict]] = []
        submitted_payloads: list[dict] = []

        @asynccontextmanager
        async def _fake_db_session():
            yield AsyncMock()

        @asynccontextmanager
        async def _fake_worker_db():
            yield _fake_db_session

        mock_media_repo = MagicMock()
        mock_media_repo.get_by_id = AsyncMock(
            return_value=MagicMock(
                workspace_id=1,
                gcs_uris=["gs://ws-bucket/in.mp4"],
                raw_data={},
            )
        )

        async def _fake_update(item_id, fields):
            updates.append((item_id, fields))

        mock_media_repo.update = AsyncMock(side_effect=_fake_update)

        mock_source_repo = MagicMock()
        mock_source_repo.get_by_id = AsyncMock(
            side_effect=lambda aid: MagicMock(
                gcs_uri=(
                    f"gs://ws-bucket/asset_{aid}.wav"
                    if aid == 99
                    else (
                        f"gs://ws-bucket/asset_{aid}.mp4"
                        if aid >= 50
                        else f"gs://ws-bucket/asset_{aid}.png"
                    )
                )
            )
        )

        async def _fake_download_asset(uri, local_path, *_args, **_kwargs):
            _ = uri
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"raw")
            return True

        mock_gcs = MagicMock()
        mock_gcs.upload_file_to_gcs.side_effect = (
            lambda local_path, destination_blob_name, mime_type=None: (
                f"gs://vpe-bucket/{destination_blob_name}"
            )
        )

        def _fake_dl(gcs_uri_path, destination_file_path):
            _ = gcs_uri_path
            with open(destination_file_path, "wb") as fh:
                fh.write(b"out")
            return destination_file_path

        mock_gcs.download_from_gcs.side_effect = _fake_dl

        mock_client = MagicMock()

        def _fake_submit(payload):
            submitted_payloads.append(payload)
            return VpeOperation.from_body(
                {
                    "name": f"operations/op-{len(submitted_payloads)}",
                    "done": True,
                    "response": {
                        "videos": [
                            {
                                "gcsUri": "gs://vpe-bucket/out/video.mov",
                                "mimeType": "video/quicktime",
                            }
                        ]
                    },
                }
            )

        mock_client.submit.side_effect = _fake_submit
        mock_client.wait_for_completion_sync.return_value = VpeResult(
            operation_name="op-done",
            videos=(
                VpeVideoOutput(
                    gcs_uri="gs://vpe-bucket/out/video.mov",
                    mime_type="video/quicktime",
                ),
            ),
            rai_media_filtered_count=0,
            raw={},
        )

        def _touch_target(_src, dst, *_args, **_kwargs):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"norm")
            return dst

        with (
            patch("src.videos.vpe_service.WorkerDatabase", _fake_worker_db),
            patch(
                "src.videos.vpe_service.MediaRepository",
                return_value=mock_media_repo,
            ),
            patch(
                "src.videos.vpe_service.SourceAssetRepository",
                return_value=mock_source_repo,
            ),
            patch(
                "src.videos.vpe_service._resolve_workspace_gcs_config",
                new=AsyncMock(return_value=("ws-proj", "ws-bucket")),
            ),
            patch("src.videos.vpe_service.GcsService", return_value=mock_gcs),
            patch("src.videos.vpe_service.VpeClient", return_value=mock_client),
            patch(
                "src.videos.vpe_service._download_asset_gcs",
                side_effect=_fake_download_asset,
            ),
            patch(
                "src.videos.vpe_service.normalize_video_720p_24fps",
                side_effect=_touch_target,
            ),
            patch(
                "src.videos.vpe_service.extract_audio_wav",
                side_effect=_touch_target,
            ),
            patch(
                "src.videos.vpe_service.transcode_to_h264_preview",
                side_effect=_touch_target,
            ),
            patch(
                "src.videos.vpe_service.has_audio_stream",
                return_value=True,
            ),
            patch(
                "src.videos.vpe_service.generate_thumbnail",
                return_value=None,
            ),
            patch(
                "src.videos.vpe_service.build_measured_metadata",
                return_value={"resolution": "1K", "duration_seconds": 8.0},
            ),
            patch(
                "src.videos.vpe_service.probe_media",
                return_value=_probe(width=1920, height=1080, frame_count=192),
            ),
        ):
            # 1. Perf Estimation worker
            _process_vpe_perf_estimation_in_background(
                101,
                CreateVeoDto(
                    prompt="",
                    workspace_id=1,
                    generation_model=GenerationModelEnum.VEO_EXP_PERF_ESTIMATION,
                    edit_source={"id": 50, "type": "source_asset"},
                ),
                "u@test.com",
            )
            # 2. Omni-Perf worker (2-step: perf-estimation -> omni-perf + audio extract)
            _process_vpe_omni_perf_in_background(
                102,
                CreateVeoDto(
                    prompt="Hero performance",
                    workspace_id=1,
                    generation_model=GenerationModelEnum.OMNI_PERF,
                    reference_images=[{"asset_id": 1}],
                    reference_video={"id": 51, "type": "source_asset"},
                    audio_control_mode="preserve_video_audio",
                    output_codec="prores",
                    compression_quality="lossless",
                ),
                "u@test.com",
            )
            # 3. Omni-Cine worker (videoTransform + mask + target_audio)
            _process_vpe_omni_cine_in_background(
                103,
                CreateVeoDto(
                    prompt="Cinematic rainstorm",
                    workspace_id=1,
                    generation_model=GenerationModelEnum.OMNI_CINE,
                    start_image_asset_id={"id": 1, "type": "source_asset"},
                    end_image_asset_id={"id": 2, "type": "source_asset"},
                    edit_source={"id": 52, "type": "source_asset"},
                    video_transform_mask_asset_id={
                        "id": 53,
                        "type": "source_asset",
                    },
                    reference_audio={"id": 99, "type": "source_asset"},
                    audio_control_mode="target_audio",
                    output_codec="prores",
                    compression_quality="lossless",
                ),
                "u@test.com",
            )
            # 4. Video Textures worker
            _process_vpe_video_textures_in_background(
                104,
                CreateVeoDto(
                    prompt="Seamless ocean waves",
                    workspace_id=1,
                    generation_model=GenerationModelEnum.VEO_EXP_VIDEO_TEXTURES,
                    seamless_loop=True,
                    tessellate_horizontal=True,
                    output_codec="prores",
                ),
                "u@test.com",
            )
            # 5. Upscale worker — SUPER_RES branch
            _process_vpe_upscale_in_background(
                105,
                "gs://ws-bucket/source.mp4",
                workspace_id=1,
                upscale_mode="4k_pro",
                steps=2,
                seed=7,
                output_codec="prores",
                compression_quality="lossless",
            )
            # 6. Upscale worker — UPSCALE_SEAMLESS branch (forces prores when h264)
            _process_vpe_upscale_in_background(
                106,
                "gs://ws-bucket/seamless_src.mp4",
                workspace_id=1,
                upscale_mode="4k",
                output_codec="h264",
                parent_raw_data={
                    "seamless": {
                        "loop": True,
                        "tessellate_horizontal": True,
                        "tessellate_vertical": False,
                    }
                },
            )

        completed_ids = [
            uid
            for uid, data in updates
            if str(data.get("status", "")).endswith("completed")
            or "COMPLETED" in str(data.get("status", ""))
        ]
        assert {101, 102, 103, 104, 105, 106} <= set(completed_ids)
        models_sent = [
            p["parameters"]["experiments"]["modelName"]
            for p in submitted_payloads
        ]
        assert "veo-exp-perf-estimation" in models_sent
        assert "omni-perf" in models_sent
        assert "omni-cine" in models_sent
        assert "veo-exp-video-textures" in models_sent
        assert "veo-exp-super-res" in models_sent
        assert "veo3p1_upscale" in models_sent
