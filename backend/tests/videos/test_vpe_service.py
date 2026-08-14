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
from unittest.mock import AsyncMock, Mock

import pytest

from src.common.base_dto import MimeTypeEnum
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
