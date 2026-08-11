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
"""Tests for the VPE REST transport.

No test here touches the network, and none could usefully be run against the
live API: the program is allowlist-gated and this project is not on the
allowlist. What is checkable is that the client sends exactly what the docs'
curl samples send, and that it understands exactly what the docs say comes
back - so every response body below is transcribed from the documentation
rather than invented.
"""

import asyncio
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from src.config.config_service import config_service
from src.videos.vpe import client as client_module
from src.videos.vpe.client import (
    DRY_RUN_MARKER,
    VPE_FETCH_METHOD,
    VPE_PREDICT_METHOD,
    VPE_REQUEST_TYPE_HEADER,
    VPE_REQUEST_TYPE_SHARED,
    VpeClient,
    VpeOperation,
    parse_result,
)
from src.videos.vpe.errors import (
    PreflightDeviation,
    PreflightSnapshot,
    VpeApiError,
    VpeAspectRatioMismatchError,
    VpeAuthenticationError,
    VpeBucketPermissionError,
    VpeConfigurationError,
    VpeFrameRateError,
    VpeInputRejectedError,
    VpeInvalidPayloadError,
    VpeMisreportedInputError,
    VpeMissingOperationError,
    VpeMissingOutputError,
    VpeServiceOverloadedError,
    VpeTimeoutError,
    VpeTransportError,
    VpeUnsupportedResolutionError,
    VpeVideoTooLongError,
    VpeVideoTooShortError,
    classify_error,
)

# --- Documented response bodies -------------------------------------------
#
# Transcribed from the VPE user guide's troubleshooting section and from the
# dialogue-driven generation page's polling samples. The doc renderer clips
# every code block at the right margin, so several strings end mid-word:
# "Expected OAuth 2 acce", "does n", "the width 12", "cannot process yo".
# They are reproduced exactly as printed, clipping included, because a
# classifier that only works on a tidied-up reconstruction of these messages
# has not been shown to work on anything the service actually sends.

# Clipped in the source the same way; used verbatim so the operation name
# carried on an error is proved to survive classification.
DOC_OPERATION_NAME = (
    "projects/${PROJECT_ID}/locations/us-central1/publishers/google/models"
    "/veo-"
)
DOC_A2V_OPERATION_NAME = (
    "projects/PROJECT_ID/locations/us-central1/publishers/google/models"
    "/veo-exp"
)

AUTH_ERROR_BODY = {
    "error": {
        "code": 401,
        "message": (
            "Request had invalid authentication credentials. Expected OAuth"
            " 2 acce"
        ),
        "status": "UNAUTHENTICATED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCESS_TOKEN_TYPE_UNSUPPORTED",
                "metadata": {
                    "service": "aiplatform.googleapis.com",
                    "method": (
                        "google.cloud.aiplatform.v1.PredictionService"
                        ".PredictLongRunning"
                    ),
                },
            },
        ],
    },
}

BUCKET_PERMISSION_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {
        "code": 7,
        "message": (
            "service-289693906766@gcp-sa-aiplatform.iam.gserviceaccount.com"
            " does n"
        ),
    },
}

VIDEO_TOO_LONG_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {
        "code": 3,
        "message": (
            "Video duration 15 seconds exceeds the maximum duration 8"
            " seconds."
        ),
    },
}

VIDEO_TOO_SHORT_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {
        "code": 3,
        "message": (
            "Video duration 3 seconds is less than the minimum duration 4"
            " seconds."
        ),
    },
}

FPS_MISMATCH_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {
        "code": 3,
        "message": "Video fps mismatch. Expected: 24 got: 60.",
    },
}

ASPECT_RATIO_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {
        "code": 3,
        "message": (
            "The request aspect ratio ASPECT_RATIO_9_16 doesn't match the"
            " width 12"
        ),
    },
}

UNSUPPORTED_WIDTH_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {"code": 3, "message": "Unsupported video width 854"},
}

HIGH_LOAD_BODY = {
    "name": DOC_OPERATION_NAME,
    "done": True,
    "error": {
        "code": 8,
        "message": (
            "The service is currently experiencing high load and cannot"
            " process yo"
        ),
    },
}

ALL_DOCUMENTED_ERROR_BODIES = (
    AUTH_ERROR_BODY,
    BUCKET_PERMISSION_BODY,
    VIDEO_TOO_LONG_BODY,
    VIDEO_TOO_SHORT_BODY,
    FPS_MISMATCH_BODY,
    ASPECT_RATIO_BODY,
    UNSUPPORTED_WIDTH_BODY,
    HIGH_LOAD_BODY,
)

