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

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.common.base_dto import GenerationModelEnum
from src.common.schema.media_item_model import (
    MediaItemModel,
    MimeTypeEnum,
    JobStatusEnum,
)
from src.users.user_model import UserModel
from src.audios.dto.create_audio_dto import CreateAudioDto
from src.audios.audio_service import AudioService, _process_audio_in_background
from src.audios.audio_constants import LanguageEnum, VoiceEnum


@pytest.fixture(name="mock_media_repo")
def fixture_mock_media_repo():
    repo = AsyncMock()
    repo.create = AsyncMock()
    repo.update = AsyncMock()
    return repo


@pytest.fixture(name="audio_service")
def fixture_audio_service(mock_media_repo):
    return AudioService(
        media_repo=mock_media_repo,
        iam_signer_credentials=MagicMock(),
    )


@pytest.fixture(name="sample_user")
def fixture_sample_user():
    return UserModel(
        id=1, email="test@example.com", name="Test User", roles=["user"]
    )


@pytest.fixture(name="sample_create_lyria_dto")
def fixture_sample_create_lyria_dto():
    return CreateAudioDto(
        workspace_id=1,
        prompt="A cute cat running",
        model=GenerationModelEnum.LYRIA_002,
        sample_count=1,
    )


@pytest.fixture(name="sample_create_tts_dto")
def fixture_sample_create_tts_dto():
    return CreateAudioDto(
        workspace_id=1,
        prompt="Sample speech text",
        model=GenerationModelEnum.CHIRP_3,
        language_code=LanguageEnum.EN_US,
        voice_name=VoiceEnum.PUCK,
    )


class TestAudioServiceMethods:

    @pytest.mark.anyio
    async def test_start_audio_generation_job_success(
        self,
        audio_service,
        mock_media_repo,
        sample_create_lyria_dto,
        sample_user,
    ):
        placeholder = MediaItemModel(
            id=123,
            workspace_id=1,
            user_id=1,
            user_email="test@example.com",
            mime_type=MimeTypeEnum.AUDIO_WAV,
            model=GenerationModelEnum.LYRIA_002,
            aspect_ratio="16:9",
            gcs_uris=[],
            thumbnail_uris=[],
        )
        mock_media_repo.create.return_value = placeholder
        mock_executor = MagicMock()

        response = await audio_service.start_audio_generation_job(
            request_dto=sample_create_lyria_dto,
            user=sample_user,
            executor=mock_executor,
        )

        assert response is not None
        assert response.id == 123
        mock_media_repo.create.assert_called_once()
        mock_executor.submit.assert_called_once()


