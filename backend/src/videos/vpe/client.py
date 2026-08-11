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
"""REST transport for the Veo Pro Experimental (VPE) endpoint.

VPE is the app's third video surface and the only one without a client
library. There is no google-genai model id for it and no interactions
session: every capability is a raw POST to one shared publisher endpoint,
routed by ``parameters.experiments.modelName`` in the body, returning a
long-running operation that is polled through a second POST.

Three things about that surface drive the design here.

Both calls need ``X-Vertex-AI-LLM-Request-Type: shared``. It appears on every
sample in every capability doc, and without it the request does not reach the
experimental checkpoints at all, so the header is set by this module rather
than left to callers to remember.

Jobs are slow. A VPE job renders up to 240 frames at up to 4K and is queued
behind an allowlisted pool, so ``wait_for_completion`` is built around a
half-hour ceiling with widening intervals rather than the flat ten-second
loop the SDK path uses - and it tolerates a few failed polls, because a
dropped connection says nothing about an operation that is still running.

Nobody on this project can execute a VPE call - the program is allowlist
gated and this project is not on the allowlist. ``VPE_DRY_RUN`` therefore
renders and logs the exact request that would have been sent and answers with
a synthetic operation that completes on its first poll, so payload builders,
polling, result parsing and the ingestion path downstream can all be walked
end to end without credentials. It is the only way this code gets exercised
before someone with access runs it for real.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import google.auth
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

from src.config.config_service import ConfigService, config_service
from src.videos.vpe.capabilities import (
    MIME_MP4,
    MIME_MXF,
    MIME_QUICKTIME,
    VPE_ENDPOINT_MODEL_ID,
    VpeCodec,
)
from src.videos.vpe.errors import (
    PreflightSnapshot,
    VpeConfigurationError,
    VpeInvalidPayloadError,
    VpeMissingOperationError,
    VpeMissingOutputError,
    VpeTimeoutError,
    VpeTransportError,
    classify_error,
)

logger = logging.getLogger(__name__)

# The two methods on the shared publisher endpoint. Note fetchPredictOperation
# is a POST with the operation name in the body, not a GET on the operation
# resource: the usual google.api_core operations client cannot drive it.
VPE_PREDICT_METHOD = "predictLongRunning"
VPE_FETCH_METHOD = "fetchPredictOperation"

# Required on both calls. Documented on every sample in every capability doc.
VPE_REQUEST_TYPE_HEADER = "X-Vertex-AI-LLM-Request-Type"
VPE_REQUEST_TYPE_SHARED = "shared"

_AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

# Submission and each poll return immediately; only the job is long. A
# generous socket timeout still bounds a hung connection.
HTTP_TIMEOUT_SECONDS = 60.0

# A VPE job runs for minutes. Poll gently and widen: the first poll is never
# going to find a finished job, and a tight loop only spends quota on the
# fetch endpoint. No jitter - one operation is polled by one worker, so there
# is no herd to disperse, and determinism keeps the tests honest.
DEFAULT_TIMEOUT_SECONDS = 1800.0
INITIAL_POLL_SECONDS = 10.0
MAX_POLL_SECONDS = 60.0
POLL_BACKOFF_FACTOR = 1.5

# A failed poll is not a failed job. Tolerate a short run of transport errors
# before concluding the operation is unreachable.
MAX_CONSECUTIVE_POLL_FAILURES = 5

# Marks an operation this module invented. Carried in the name so a poll can
# recognise it even if the flag was turned off in between, which otherwise
# would send a fabricated name to the real API.
DRY_RUN_MARKER = "vpe-dry-run-"

# Preview container per codec, used only to make a dry-run output URI look
# like the real thing. Real jobs write whatever experiments.codec selected.
_CODEC_PREVIEW = {
    VpeCodec.H264.value: ("mp4", MIME_MP4),
    VpeCodec.PRORES.value: ("mov", MIME_QUICKTIME),
    VpeCodec.DNXHR.value: ("mxf", MIME_MXF),
}

# Enough of a failing body to diagnose it, not enough to flood the log with a
# base64 payload echoed back at us.
_MAX_LOGGED_BODY_CHARS = 2000


class HttpResponse(Protocol):
    """The part of a requests.Response this module uses."""

    status_code: int
    text: str

    def json(self) -> Any:
        """Returns the parsed JSON body."""


class HttpSession(Protocol):
    """The part of an AuthorizedSession this module uses.

    Narrowed to one method so tests can substitute a fake without standing up
    credentials, and so the transport can be swapped later without touching
    call sites.
    """

    # pylint: disable=redefined-outer-name
    # The keyword is named json because requests names it json; renaming it
    # here would stop the protocol describing the object it stands for.
    def post(
        self,
        url: str,
        *,
        json: Any,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        """Sends a JSON POST and returns the response."""


@dataclass(frozen=True, slots=True)
class VpeOperation:
    """A long-running VPE operation, as last observed.

    Attributes:
        name: Full resource name, the only handle on the job.
        done: Whether the operation has finished, successfully or not. An
            in-progress poll omits the field entirely rather than sending
            false, which is why this is derived rather than read straight.
        raw: The parsed response body, kept whole so nothing undocumented is
            silently discarded.
        dry_run: True when this module invented the operation.
    """

    name: str
    done: bool
    raw: Mapping[str, Any]
    dry_run: bool = False

    @property
    def operation_id(self) -> str:
        """Returns the trailing id, which is what support asks for."""
        return self.name.rsplit("/", 1)[-1]

    @property
    def has_error(self) -> bool:
        """Returns True if the operation finished with a failure."""
        return isinstance(self.raw.get("error"), Mapping)

    @classmethod
    def from_body(
        cls,
        body: Mapping[str, Any],
        *,
        dry_run: bool = False,
    ) -> "VpeOperation":
        """Builds an operation from a submit or poll response.

        Args:
            body: The parsed response body.
            dry_run: True when the body was synthesised locally.

        Returns:
            The operation the body describes.
        """
        return cls(
            name=str(body.get("name") or ""),
            done=bool(body.get("done", False)),
            raw=body,
            dry_run=dry_run,
        )


@dataclass(frozen=True, slots=True)
class VpeVideoOutput:
    """One entry of ``response.videos``."""

    gcs_uri: str
    mime_type: str = ""

    @property
    def directory_uri(self) -> str:
        """Returns the folder the job wrote into.

        VPE writes a whole directory, not a file: the named preview video
        sits beside ``sample_0/frame_####.png|exr``, ``sample_0_audio.wav``,
        ``request.json`` and ``prompt.txt``, inside a random subdirectory of
        the requested storageUri. The response names only the preview, so
        this is the prefix everything else has to be found under.

        Returns:
            The parent gs:// prefix of the named file, without a trailing
            slash.
        """
        return self.gcs_uri.rsplit("/", 1)[0]


@dataclass(frozen=True, slots=True)
class VpeResult:
    """The outputs of a successful VPE operation.

    Only what the response actually carries. Enumerating the rest of the
    output directory - the frame sequence, the audio stem, the provenance
    files - is left to a later task, and will need a list-by-prefix call that
    ``src.common.storage_service.GcsService`` does not expose today: it can
    download, upload and delete a known object, but cannot list one.
    """

    operation_name: str
    videos: tuple[VpeVideoOutput, ...]
    rai_media_filtered_count: int
    raw: Mapping[str, Any]

    @property
    def primary_video(self) -> VpeVideoOutput:
        """Returns the preview video, which is the gallery-ingestible one."""
        return self.videos[0]

    @property
    def output_directory_uri(self) -> str:
        """Returns the folder holding every artifact of this job."""
        return self.primary_video.directory_uri


def parse_result(
    operation: VpeOperation,
    *,
    preflight: PreflightSnapshot | None = None,
) -> VpeResult:
    """Reads the outputs off a finished operation.

    Args:
        operation: A completed operation, as returned by a poll.
        preflight: What preflight measured before submitting, forwarded to
            error classification so a spurious high-load failure can be
            reported as the input problem it probably is.

    Returns:
        The operation's outputs.

    Raises:
        VpeApiError: If the operation finished with a failure.
        VpeMissingOutputError: If it finished clean but named no output.
        ValueError: If the operation has not finished yet.
    """
    if not operation.done:
        raise ValueError(
            f"Operation {operation.name} has not finished; poll it first",
        )
    if operation.has_error:
        raise classify_error(operation.raw, preflight=preflight)

    response = operation.raw.get("response")
    videos: list[VpeVideoOutput] = []
    if isinstance(response, Mapping):
        for entry in response.get("videos") or ():
            if isinstance(entry, Mapping) and entry.get("gcsUri"):
                videos.append(
                    VpeVideoOutput(
                        gcs_uri=str(entry["gcsUri"]),
                        mime_type=str(entry.get("mimeType") or ""),
                    ),
                )
    if not videos:
        raise VpeMissingOutputError(operation.name, operation.raw)

    filtered = 0
    if isinstance(response, Mapping):
        raw_filtered = response.get("raiMediaFilteredCount")
        if isinstance(raw_filtered, (int, str)):
            try:
                filtered = int(raw_filtered)
            except ValueError:
                filtered = 0

    return VpeResult(
        operation_name=operation.name,
        videos=tuple(videos),
        rai_media_filtered_count=filtered,
        raw=operation.raw,
    )


class VpeClient:
    """Authenticated REST client for the veo-experimental endpoint.

    One instance can drive every capability: the checkpoint lives in the
    payload, not in the URL, so nothing here is capability-specific.

    Configuration is read at call time rather than captured in the
    constructor, so a client held by a long-lived worker honours a flag that
    changed since it was built.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str | None = None,
        session: HttpSession | None = None,
        config: ConfigService | None = None,
        dry_run: bool | None = None,
    ) -> None:
        """Initialises the client without touching credentials.

        Nothing is authenticated here. A worker builds this object on a
        deployment where VPE is switched off, and a client that reached for
        application default credentials in its constructor would fail, or
        hang against the metadata server, for a feature nobody enabled.

        Args:
            project_id: Overrides ``PROJECT_ID``.
            location: Overrides ``VPE_LOCATION``. Must be a real region:
                ``global`` cannot form the regional hostname VPE requires.
            session: Pre-built authorised session, mainly for tests.
            config: Settings object, defaulting to the app-wide instance.
            dry_run: Overrides ``VPE_DRY_RUN`` for a single client.
        """
        self._config = config or config_service
        self._project_id_override = project_id
        self._location_override = location
        self._session = session
        self._dry_run_override = dry_run
        # A dry-run poll has to answer with the output folder the submission
        # asked for, and only the submission knows it. Cheap to remember, and
        # a restart just falls back to the configured bucket.
        self._dry_run_outputs: dict[str, tuple[str, str]] = {}

    @property
    def dry_run(self) -> bool:
        """Returns True when requests are rendered instead of sent."""
        if self._dry_run_override is not None:
            return self._dry_run_override
        return bool(self._config.VPE_DRY_RUN)

    @property
    def location(self) -> str:
        """Returns the region whose regional endpoint will be called."""
        return self._location_override or self._config.VPE_LOCATION

    @property
    def project_id(self) -> str:
        """Returns the project the endpoint is addressed under."""
        return self._project_id_override or self._config.PROJECT_ID

    @property
    def resource_path(self) -> str:
        """Returns the publisher model path shared by both methods."""
        return (
            f"projects/{self.project_id}/locations/{self.location}"
            f"/publishers/google/models/{VPE_ENDPOINT_MODEL_ID}"
        )

    def endpoint_url(self, method: str) -> str:
        """Builds the URL of one endpoint method.

        Args:
            method: ``predictLongRunning`` or ``fetchPredictOperation``.

        Returns:
            The absolute URL to POST to.

        Raises:
            VpeConfigurationError: If the project or location is unset.
        """
        if not self.project_id:
            raise VpeConfigurationError(
                "PROJECT_ID is not set; VPE has no project to call under.",
            )
        location = self.location
        if not location or location == "global":
            raise VpeConfigurationError(
                f"VPE_LOCATION must be a region, got {location!r}. VPE is"
                " reached at {region}-aiplatform.googleapis.com, which"
                " 'global' cannot form.",
            )
        return (
            f"https://{location}-aiplatform.googleapis.com/v1/"
            f"{self.resource_path}:{method}"
        )

    @property
    def headers(self) -> dict[str, str]:
        """Returns the headers both endpoint methods require.

        Authorization is not among them: the authorised session mints and
        refreshes the bearer token per request.
        """
        return {
            "Content-Type": "application/json",
            VPE_REQUEST_TYPE_HEADER: VPE_REQUEST_TYPE_SHARED,
        }

    def submit(
        self,
        payload: Mapping[str, Any],
        *,
        preflight: PreflightSnapshot | None = None,
    ) -> VpeOperation:
        """Starts a VPE job.

        Args:
            payload: The full request body - ``instances`` plus
                ``parameters``, with the checkpoint in
                ``parameters.experiments.modelName``.
            preflight: What preflight measured about this job's inputs. Used
                only to classify a failure: a rejection at submission time
                can be the misreported-input defect just as a rejection at
                poll time can.

        Returns:
            The accepted operation, not yet finished.

        Raises:
            VpeInvalidPayloadError: If the body names no checkpoint.
            VpeConfigurationError: If VPE is off or unconfigured.
            VpeApiError: If the endpoint rejected the request.
            VpeMissingOperationError: If it accepted it but named no
                operation.
            VpeTransportError: If the exchange itself failed.
        """
        model_name = self._require_model_name(payload)
        url = self.endpoint_url(VPE_PREDICT_METHOD)

        if self.dry_run:
            return self._synthetic_submit(url, payload, model_name)

        body = self._post(url, payload, preflight=preflight)
        operation = VpeOperation.from_body(body)
        if not operation.name:
            raise VpeMissingOperationError(body)
        logger.info(
            "VPE job submitted",
            extra={
                "json_fields": {
                    "vpe_model_name": model_name,
                    "operation_name": operation.name,
                },
            },
        )
        return operation

    def poll(
        self,
        operation_name: str,
        *,
        preflight: PreflightSnapshot | None = None,
    ) -> VpeOperation:
        """Fetches the current state of an operation.

        Args:
            operation_name: Full resource name returned by ``submit``.
            preflight: What preflight measured about this job's inputs, used
                to classify a rejection of the fetch call itself.

        Returns:
            The operation as the service currently reports it. An
            in-progress operation comes back with ``done`` False; a failed
            one comes back done, carrying an error, over HTTP 200.

        Raises:
            ValueError: If no operation name was given.
            VpeConfigurationError: If VPE is off or unconfigured.
            VpeApiError: If the fetch call itself was rejected.
            VpeTransportError: If the exchange itself failed.
        """
        if not operation_name:
            raise ValueError("operation_name is required to poll")
        if DRY_RUN_MARKER in operation_name:
            return self._synthetic_poll(operation_name)

        url = self.endpoint_url(VPE_FETCH_METHOD)
        body = self._post(
            url,
            {"operationName": operation_name},
            preflight=preflight,
        )
        # The fetch response echoes the name, but an error body may not, and
        # losing it would leave the caller unable to say which job failed.
        if not body.get("name"):
            body = {**body, "name": operation_name}
        return VpeOperation.from_body(body)

    async def submit_async(
        self,
        payload: Mapping[str, Any],
        *,
        preflight: PreflightSnapshot | None = None,
    ) -> VpeOperation:
        """Starts a VPE job without blocking the event loop.

        The transport is the blocking ``requests`` stack, wrapped the way
        veo_service.py wraps the blocking GenAI SDK, so a submission cannot
        stall the worker's loop.

        Args:
            payload: The full request body.
            preflight: What preflight measured about this job's inputs.

        Returns:
            The accepted operation.
        """
        return await asyncio.to_thread(
            self.submit,
            payload,
            preflight=preflight,
        )

    async def poll_async(
        self,
        operation_name: str,
        *,
        preflight: PreflightSnapshot | None = None,
    ) -> VpeOperation:
        """Fetches operation state without blocking the event loop.

        Args:
            operation_name: Full resource name returned by ``submit``.
            preflight: What preflight measured about this job's inputs.

        Returns:
            The operation as the service currently reports it.
        """
        return await asyncio.to_thread(
            self.poll,
            operation_name,
            preflight=preflight,
        )

    async def wait_for_completion(
        self,
        operation: VpeOperation | str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        initial_interval_seconds: float = INITIAL_POLL_SECONDS,
        max_interval_seconds: float = MAX_POLL_SECONDS,
        backoff_factor: float = POLL_BACKOFF_FACTOR,
        max_consecutive_failures: int = MAX_CONSECUTIVE_POLL_FAILURES,
        preflight: PreflightSnapshot | None = None,
        on_poll: Callable[[VpeOperation], None] | None = None,
    ) -> VpeResult:
        """Polls until the job finishes, then returns its outputs.

        Waits before the first poll on purpose: no VPE job has ever finished
        in under a second, so an immediate fetch is a wasted call.

        Args:
            operation: The operation, or just its resource name.
            timeout_seconds: How long to keep polling in total.
            initial_interval_seconds: Delay before the first poll.
            max_interval_seconds: Ceiling the widening delay stops at.
            backoff_factor: Multiplier applied after each unfinished poll.
            max_consecutive_failures: Transport failures tolerated in a row
                before giving up. A dropped poll says nothing about the job.
            preflight: What preflight measured before submitting, used to
                explain a misreported high-load failure.
            on_poll: Called with each observed state, for progress logging.

        Returns:
            The finished operation's outputs.

        Raises:
            VpeTimeoutError: If the ceiling is reached first. The job is not
                cancelled and can still be polled by name.
            VpeApiError: If the operation finished with a failure.
            VpeMissingOutputError: If it finished clean but named no output.
            VpeTransportError: If polling fails repeatedly.
        """
        if isinstance(operation, VpeOperation):
            if operation.done:
                return parse_result(operation, preflight=preflight)
            name = operation.name
        else:
            name = operation

        started = time.monotonic()
        deadline = started + timeout_seconds
        interval = initial_interval_seconds
        failures = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VpeTimeoutError(
                    name,
                    elapsed_seconds=time.monotonic() - started,
                    timeout_seconds=timeout_seconds,
                )
            await asyncio.sleep(min(interval, remaining))

            try:
                current = await self.poll_async(name, preflight=preflight)
                failures = 0
            except VpeTransportError as error:
                failures += 1
                if failures >= max_consecutive_failures:
                    raise
                logger.warning(
                    "VPE poll failed (%d/%d), the job is still running: %s",
                    failures,
                    max_consecutive_failures,
                    error,
                    extra={"json_fields": {"operation_name": name}},
                )
                interval = min(interval * backoff_factor, max_interval_seconds)
                continue

            if on_poll is not None:
                on_poll(current)
            if current.done:
                return parse_result(current, preflight=preflight)

            logger.info(
                "Waiting for VPE operation to complete",
                extra={
                    "json_fields": {
                        "operation_name": name,
                        "elapsed_seconds": round(
                            time.monotonic() - started,
                            1,
                        ),
                    },
                },
            )
            interval = min(interval * backoff_factor, max_interval_seconds)

    def _post(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        preflight: PreflightSnapshot | None = None,
    ) -> Mapping[str, Any]:
        """POSTs a JSON body and returns the parsed response.

        Args:
            url: Absolute endpoint URL.
            body: The JSON body to send.
            preflight: What preflight measured, forwarded to classification.

        Returns:
            The parsed response body, including a completed-but-failed
            operation, which is a valid answer rather than a call failure.

        Raises:
            VpeConfigurationError: If VPE is disabled or has no credentials.
            VpeApiError: If the endpoint refused the request itself.
            VpeTransportError: If the call or the parse failed.
        """
        session = self._authorized_session()
        try:
            response = session.post(
                url,
                json=body,
                headers=self.headers,
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        # pylint: disable=broad-exception-caught
        except Exception as error:
            # Anything the transport stack raises - DNS, TLS, a read timeout
            # - means the same thing to a caller: no answer, try again.
            raise VpeTransportError(
                f"VPE request to {url} failed: {error}",
            ) from error

        try:
            parsed = response.json()
        # pylint: disable=broad-exception-caught
        except Exception as error:
            raise VpeTransportError(
                f"VPE returned a non-JSON body (HTTP {response.status_code})",
                status_code=response.status_code,
                body=response.text[:_MAX_LOGGED_BODY_CHARS],
            ) from error

        if not isinstance(parsed, Mapping):
            raise VpeTransportError(
                "VPE returned a JSON body that is not an object"
                f" (HTTP {response.status_code})",
                status_code=response.status_code,
                body=str(parsed)[:_MAX_LOGGED_BODY_CHARS],
            )

        # Two envelopes carry an error object and they mean different things.
        # A refused *request* is an HTTP error carrying a bare
        # {"error": ...} and nothing else; there is no operation to return,
        # so it raises here. A failed *operation* arrives over HTTP 200 as a
        # complete operation that happens to have failed - it names itself,
        # or at least says done - and it is a legitimate answer to the fetch,
        # so it is returned and parse_result classifies it.
        is_error = isinstance(parsed.get("error"), Mapping)
        is_operation = bool(parsed.get("name")) or "done" in parsed
        if is_error and (response.status_code >= 400 or not is_operation):
            raise classify_error(
                parsed,
                http_status=response.status_code,
                preflight=preflight,
            )

        if response.status_code >= 400:
            raise VpeTransportError(
                f"VPE returned HTTP {response.status_code} with no error"
                " envelope",
                status_code=response.status_code,
                body=response.text[:_MAX_LOGGED_BODY_CHARS],
            )
        return parsed

    def _authorized_session(self) -> HttpSession:
        """Returns the session that carries the bearer token.

        AuthorizedSession rather than a token minted once at startup: it
        refreshes application default credentials per request, and a VPE job
        outlives an access token, so the poll an hour in must not be signed
        with the token the submission used.

        Returns:
            The session to POST through.

        Raises:
            VpeConfigurationError: If VPE is disabled, or credentials cannot
                be resolved.
        """
        if self._session is not None:
            return self._session
        # Defence in depth behind the feature flag. Everything above this
        # point is inert; this is the one place that spends money, so it is
        # the one place that refuses to act on a deployment that never opted
        # in. Dry run never reaches here.
        if not self._config.VPE_ENABLED:
            raise VpeConfigurationError(
                "VPE_ENABLED is false; refusing to call the Veo Pro"
                " Experimental endpoint. Set VPE_DRY_RUN to render requests"
                " without sending them.",
            )
        try:
            credentials, _ = google.auth.default(scopes=list(_AUTH_SCOPES))
        except GoogleAuthError as error:
            raise VpeConfigurationError(
                f"No application default credentials for VPE: {error}",
            ) from error
        self._session = AuthorizedSession(credentials)
        return self._session

    @staticmethod
    def _require_model_name(payload: Mapping[str, Any]) -> str:
        """Returns the checkpoint the payload routes to.

        Args:
            payload: The request body about to be sent.

        Returns:
            The ``parameters.experiments.modelName`` value.

        Raises:
            VpeInvalidPayloadError: If instances or the checkpoint are
                missing. One shared endpoint serves every capability, so a
                body without a model name is not a smaller request - it is an
                unroutable one.
        """
        instances = payload.get("instances")
        if not isinstance(instances, list) or not instances:
            raise VpeInvalidPayloadError(
                "A VPE request needs a non-empty instances array.",
            )
        parameters = payload.get("parameters")
        experiments = (
            parameters.get("experiments")
            if isinstance(parameters, Mapping)
            else None
        )
        model_name = (
            experiments.get("modelName")
            if isinstance(experiments, Mapping)
            else None
        )
        if not model_name:
            raise VpeInvalidPayloadError(
                "A VPE request needs parameters.experiments.modelName; the"
                " shared endpoint has no other way to pick a checkpoint.",
            )
        return str(model_name)

    def _synthetic_submit(
        self,
        url: str,
        payload: Mapping[str, Any],
        model_name: str,
    ) -> VpeOperation:
        """Logs the rendered request and invents an operation.

        Args:
            url: The URL the request would have gone to.
            payload: The body that would have been sent.
            model_name: The checkpoint it would have routed to.

        Returns:
            An unfinished synthetic operation whose first poll completes.
        """
        operation_name = (
            f"{self.resource_path}/operations/{DRY_RUN_MARKER}"
            f"{uuid.uuid4().hex}"
        )
        # The rendered body is the whole point of dry run, so it is logged
        # in full and pretty-printed: this is what a reviewer compares
        # against the curl sample in the docs.
        logger.info(
            "VPE dry run - request NOT sent:\nPOST %s\n%s\n%s",
            url,
            json.dumps(self.headers, indent=2, sort_keys=True),
            json.dumps(payload, indent=2, sort_keys=False, default=str),
            extra={
                "json_fields": {
                    "vpe_dry_run": True,
                    "vpe_model_name": model_name,
                    "operation_name": operation_name,
                },
            },
        )
        self._dry_run_outputs[operation_name] = self._synthetic_output(
            payload,
            operation_name,
        )
        return VpeOperation(
            name=operation_name,
            done=False,
            raw={"name": operation_name},
            dry_run=True,
        )

    def _synthetic_poll(self, operation_name: str) -> VpeOperation:
        """Completes a synthetic operation without calling anything.

        Keyed off the name rather than the flag so an operation invented in
        dry run is never posted to the real endpoint if the flag flips.

        Args:
            operation_name: A name carrying the dry-run marker.

        Returns:
            A finished synthetic operation shaped like the documented
            success response.
        """
        if not self.dry_run:
            logger.warning(
                "Completing dry-run operation %s locally even though"
                " VPE_DRY_RUN is now off; it was never sent anywhere.",
                operation_name,
            )
        gcs_uri, mime_type = self._dry_run_outputs.get(
            operation_name,
            self._synthetic_output({}, operation_name),
        )
        return VpeOperation(
            name=operation_name,
            done=True,
            raw={
                "name": operation_name,
                "done": True,
                "response": {
                    "@type": (
                        "type.googleapis.com/cloud.ai.large_models.vision"
                        ".GenerateVideoResponse"
                    ),
                    "raiMediaFilteredCount": 0,
                    "videos": [
                        {"gcsUri": gcs_uri, "mimeType": mime_type},
                    ],
                },
            },
            dry_run=True,
        )

    def _synthetic_output(
        self,
        payload: Mapping[str, Any],
        operation_name: str,
    ) -> tuple[str, str]:
        """Predicts where a real job would have written its preview.

        Mirrors the documented layout - a random subdirectory of storageUri
        holding ``sample_0.<ext>`` - so downstream ingestion sees the same
        shape of URI in dry run as it will in production.

        Args:
            payload: The submitted body, empty if it is no longer known.
            operation_name: Stands in for the random subdirectory id.

        Returns:
            The preview URI and its mime type.
        """
        parameters = payload.get("parameters")
        storage_uri = ""
        codec = VpeCodec.H264.value
        if isinstance(parameters, Mapping):
            storage_uri = str(parameters.get("storageUri") or "")
            experiments = parameters.get("experiments")
            if isinstance(experiments, Mapping):
                codec = str(experiments.get("codec") or codec)
        if not storage_uri:
            bucket = self._config.VPE_BUCKET or "vpe-dry-run-bucket"
            storage_uri = f"gs://{bucket}/vpe-dry-run/"
        extension, mime_type = _CODEC_PREVIEW.get(
            codec,
            _CODEC_PREVIEW[VpeCodec.H264.value],
        )
        folder = storage_uri.rstrip("/")
        subdirectory = operation_name.rsplit("/", 1)[-1]
        return (
            f"{folder}/{subdirectory}/sample_0.{extension}",
            mime_type,
        )