# Dialogue-driven generation, "Poll for completion": an operation still
# running answers with the name alone - no "done" key at all, which is why
# the client derives done rather than reading it.
IN_PROGRESS_BODY = {"name": DOC_A2V_OPERATION_NAME}

SUCCESS_BODY = {
    "name": DOC_A2V_OPERATION_NAME,
    "done": True,
    "response": {
        "@type": (
            "type.googleapis.com/cloud.ai.large_models.vision"
            ".GenerateVideoResponse"
        ),
        "raiMediaFilteredCount": 0,
        "videos": [
            {
                "gcsUri": "gs://BUCKET_NAME/outputs/sample_0.mp4",
                "mimeType": "video/mp4",
            },
        ],
    },
}

SAMPLE_PAYLOAD = {
    "parameters": {
        "experiments": {"modelName": "veo-exp-a2v-generation"},
        "storageUri": "gs://my-bucket/outputs/",
    },
    "instances": [
        {
            "prompt": "A person speaking passionately about shampoo",
            "image": {
                "gcsUri": "gs://my-bucket/inputs/first_frame.png",
                "mimeType": "image/png",
            },
            "referenceAudios": [
                {
                    "audio": {
                        "gcsUri": "gs://my-bucket/inputs/dialogue.wav",
                        "mimeType": "audio/wav",
                    },
                },
            ],
        },
    ],
}

FPS_DEVIATION = PreflightSnapshot(
    source_uri="gs://my-bucket/inputs/clip.mp4",
    deviations=(
        PreflightDeviation(field="frame rate", expected="24", actual="60"),
    ),
)
CLEAN_PREFLIGHT = PreflightSnapshot(source_uri="gs://my-bucket/in/clip.mp4")