class TestBackgroundWorkers:

    @patch("src.database.WorkerDatabase")
    @patch("src.audios.audio_service.MediaRepository")
    @patch("src.audios.audio_service.aiplatform.gapic.PredictionServiceClient")
    @patch("src.audios.audio_service.GcsService")
    def test_process_lyria_in_background_sync(
        self,
        mock_gcs,
        mock_aiplatform,
        mock_repo_cls,
        mock_worker_db,
        sample_create_lyria_dto,
        sample_user,
    ):
        mock_db_factory = MagicMock()
        mock_worker_db.return_value.__aenter__.return_value = mock_db_factory
        mock_db_session = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db_session

        mock_repo = AsyncMock()
        mock_repo_cls.return_value = mock_repo

        mock_gcs_singleton = MagicMock()
        mock_gcs_singleton.store_to_gcs.return_value = "gs://foo/bar.wav"
        mock_gcs.return_value = mock_gcs_singleton

        mock_ai_instance = MagicMock()
        mock_ai_instance.predict.return_value = MagicMock(
            predictions=[{"bytesBase64Encoded": "SGVsbG8="}]
        )
        mock_aiplatform.return_value = mock_ai_instance

        _process_audio_in_background(
            media_item_id=123,
            request_dto=sample_create_lyria_dto,
            user_email=sample_user.email,
            user_id=sample_user.id,
        )

        mock_repo.update.assert_called_with(
            123,
            {
                "status": JobStatusEnum.COMPLETED,
                "gcs_uris": ["gs://foo/bar.wav"],
                "generation_time": pytest.approx(
                    0, abs=10.0
                ),  # loose assertion
            },
        )

    @patch("src.database.WorkerDatabase")
    @patch("src.audios.audio_service.MediaRepository")
    @patch("src.audios.audio_service.texttospeech.TextToSpeechClient")
    @patch("src.audios.audio_service.GcsService")
    def test_process_tts_in_background_sync(
        self,
        mock_gcs,
        mock_tts_client,
        mock_repo_cls,
        mock_worker_db,
        sample_create_tts_dto,
        sample_user,
    ):
        mock_db_factory = MagicMock()
        mock_worker_db.return_value.__aenter__.return_value = mock_db_factory
        mock_db_session = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db_session

        mock_repo = AsyncMock()
        mock_repo_cls.return_value = mock_repo

        mock_gcs_singleton = MagicMock()
        mock_gcs_singleton.store_to_gcs.return_value = "gs://foo/tts.wav"
        mock_gcs.return_value = mock_gcs_singleton

        mock_tts_instance = MagicMock()
        mock_tts_instance.synthesize_speech.return_value = MagicMock(
            audio_content=b"123"
        )
        mock_tts_client.return_value = mock_tts_instance

        _process_audio_in_background(
            media_item_id=125,
            request_dto=sample_create_tts_dto,
            user_email=sample_user.email,
            user_id=sample_user.id,
        )

        mock_repo.update.assert_called_with(
            125,
            {
                "status": JobStatusEnum.COMPLETED,
                "gcs_uris": ["gs://foo/tts.wav"],
                "generation_time": pytest.approx(0, abs=10.0),
            },
        )

    @patch("src.database.WorkerDatabase")
    @patch("src.audios.audio_service.MediaRepository")
    @patch("src.audios.audio_service.GenAIModelSetup")
    @patch("src.audios.audio_service.GcsService")
    def test_process_gemini_in_background_sync(
        self,
        mock_gcs,
        mock_genai,
        mock_repo_cls,
        mock_worker_db,
        sample_user,
    ):
        gemini_dto = CreateAudioDto(
            workspace_id=1,
            prompt="Gemini prompt",
            model=GenerationModelEnum.GEMINI_2_5_FLASH_TTS,
            sample_count=1,
            language_code=LanguageEnum.EN_US,
            voice_name=VoiceEnum.AOEDE,
        )

        mock_db_factory = MagicMock()
        mock_worker_db.return_value.__aenter__.return_value = mock_db_factory
        mock_db_session = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db_session

        mock_repo = AsyncMock()
        mock_repo_cls.return_value = mock_repo

        mock_gcs_singleton = MagicMock()
        mock_gcs_singleton.store_to_gcs.return_value = "gs://foo/gemini.wav"
        mock_gcs.return_value = mock_gcs_singleton

        mock_client = MagicMock()
        mock_content = MagicMock()
        mock_part = MagicMock()
        mock_part.inline_data = MagicMock()
        mock_part.inline_data.data = "SGVsbG8="  # Base64
        mock_content.parts = [mock_part]

        mock_candidate = MagicMock()
        mock_candidate.content = mock_content
        mock_client.models.generate_content.return_value = MagicMock(
            candidates=[mock_candidate]
        )
        mock_genai.init.return_value = mock_client

        _process_audio_in_background(
            media_item_id=126,
            request_dto=gemini_dto,
            user_email=sample_user.email,
            user_id=sample_user.id,
        )

        mock_repo.update.assert_called_with(
            126,
            {
                "status": JobStatusEnum.COMPLETED,
                "gcs_uris": ["gs://foo/gemini.wav"],
                "generation_time": pytest.approx(0, abs=10.0),
            },
        )
        mock_client.models.generate_content.assert_called_once()
        _, call_kwargs = mock_client.models.generate_content.call_args
        called_config = call_kwargs.get("config")
        assert called_config is not None
        assert (
            called_config.speech_config.voice_config.prebuilt_voice_config.voice_name
            == "Aoede"
        )

    @patch("src.database.WorkerDatabase")
    @patch("src.audios.audio_service.MediaRepository")
    @patch("src.audios.audio_service.GenAIModelSetup")
    @patch("src.audios.audio_service.GcsService")
    def test_process_lyria3_mp3_and_content_blocked_retry_sync(
        self,
        mock_gcs,
        mock_genai,
        mock_repo_cls,
        mock_worker_db,
        sample_user,
    ):
        lyria3_dto = CreateAudioDto(
            workspace_id=1,
            prompt="Upbeat synthwave track featuring neon city chase scenes",
            negative_prompt="vocals, distortion",
            model=GenerationModelEnum.LYRIA_3_CLIP_PREVIEW,
            sample_count=1,
        )

        mock_db_factory = MagicMock()
        mock_worker_db.return_value.__aenter__.return_value = mock_db_factory
        mock_db_session = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db_session

        mock_repo = AsyncMock()
        mock_repo_cls.return_value = mock_repo

        mock_gcs_singleton = MagicMock()
        mock_gcs_singleton.store_to_gcs.return_value = "gs://foo/lyria3.mp3"
        mock_gcs.return_value = mock_gcs_singleton

        blocked_err = RuntimeError("400 content_blocked policy filter")
        ok_interaction = {
            "outputs": [
                {
                    "type": "audio",
                    "mime_type": "audio/mp3",
                    "data": "SGVsbG8=",
                }
            ]
        }
        mock_omni_client = MagicMock()
        mock_omni_client.interactions.create.side_effect = [
            blocked_err,
            ok_interaction,
        ]
        mock_genai.get_omni_client.return_value = mock_omni_client

        _process_audio_in_background(
            media_item_id=127,
            request_dto=lyria3_dto,
            user_email=sample_user.email,
            user_id=sample_user.id,
        )

        assert mock_omni_client.interactions.create.call_count == 2
        update_args = mock_repo.update.call_args.args
        assert update_args[0] == 127
        assert update_args[1]["status"] == JobStatusEnum.COMPLETED
        assert update_args[1]["mime_type"] == MimeTypeEnum.AUDIO_MPEG
        assert "Instrumental background music" in update_args[1]["prompt"]

    @patch("src.database.WorkerDatabase")
    @patch("src.audios.audio_service.MediaRepository")
    @patch("src.audios.audio_service.GenAIModelSetup")
    @patch("src.audios.audio_service.GcsService")
    def test_process_lyria3_double_content_blocked_marks_failed(
        self,
        mock_gcs,
        mock_genai,
        mock_repo_cls,
        mock_worker_db,
        sample_user,
    ):
        lyria3_pro_dto = CreateAudioDto(
            workspace_id=1,
            prompt="Cinematic orchestral score with dramatic battle drums",
            model=GenerationModelEnum.LYRIA_3_PRO_PREVIEW,
            sample_count=1,
        )

        mock_db_factory = MagicMock()
        mock_worker_db.return_value.__aenter__.return_value = mock_db_factory
        mock_db_session = AsyncMock()
        mock_db_factory.return_value.__aenter__.return_value = mock_db_session

        mock_repo = AsyncMock()
        mock_repo_cls.return_value = mock_repo
        mock_gcs.return_value = MagicMock()

        mock_omni_client = MagicMock()
        mock_omni_client.interactions.create.side_effect = RuntimeError(
            "content_blocked"
        )
        mock_genai.get_omni_client.return_value = mock_omni_client

        _process_audio_in_background(
            media_item_id=128,
            request_dto=lyria3_pro_dto,
            user_email=sample_user.email,
            user_id=sample_user.id,
        )

        update_args = mock_repo.update.call_args.args
        assert update_args[0] == 128
        assert update_args[1]["status"] == JobStatusEnum.FAILED
        assert "content_blocked" in update_args[1]["error_message"]