# --- Test doubles ----------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, body=None, *, status_code=200, text="", bad_json=False):
        """Initialises the response."""
        self.status_code = status_code
        self.text = text or json.dumps(body if body is not None else {})
        self._body = body
        self._bad_json = bad_json

    def json(self):
        """Returns the parsed body, or raises like a bad JSON payload."""
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeSession:
    """Records POSTs and replays a scripted list of responses."""

    def __init__(self, responses):
        """Initialises the session with the responses to hand back."""
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json, headers, timeout):
        """Records the call and returns (or raises) the next response."""
        # pylint: disable=redefined-outer-name
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            },
        )
        if not self.responses:
            raise AssertionError(f"Unexpected extra POST to {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class ExplodingSession:
    """A session that fails the test if anything is sent through it."""

    def post(self, *args, **kwargs):
        """Fails immediately."""
        raise AssertionError("A request was sent when none was expected")


class FakeClock:
    """Stands in for the time module inside the client's namespace."""

    def __init__(self):
        """Starts the clock at zero."""
        self.now = 0.0

    def monotonic(self):
        """Returns the current fake time."""
        return self.now


class FakeAsyncio:
    """Stands in for the asyncio module inside the client's namespace.

    Only ``sleep`` is faked - it records the delay and advances the fake
    clock instead of waiting - so a half-hour timeout is testable in
    microseconds. ``to_thread`` delegates to the real one so the client's
    blocking-call offloading is still exercised rather than bypassed.
    """

    def __init__(self, clock):
        """Initialises the double against a clock to advance."""
        self.clock = clock
        self.sleeps = []

    async def sleep(self, seconds):
        """Records the delay and advances the fake clock."""
        self.sleeps.append(seconds)
        self.clock.now += seconds

    @staticmethod
    async def to_thread(func, *args, **kwargs):
        """Delegates to the real asyncio.to_thread."""
        return await asyncio.to_thread(func, *args, **kwargs)


@pytest.fixture(name="fake_time")
def fixture_fake_time(monkeypatch):
    """Replaces time and asyncio inside the client module only.

    Patching the real ``time.monotonic`` would also move the event loop's
    own clock, so the doubles are installed in the client's namespace and
    nowhere else.
    """
    clock = FakeClock()
    fake_asyncio = FakeAsyncio(clock)
    monkeypatch.setattr(client_module, "time", clock)
    monkeypatch.setattr(client_module, "asyncio", fake_asyncio)
    return fake_asyncio


def make_client(session=None, **kwargs):
    """Builds a client with a fake session and no live configuration."""
    kwargs.setdefault("project_id", "test-project")
    kwargs.setdefault("location", "us-central1")
    return VpeClient(
        session=session if session is not None else FakeSession([]),
        **kwargs,
    )


# --- Error classification: one test per documented body --------------------


def test_invalid_authentication_credentials_is_classified():
    """The 401 body maps to an auth error carrying its reason."""
    error = classify_error(AUTH_ERROR_BODY)

    assert isinstance(error, VpeAuthenticationError)
    assert error.code == 401
    assert error.status == "UNAUTHENTICATED"
    assert error.reason == "ACCESS_TOKEN_TYPE_UNSUPPORTED"
    assert not error.retryable
    assert "application-default" in error.remedy


def test_bucket_permission_error_is_classified_from_code_seven():
    """The permission body is clipped, so only the code identifies it."""
    error = classify_error(BUCKET_PERMISSION_BODY)

    assert isinstance(error, VpeBucketPermissionError)
    assert error.code == 7
    assert error.operation_name == DOC_OPERATION_NAME
    assert "allowlisted project" in error.remedy
    assert not error.retryable


def test_video_too_long_captures_both_durations():
    """The measured and permitted durations are pulled out of the text."""
    error = classify_error(VIDEO_TOO_LONG_BODY)

    assert isinstance(error, VpeVideoTooLongError)
    assert isinstance(error, VpeInputRejectedError)
    assert error.actual_seconds == 15.0
    assert error.limit_seconds == 8.0
    assert error.remedy == (
        "Shorten or chunk the video to be less than 8 seconds."
    )


def test_video_too_short_captures_both_durations():
    """The minimum-duration rejection is distinguished from the maximum."""
    error = classify_error(VIDEO_TOO_SHORT_BODY)

    assert isinstance(error, VpeVideoTooShortError)
    assert error.actual_seconds == 3.0
    assert error.limit_seconds == 4.0
    assert "minimum of 4 seconds" in error.remedy


def test_frame_rate_mismatch_captures_expected_and_actual():
    """A wrong fps reports both numbers rather than INVALID_ARGUMENT."""
    error = classify_error(FPS_MISMATCH_BODY)

    assert isinstance(error, VpeFrameRateError)
    assert error.expected_fps == 24
    assert error.actual_fps == 60
    assert error.remedy == "Change the video frames per second to exactly 24."


def test_aspect_ratio_mismatch_captures_the_requested_ratio():
    """The declared ratio is recovered from the clipped literal message."""
    error = classify_error(ASPECT_RATIO_BODY)

    assert isinstance(error, VpeAspectRatioMismatchError)
    assert error.requested_aspect_ratio == "ASPECT_RATIO_9_16"
    assert "Match the aspect ratio" in error.remedy


def test_aspect_ratio_mismatch_captures_width_when_not_clipped():
    """The same rule reads the real width off an unclipped message.

    The doc's copy stops at "the width 12", so this is the same message with
    the sentence the service would actually have sent finished off.
    """
    body = {
        "name": DOC_OPERATION_NAME,
        "done": True,
        "error": {
            "code": 3,
            "message": (
                "The request aspect ratio ASPECT_RATIO_9_16 doesn't match"
                " the width 1280 and height 720."
            ),
        },
    }

    error = classify_error(body)

    assert isinstance(error, VpeAspectRatioMismatchError)
    assert error.actual_width == 1280


def test_unsupported_width_is_classified():
    """A sub-720p input is reported as a resolution problem."""
    error = classify_error(UNSUPPORTED_WIDTH_BODY)

    assert isinstance(error, VpeUnsupportedResolutionError)
    assert error.actual_width == 854
    assert error.remedy == "Rightsize the input video file to 720p."


def test_high_load_is_retryable_when_nothing_is_known_about_the_input():
    """Without a preflight snapshot, high load is taken at face value."""
    error = classify_error(HIGH_LOAD_BODY)

    assert isinstance(error, VpeServiceOverloadedError)
    assert error.code == 8
    assert error.retryable
    assert "Wait a few minutes" in error.remedy


def test_high_load_stays_retryable_when_preflight_found_nothing():
    """A clean preflight makes a capacity error believable as one."""
    error = classify_error(HIGH_LOAD_BODY, preflight=CLEAN_PREFLIGHT)

    assert isinstance(error, VpeServiceOverloadedError)
    assert error.retryable


def test_high_load_on_an_off_spec_input_reports_the_input():
    """The documented misreporting defect is named, not retried.

    The user guide records that a wrong input fps came back as a high-load
    message. Preflight already measured 60 fps here, so that is the reported
    cause and the error is not retryable.
    """
    error = classify_error(HIGH_LOAD_BODY, preflight=FPS_DEVIATION)

    assert isinstance(error, VpeMisreportedInputError)
    assert isinstance(error, VpeInputRejectedError)
    assert not isinstance(error, VpeServiceOverloadedError)
    assert not error.retryable
    assert error.preflight is FPS_DEVIATION
    assert "frame rate should be 24 but is 60" in str(error)
    assert "Fix the input before retrying." in str(error)


def test_preflight_does_not_distort_an_unambiguous_error():
    """A snapshot only ever reinterprets high load, never anything else."""
    error = classify_error(FPS_MISMATCH_BODY, preflight=FPS_DEVIATION)

    assert isinstance(error, VpeFrameRateError)


def test_unrecognised_invalid_argument_still_says_something_useful():
    """An undocumented code 3 keeps the generic input remedy."""
    body = {
        "name": DOC_OPERATION_NAME,
        "done": True,
        "error": {"code": 3, "message": "Something new went wrong."},
    }

    error = classify_error(body)

    assert type(error) is VpeInputRejectedError  # pylint: disable=C0123
    assert "frame rate" in error.remedy


def test_completely_unknown_error_falls_back_to_the_base_type():
    """An unmatched body is still typed, just without a remedy."""
    body = {"error": {"code": 13, "message": "Internal error."}}

    error = classify_error(body)

    assert type(error) is VpeApiError  # pylint: disable=C0123
    assert error.code == 13
    assert not error.retryable


def test_http_status_stands_in_for_a_missing_code():
    """A body with no code is classified from the transport status."""
    error = classify_error({"error": {"message": "nope"}}, http_status=401)

    assert isinstance(error, VpeAuthenticationError)


@pytest.mark.parametrize("body", ALL_DOCUMENTED_ERROR_BODIES)
def test_every_documented_body_yields_an_actionable_message(body):
    """No documented failure reaches a user as a bare status code."""
    error = classify_error(body)

    assert error.remedy
    assert error.message in str(error)
    assert error.remedy in str(error)


def test_preflight_snapshot_summarises_its_deviations():
    """The snapshot renders phrases meant to be read in a sentence."""
    snapshot = PreflightSnapshot(
        deviations=(
            PreflightDeviation("frame rate", "24", "60"),
            PreflightDeviation("frame size", "1280x720", "854x480"),
        ),
    )

    assert snapshot.has_deviations
    assert snapshot.summary() == (
        "frame rate should be 24 but is 60,"
        " frame size should be 1280x720 but is 854x480"
    )
    assert not PreflightSnapshot().has_deviations


# --- Parsing the documented polling responses ------------------------------


def test_in_progress_poll_response_is_not_done():
    """The in-progress body has no done key at all."""
    operation = VpeOperation.from_body(IN_PROGRESS_BODY)

    assert operation.name == DOC_A2V_OPERATION_NAME
    assert not operation.done
    assert not operation.has_error


def test_success_response_exposes_the_preview_and_its_folder():
    """The documented success body yields the URI and its directory."""
    operation = VpeOperation.from_body(SUCCESS_BODY)
    result = parse_result(operation)

    assert operation.done
    assert result.operation_name == DOC_A2V_OPERATION_NAME
    assert result.rai_media_filtered_count == 0
    assert len(result.videos) == 1
    assert result.primary_video.gcs_uri == (
        "gs://BUCKET_NAME/outputs/sample_0.mp4"
    )
    assert result.primary_video.mime_type == "video/mp4"
    # The frame sequence, the audio stem and the provenance files all live
    # beside the preview; the response never names them.
    assert result.output_directory_uri == "gs://BUCKET_NAME/outputs"
    assert result.raw is operation.raw


def test_parse_result_refuses_an_unfinished_operation():
    """Reading outputs off a running job is a programming error."""
    operation = VpeOperation.from_body(IN_PROGRESS_BODY)

    with pytest.raises(ValueError, match="has not finished"):
        parse_result(operation)


def test_parse_result_classifies_an_operation_that_failed():
    """A failed operation raises the typed error, not a generic one."""
    operation = VpeOperation.from_body(FPS_MISMATCH_BODY)

    with pytest.raises(VpeFrameRateError) as raised:
        parse_result(operation)

    assert raised.value.actual_fps == 60


def test_parse_result_applies_the_preflight_snapshot():
    """The snapshot reaches classification through the parse path too."""
    operation = VpeOperation.from_body(HIGH_LOAD_BODY)

    with pytest.raises(VpeMisreportedInputError):
        parse_result(operation, preflight=FPS_DEVIATION)


def test_parse_result_reports_a_completed_job_with_no_output():
    """The documented 4K 16-bit PNG defect is named, not swallowed."""
    operation = VpeOperation.from_body(
        {
            "name": DOC_A2V_OPERATION_NAME,
            "done": True,
            "response": {"raiMediaFilteredCount": 0, "videos": []},
        },
    )

    with pytest.raises(VpeMissingOutputError, match="storageUri"):
        parse_result(operation)


def test_parse_result_ignores_a_video_entry_with_no_uri():
    """An entry without gcsUri is no output at all."""
    operation = VpeOperation.from_body(
        {
            "name": DOC_A2V_OPERATION_NAME,
            "done": True,
            "response": {"videos": [{"mimeType": "video/mp4"}]},
        },
    )

    with pytest.raises(VpeMissingOutputError):
        parse_result(operation)


def test_operation_id_is_the_trailing_segment():
    """Support asks for the id, not the whole resource name."""
    operation = VpeOperation.from_body({"name": "projects/p/o/operations/xyz"})

    assert operation.operation_id == "xyz"


# --- Submitting ------------------------------------------------------------


def test_submit_sends_the_documented_request():
    """URL, headers and body match the docs' curl sample exactly."""
    session = FakeSession([FakeResponse({"name": DOC_A2V_OPERATION_NAME})])
    client = make_client(session)

    operation = client.submit(SAMPLE_PAYLOAD)

    assert operation.name == DOC_A2V_OPERATION_NAME
    assert not operation.done
    call = session.calls[0]
    assert call["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects"
        "/test-project/locations/us-central1/publishers/google/models"
        "/veo-experimental:predictLongRunning"
    )
    assert call["json"] == SAMPLE_PAYLOAD
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["timeout"] == client_module.HTTP_TIMEOUT_SECONDS


def test_both_endpoints_send_the_shared_request_type_header():
    """Without this header the experimental checkpoints are unreachable."""
    session = FakeSession(
        [
            FakeResponse({"name": DOC_A2V_OPERATION_NAME}),
            FakeResponse(IN_PROGRESS_BODY),
        ],
    )
    client = make_client(session)

    client.submit(SAMPLE_PAYLOAD)
    client.poll(DOC_A2V_OPERATION_NAME)

    assert len(session.calls) == 2
    for call in session.calls:
        assert (
            call["headers"][VPE_REQUEST_TYPE_HEADER] == VPE_REQUEST_TYPE_SHARED
        )


def test_submit_rejects_a_payload_with_no_model_name():
    """The shared endpoint cannot route a body without a checkpoint."""
    client = make_client(ExplodingSession())

    with pytest.raises(VpeInvalidPayloadError, match="modelName"):
        client.submit(
            {"instances": [{}], "parameters": {"storageUri": "gs://b/o/"}},
        )


def test_submit_rejects_a_payload_with_no_instances():
    """An empty instances array never reaches the wire."""
    client = make_client(ExplodingSession())

    with pytest.raises(VpeInvalidPayloadError, match="instances"):
        client.submit(
            {
                "instances": [],
                "parameters": {"experiments": {"modelName": "omni-cine"}},
            },
        )


def test_submit_raises_the_typed_error_for_a_rejected_request():
    """A 401 at submission is an auth error, not a transport failure."""
    session = FakeSession(
        [FakeResponse(AUTH_ERROR_BODY, status_code=401)],
    )
    client = make_client(session)

    with pytest.raises(VpeAuthenticationError):
        client.submit(SAMPLE_PAYLOAD)


def test_submit_reports_a_response_with_no_operation_name():
    """The documented 4K 16-bit PNG defect leaves nothing to poll."""
    session = FakeSession([FakeResponse({})])
    client = make_client(session)

    with pytest.raises(VpeMissingOperationError, match="no operation name"):
        client.submit(SAMPLE_PAYLOAD)


def test_a_non_json_body_is_a_transport_error():
    """An HTML error page is not a VPE opinion about the request."""
    session = FakeSession(
        [
            FakeResponse(
                status_code=502,
                text="<html>502 Bad Gateway</html>",
                bad_json=True,
            ),
        ],
    )
    client = make_client(session)

    with pytest.raises(VpeTransportError) as raised:
        client.submit(SAMPLE_PAYLOAD)

    assert raised.value.status_code == 502
    assert "502 Bad Gateway" in raised.value.body
    assert raised.value.retryable


def test_a_connection_failure_is_a_transport_error():
    """Anything the socket layer raises becomes one retryable type."""
    session = FakeSession([ConnectionResetError("connection reset by peer")])
    client = make_client(session)

    with pytest.raises(VpeTransportError, match="connection reset"):
        client.submit(SAMPLE_PAYLOAD)


def test_an_http_error_without_an_envelope_is_a_transport_error():
    """A 500 with a JSON body but no error object is not classifiable."""
    session = FakeSession([FakeResponse({"foo": "bar"}, status_code=500)])
    client = make_client(session)

    with pytest.raises(VpeTransportError, match="HTTP 500"):
        client.submit(SAMPLE_PAYLOAD)


def test_a_json_body_that_is_not_an_object_is_a_transport_error():
    """A bare list is not an operation, whatever the status says."""
    session = FakeSession([FakeResponse([1, 2, 3])])
    client = make_client(session)

    with pytest.raises(VpeTransportError, match="not an object"):
        client.submit(SAMPLE_PAYLOAD)


# --- Polling ---------------------------------------------------------------


def test_poll_posts_the_operation_name_to_the_fetch_endpoint():
    """fetchPredictOperation is a POST with the name in the body."""
    session = FakeSession([FakeResponse(IN_PROGRESS_BODY)])
    client = make_client(session)

    operation = client.poll(DOC_A2V_OPERATION_NAME)

    call = session.calls[0]
    assert call["url"].endswith(f":{VPE_FETCH_METHOD}")
    assert VPE_PREDICT_METHOD not in call["url"]
    assert call["json"] == {"operationName": DOC_A2V_OPERATION_NAME}
    assert not operation.done


def test_poll_returns_a_failed_operation_rather_than_raising():
    """A failed job is a valid answer to the fetch call, over HTTP 200."""
    session = FakeSession([FakeResponse(HIGH_LOAD_BODY)])
    client = make_client(session)

    operation = client.poll(DOC_OPERATION_NAME)

    assert operation.done
    assert operation.has_error
    with pytest.raises(VpeServiceOverloadedError):
        parse_result(operation)


def test_poll_keeps_the_operation_name_when_the_response_omits_it():
    """An error body without a name still identifies its own job."""
    session = FakeSession(
        [FakeResponse({"done": True, "error": {"code": 8, "message": "x"}})],
    )
    client = make_client(session)

    operation = client.poll(DOC_A2V_OPERATION_NAME)

    assert operation.name == DOC_A2V_OPERATION_NAME


def test_poll_requires_an_operation_name():
    """Polling nothing is a programming error, caught locally."""
    client = make_client(ExplodingSession())

    with pytest.raises(ValueError, match="operation_name is required"):
        client.poll("")


# --- Configuration guards --------------------------------------------------


def test_no_call_is_made_when_vpe_is_disabled():
    """The transport is the last gate before money is spent."""
    client = VpeClient(project_id="test-project", location="us-central1")

    with patch.object(config_service, "VPE_ENABLED", False):
        with patch.object(client_module.google.auth, "default") as default:
            with pytest.raises(VpeConfigurationError, match="VPE_ENABLED"):
                client.submit(SAMPLE_PAYLOAD)

    default.assert_not_called()


def test_the_session_is_built_once_from_default_credentials():
    """Credentials are resolved lazily and the session is reused."""
    client = VpeClient(project_id="test-project", location="us-central1")
    credentials = MagicMock()

    with patch.object(config_service, "VPE_ENABLED", True):
        with patch.object(
            client_module.google.auth,
            "default",
            return_value=(credentials, "test-project"),
        ) as default:
            with patch.object(
                client_module,
                "AuthorizedSession",
                return_value=FakeSession(
                    [
                        FakeResponse({"name": "op/1"}),
                        FakeResponse(IN_PROGRESS_BODY),
                    ],
                ),
            ) as session_class:
                client.submit(SAMPLE_PAYLOAD)
                client.poll("op/1")

    assert default.call_count == 1
    assert default.call_args.kwargs["scopes"] == [
        "https://www.googleapis.com/auth/cloud-platform",
    ]
    session_class.assert_called_once_with(credentials)


def test_a_global_location_is_refused():
    """VPE has no global endpoint; the hostname would not resolve."""
    client = make_client(location="global")

    with pytest.raises(VpeConfigurationError, match="must be a region"):
        client.submit(SAMPLE_PAYLOAD)


def test_a_missing_project_is_refused():
    """An unset project would build a URL with an empty path segment."""
    client = VpeClient(
        project_id="",
        location="us-central1",
        session=ExplodingSession(),
        config=MagicMock(PROJECT_ID="", VPE_LOCATION="us-central1"),
    )

    with pytest.raises(VpeConfigurationError, match="PROJECT_ID"):
        client.submit(SAMPLE_PAYLOAD)


# --- Dry run ---------------------------------------------------------------


def test_dry_run_sends_nothing_and_returns_a_synthetic_operation():
    """Dry run is the only way this path runs without an allowlist."""
    client = make_client(ExplodingSession(), dry_run=True)

    operation = client.submit(SAMPLE_PAYLOAD)

    assert operation.dry_run
    assert DRY_RUN_MARKER in operation.name
    assert operation.name.startswith("projects/test-project/locations/")
    assert not operation.done


def test_dry_run_logs_the_fully_rendered_request(caplog):
    """The logged body is what a reviewer diffs against the doc sample."""
    caplog.set_level(logging.INFO, logger=client_module.__name__)
    client = make_client(ExplodingSession(), dry_run=True)

    client.submit(SAMPLE_PAYLOAD)

    logged = caplog.text
    assert "request NOT sent" in logged
    assert ":predictLongRunning" in logged
    assert f'"{VPE_REQUEST_TYPE_HEADER}": "{VPE_REQUEST_TYPE_SHARED}"' in logged
    assert '"modelName": "veo-exp-a2v-generation"' in logged
    assert '"storageUri": "gs://my-bucket/outputs/"' in logged
    assert '"prompt": "A person speaking passionately about shampoo"' in logged


def test_dry_run_completes_on_the_first_poll():
    """The synthetic job finishes so the whole path can be walked."""
    client = make_client(ExplodingSession(), dry_run=True)

    operation = client.submit(SAMPLE_PAYLOAD)
    polled = client.poll(operation.name)
    result = parse_result(polled)

    assert polled.done
    assert result.primary_video.gcs_uri == (
        f"gs://my-bucket/outputs/{operation.operation_id}/sample_0.mp4"
    )
    assert result.primary_video.mime_type == "video/mp4"
    assert result.output_directory_uri.startswith("gs://my-bucket/outputs/")


def test_dry_run_output_follows_the_requested_codec():
    """A ProRes job is predicted to write a .mov preview."""
    payload = {
        "instances": [{"prompt": "x"}],
        "parameters": {
            "storageUri": "gs://my-bucket/outputs/",
            "experiments": {"modelName": "omni-cine", "codec": "prores"},
        },
    }
    client = make_client(ExplodingSession(), dry_run=True)

    result = parse_result(client.poll(client.submit(payload).name))

    assert result.primary_video.gcs_uri.endswith("/sample_0.mov")
    assert result.primary_video.mime_type == "video/quicktime"


def test_a_dry_run_operation_is_never_sent_to_the_real_api(caplog):
    """A fabricated name stays local even after the flag is turned off."""
    caplog.set_level(logging.WARNING, logger=client_module.__name__)
    submitter = make_client(ExplodingSession(), dry_run=True)
    name = submitter.submit(SAMPLE_PAYLOAD).name

    live_client = make_client(ExplodingSession(), dry_run=False)
    operation = live_client.poll(name)

    assert operation.done
    assert "VPE_DRY_RUN is now off" in caplog.text


def test_a_dry_run_poll_on_a_fresh_client_falls_back_to_the_bucket():
    """A restart loses the submitted storageUri but still answers."""
    config = MagicMock(
        PROJECT_ID="test-project",
        VPE_LOCATION="us-central1",
        VPE_DRY_RUN=True,
        VPE_BUCKET="configured-bucket",
    )
    client = VpeClient(config=config, session=ExplodingSession())

    result = parse_result(
        client.poll(
            f"projects/test-project/operations/{DRY_RUN_MARKER}abc123",
        ),
    )

    assert result.primary_video.gcs_uri == (
        "gs://configured-bucket/vpe-dry-run"
        f"/{DRY_RUN_MARKER}abc123/sample_0.mp4"
    )


@pytest.mark.asyncio
async def test_dry_run_walks_submit_then_wait_end_to_end(fake_time):
    """The async path completes without credentials or network."""
    client = make_client(ExplodingSession(), dry_run=True)

    operation = await client.submit_async(SAMPLE_PAYLOAD)
    result = await client.wait_for_completion(operation)

    assert result.primary_video.gcs_uri.endswith("/sample_0.mp4")
    assert fake_time.sleeps == [client_module.INITIAL_POLL_SECONDS]


# --- Waiting for completion ------------------------------------------------


@pytest.mark.asyncio
async def test_wait_polls_with_widening_intervals_until_done(fake_time):
    """Backoff widens between polls instead of hammering the endpoint."""
    session = FakeSession(
        [
            FakeResponse(IN_PROGRESS_BODY),
            FakeResponse(IN_PROGRESS_BODY),
            FakeResponse(SUCCESS_BODY),
        ],
    )
    client = make_client(session)

    result = await client.wait_for_completion(DOC_A2V_OPERATION_NAME)

    assert result.primary_video.gcs_uri == (
        "gs://BUCKET_NAME/outputs/sample_0.mp4"
    )
    assert len(session.calls) == 3
    assert fake_time.sleeps == [10.0, 15.0, 22.5]


@pytest.mark.asyncio
async def test_wait_caps_the_poll_interval(fake_time):
    """The delay stops widening at the ceiling."""
    session = FakeSession(
        [FakeResponse(IN_PROGRESS_BODY) for _ in range(11)]
        + [FakeResponse(SUCCESS_BODY)],
    )
    client = make_client(session)

    await client.wait_for_completion(DOC_A2V_OPERATION_NAME)

    assert max(fake_time.sleeps) == client_module.MAX_POLL_SECONDS
    assert fake_time.sleeps[-1] == client_module.MAX_POLL_SECONDS


@pytest.mark.asyncio
async def test_wait_gives_up_at_the_timeout_without_cancelling(fake_time):
    """A timeout is a client decision; the job keeps running."""
    session = FakeSession([FakeResponse(IN_PROGRESS_BODY) for _ in range(10)])
    client = make_client(session)

    with pytest.raises(VpeTimeoutError) as raised:
        await client.wait_for_completion(
            DOC_A2V_OPERATION_NAME,
            timeout_seconds=30.0,
        )

    assert raised.value.operation_name == DOC_A2V_OPERATION_NAME
    assert raised.value.timeout_seconds == 30.0
    assert "was not cancelled" in str(raised.value)
    # 10 + 15 + the 5 seconds left of the budget, then the deadline.
    assert fake_time.sleeps == [10.0, 15.0, 5.0]


@pytest.mark.asyncio
async def test_wait_short_circuits_an_already_finished_operation(fake_time):
    """A done operation needs no poll and no sleep."""
    session = FakeSession([])
    client = make_client(session)

    result = await client.wait_for_completion(
        VpeOperation.from_body(SUCCESS_BODY),
    )

    assert result.primary_video.mime_type == "video/mp4"
    assert not session.calls
    assert not fake_time.sleeps


@pytest.mark.asyncio
async def test_wait_survives_a_few_failed_polls(fake_time, caplog):
    """A dropped poll says nothing about a job that is still running."""
    caplog.set_level(logging.WARNING, logger=client_module.__name__)
    session = FakeSession(
        [
            ConnectionResetError("boom"),
            ConnectionResetError("boom"),
            FakeResponse(SUCCESS_BODY),
        ],
    )
    client = make_client(session)

    result = await client.wait_for_completion(DOC_A2V_OPERATION_NAME)

    assert result.primary_video.gcs_uri.endswith("sample_0.mp4")
    assert len(fake_time.sleeps) == 3
    assert "the job is still running" in caplog.text


@pytest.mark.asyncio
async def test_wait_gives_up_after_repeated_poll_failures(fake_time):
    """Persistent transport failure eventually has to be reported."""
    session = FakeSession([ConnectionResetError("boom") for _ in range(6)])
    client = make_client(session)

    with pytest.raises(VpeTransportError):
        await client.wait_for_completion(
            DOC_A2V_OPERATION_NAME,
            max_consecutive_failures=3,
        )

    assert len(session.calls) == 3
    assert fake_time.sleeps


@pytest.mark.asyncio
async def test_wait_raises_the_typed_error_for_a_failed_job(fake_time):
    """The failure surfaces as its documented cause, not as "done"."""
    session = FakeSession(
        [FakeResponse(IN_PROGRESS_BODY), FakeResponse(VIDEO_TOO_LONG_BODY)],
    )
    client = make_client(session)

    with pytest.raises(VpeVideoTooLongError) as raised:
        await client.wait_for_completion(DOC_OPERATION_NAME)

    assert raised.value.actual_seconds == 15.0
    assert fake_time.sleeps


@pytest.mark.asyncio
async def test_wait_reinterprets_high_load_with_a_preflight_snapshot(
    fake_time,
):
    """End to end, an off-spec input is not reported as capacity."""
    session = FakeSession([FakeResponse(HIGH_LOAD_BODY)])
    client = make_client(session)

    with pytest.raises(VpeMisreportedInputError) as raised:
        await client.wait_for_completion(
            DOC_OPERATION_NAME,
            preflight=FPS_DEVIATION,
        )

    assert not raised.value.retryable
    assert fake_time.sleeps


@pytest.mark.asyncio
async def test_wait_reports_progress_through_the_callback(fake_time):
    """Callers need a hook to log or persist intermediate state."""
    session = FakeSession(
        [FakeResponse(IN_PROGRESS_BODY), FakeResponse(SUCCESS_BODY)],
    )
    client = make_client(session)
    seen = []

    await client.wait_for_completion(
        DOC_A2V_OPERATION_NAME,
        on_poll=seen.append,
    )

    assert [operation.done for operation in seen] == [False, True]
    assert fake_time.sleeps


@pytest.mark.asyncio
async def test_the_blocking_call_runs_off_the_event_loop():
    """Submission is offloaded the way veo_service offloads the SDK."""
    session = FakeSession([FakeResponse({"name": DOC_A2V_OPERATION_NAME})])
    client = make_client(session)

    with patch.object(
        client_module.asyncio,
        "to_thread",
        wraps=asyncio.to_thread,
    ) as to_thread:
        operation = await client.submit_async(SAMPLE_PAYLOAD)

    assert operation.name == DOC_A2V_OPERATION_NAME
    to_thread.assert_called_once()