def test_lyria3_helpers_and_gemini_3_1_tts_dto():
    from src.audios.audio_service import (
        _build_lyria3_prompt,
        _extract_lyria3_audio,
        _is_lyria3_content_blocked,
        _simplify_lyria3_prompt,
    )

    tts_dto = CreateAudioDto(
        workspace_id=1,
        prompt="Hello world",
        model=GenerationModelEnum.GEMINI_3_1_FLASH_TTS_PREVIEW,
        language_code=LanguageEnum.EN_US,
        voice_name=VoiceEnum.PUCK,
    )
    assert tts_dto.model == GenerationModelEnum.GEMINI_3_1_FLASH_TTS_PREVIEW
    assert tts_dto.model in AudioService.GEMINI_MODELS

    music_dto = CreateAudioDto(
        workspace_id=1,
        prompt="Warm acoustic guitar",
        negative_prompt="drums",
        model=GenerationModelEnum.LYRIA_3_CLIP_PREVIEW,
    )
    assert "Avoid: drums" in _build_lyria3_prompt(music_dto)
    simplified = _simplify_lyria3_prompt(
        "Warm acoustic guitar featuring dramatic thunderstorm sound effects."
    )
    assert simplified.startswith("Warm acoustic guitar.")
    assert _simplify_lyria3_prompt("   ... ") == ""

    err = Exception("other")
    err.code = "content_blocked"
    assert _is_lyria3_content_blocked(err) is True

    wav_bytes, wav_mime = _extract_lyria3_audio(
        {
            "steps": [
                {
                    "content": [
                        {"type": "text", "text": "thinking"},
                        {
                            "type": "audio",
                            "mime_type": "audio/wav",
                            "data": b"RIFFDATA",
                        },
                    ]
                }
            ]
        }
    )
    assert wav_bytes == b"RIFFDATA"
    assert wav_mime == "audio/wav"

    none_bytes, default_mime = _extract_lyria3_audio({"outputs": []})
    assert none_bytes is None
    assert default_mime == "audio/mpeg"
