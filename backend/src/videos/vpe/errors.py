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
"""Typed errors for the Veo Pro Experimental (VPE) REST surface.

VPE reports a failure in one of two envelopes and the difference is not
cosmetic. A request the endpoint rejects outright comes back as an HTTP
error with a top-level ``error`` object; a request it accepts and then fails
comes back HTTP 200 as a *completed operation* carrying ``done: true`` and an
``error`` object, minutes later, from the poll call. Both are handled here so
a caller never has to know which one it got.

The messages themselves are the reason this module exists. The user guide
documents eight literal failure bodies whose text is the only machine-usable
signal - the status codes collapse five distinct input problems into a single
``code: 3``. ``classify_error`` matches on that text and returns a type whose
``remedy`` is the doc's own "Suggested resolution", so the person who gets a
long-running job back after ten minutes is told what to change rather than
being shown ``INVALID_ARGUMENT``.

One rule is not derivable from the response at all. The known-issues table
records that input violations were reported as high-load errors - a wrong
input fps surfaced as a capacity message. That defect is marked resolved, but
a spurious capacity failure is indistinguishable from a real one at the wire
level, so ``classify_error`` accepts the preflight snapshot taken before
submission: if the input was already known to be off-spec, a "high load"
reply is reported as the input problem it is far more likely to be. Retrying
a bad input forever is the failure mode this exists to prevent.

Transcribed from the VPE documentation (August 2026 revision). The literal
bodies in that guide are clipped at the right margin by the doc renderer, so
every pattern here matches on the part that is legible in the source rather
than on a reconstructed full sentence.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class VpeStatusCode(int, Enum):
    """Status codes the documented VPE failures carry.

    Mostly canonical gRPC codes, with one exception: the authentication
    failure is rejected before the request reaches the model and comes back
    with the HTTP status in ``error.code`` (401) rather than the gRPC
    UNAUTHENTICATED (16), so both are recognised.
    """

    INVALID_ARGUMENT = 3
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    UNAUTHENTICATED = 16
    HTTP_UNAUTHORIZED = 401


@dataclass(frozen=True, slots=True)
class PreflightDeviation:
    """One measured input property that does not match the VPE spec."""

    field: str
    expected: str
    actual: str

    def __str__(self) -> str:
        """Returns a phrase usable inside a sentence shown to a user."""
        return f"{self.field} should be {self.expected} but is {self.actual}"


@dataclass(frozen=True, slots=True)
class PreflightSnapshot:
    """What local preflight measured about a job's inputs before submitting.

    Deliberately tiny and owned here rather than by the preflight module:
    error classification is the only consumer, and keeping the shared shape
    next to the classifier means the transport layer does not have to import
    an ffmpeg-backed module to explain an error.

    An empty ``deviations`` means preflight found nothing wrong, which is
    what makes a later high-load error believable as a real capacity problem.
    """

    source_uri: str = ""
    deviations: tuple[PreflightDeviation, ...] = ()

    @property
    def has_deviations(self) -> bool:
        """Returns True if preflight found the input off-spec."""
        return bool(self.deviations)

    def summary(self) -> str:
        """Returns every deviation as one comma-separated phrase.

        Returns:
            A human-readable summary, empty if the input was in spec.
        """
        return ", ".join(str(deviation) for deviation in self.deviations)


class VpeError(Exception):
    """Base class for every VPE failure.

    Attributes:
        retryable: Whether repeating the identical request could succeed.
            False by default because most VPE failures are input or
            configuration problems that a retry only makes more expensive.
    """

    retryable: bool = False


class VpeConfigurationError(VpeError):
    """The deployment cannot make the call at all.

    Raised before any network access: VPE disabled, no project, no location,
    no credentials. Never the API's fault and never worth retrying.
    """


class VpeInvalidPayloadError(VpeError):
    """The request body could not route to a VPE checkpoint.

    The shared ``veo-experimental`` endpoint dispatches on
    ``parameters.experiments.modelName``; a body without it is a programming
    error in the payload builder, caught locally rather than paid for.
    """


class VpeTransportError(VpeError):
    """The HTTP exchange failed, so the API's own opinion is unknown.

    Covers a network error, a non-JSON body and any HTTP status without a
    recognisable error envelope. Retryable: a poll that fails this way says
    nothing about the operation, which is still running.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
    ) -> None:
        """Initialises the error.

        Args:
            message: What went wrong at the transport level.
            status_code: HTTP status, if a response arrived at all.
            body: Raw response text, truncated by the caller if large.
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body


class VpeTimeoutError(VpeError):
    """Polling gave up before the operation finished.

    Not a cancellation: VPE documents no way to cancel, so the job keeps
    running and keeps its output. The operation name is the only handle left
    on it, which is why it is carried here rather than logged and dropped.
    """

    def __init__(
        self,
        operation_name: str,
        *,
        elapsed_seconds: float,
        timeout_seconds: float,
    ) -> None:
        """Initialises the error.

        Args:
            operation_name: Full operation resource name, still pollable.
            elapsed_seconds: How long polling actually ran.
            timeout_seconds: The ceiling that was exceeded.
        """
        super().__init__(
            f"VPE operation did not finish within {timeout_seconds:.0f}s"
            f" (waited {elapsed_seconds:.0f}s). The job was not cancelled and"
            f" may still complete; poll {operation_name} to recover it.",
        )
        self.operation_name = operation_name
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds


class VpeMissingOperationError(VpeError):
    """A submission succeeded but named no operation to poll.

    The upscaler's known-issues table records exactly this for 4K 16-bit PNG
    output: neither the operation ID nor the output path comes back, leaving
    the caller unable to find work that is nonetheless running and billable.
    """

    def __init__(self, body: Mapping[str, Any]) -> None:
        """Initialises the error.

        Args:
            body: The parsed submission response, kept for the log.
        """
        super().__init__(
            "VPE accepted the request but returned no operation name, so the"
            " job cannot be polled. This is the documented 4K 16-bit PNG"
            " defect; the output may still appear under storageUri.",
        )
        self.body = body


class VpeContentFilteredError(VpeError):
    """The job ran to completion and its output was withheld.

    Distinct from an operation that merely names no file: nothing was
    written, so no amount of looking in the output folder will find
    anything, and resubmitting the same request unchanged will not help.
    The prompt or the input has to change.

    Seen live on both video-transform variants against a synthetic
    test-card input. A structure-preserving restyle of a canonical image
    reproduces it closely enough to read as recitation, which is worth
    knowing before concluding the request was malformed.
    """

    def __init__(
        self,
        operation_name: str,
        raw: Mapping[str, Any],
        *,
        filtered_count: int,
        reasons: tuple[str, ...] = (),
    ) -> None:
        """Initialises the error.

        Args:
            operation_name: The operation whose output was withheld.
            raw: The full operation body, kept for the log.
            filtered_count: How many outputs the filter removed.
            reasons: Whatever the response gave as a reason, if anything.
        """
        joined = ", ".join(reasons)
        detail = f": {joined}" if reasons else ""
        super().__init__(
            f"VPE filtered every output of operation {operation_name}"
            f" ({filtered_count} filtered){detail}. The job ran, so this is"
            " not a malformed request - the prompt or the input has to"
            " change. A structure-preserving edit of a highly recognisable"
            " source can trip a recitation check on its own.",
        )
        self.operation_name = operation_name
        self.filtered_count = filtered_count
        self.reasons = reasons
        self.raw = raw


class VpeMissingOutputError(VpeError):
    """A completed operation carried no output URI.

    Same documented defect as VpeMissingOperationError, seen from the other
    end: the operation finishes clean but names no file. The output folder is
    the only place left to look, and finding it needs a list-by-prefix that
    ``src.common.storage_service.GcsService`` does not expose today.
    """

    def __init__(
        self,
        operation_name: str,
        raw: Mapping[str, Any],
        *,
        hint: str = "",
    ) -> None:
        """Initialises the error.

        Args:
            operation_name: The operation that finished without an output.
            raw: The full operation body, kept for the log.
            hint: A cause worth naming, where one is actually plausible.
                Empty otherwise. The 4K 16-bit PNG defect used to be offered
                unconditionally, which sent the first live investigation of
                a filtered video-transform job hunting an upscaler defect
                that could not possibly apply to it.
        """
        suffix = f" {hint}" if hint else ""
        super().__init__(
            f"VPE operation {operation_name} completed without naming an"
            f" output file. Check the storageUri folder directly.{suffix}",
        )
        self.operation_name = operation_name
        self.raw = raw


class VpeApiError(VpeError):
    """A failure VPE itself reported, in either envelope."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status: str = "",
        operation_name: str = "",
        remedy: str = "",
        reason: str = "",
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own message, verbatim.
            code: ``error.code`` as sent.
            status: ``error.status``, e.g. ``UNAUTHENTICATED``.
            operation_name: Operation the failure arrived on, if any.
            remedy: What the user should change, from the doc's suggested
                resolution.
            reason: ``error.details[].reason``, when the body carries one.
            raw: The full parsed body, for logging and later diagnosis.
        """
        rendered = message.strip() or "VPE reported an unspecified failure."
        if remedy:
            rendered = f"{rendered} {remedy}"
        super().__init__(rendered)
        self.message = message
        self.code = code
        self.status = status
        self.operation_name = operation_name
        self.remedy = remedy
        self.reason = reason
        self.raw = raw or {}


class VpeAuthenticationError(VpeApiError):
    """Credentials were absent, expired or of the wrong type.

    ``ACCESS_TOKEN_TYPE_UNSUPPORTED`` in the documented body means the call
    presented something that is not an OAuth 2 access token - an API key, or
    an identity token minted for a different audience.
    """


class VpeBucketPermissionError(VpeApiError):
    """A service agent cannot reach an input or output bucket.

    Two distinct root causes share this code. Either the bucket lives outside
    the allowlisted project, or the three service agents never received
    ``roles/storage.admin`` - and two of them do not exist until a batch
    prediction and a tuning job have each been started once in the project.
    """


class VpeInputRejectedError(VpeApiError):
    """The submitted media does not meet the capability's input spec.

    Base for every documented ``code: 3`` media rejection, so a caller can
    tell "the user must supply different media" from "the service failed"
    with one isinstance check.
    """


class VpeVideoTooLongError(VpeInputRejectedError):
    """The input clip exceeds the capability's maximum duration."""

    def __init__(
        self,
        message: str,
        *,
        actual_seconds: float | None = None,
        limit_seconds: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own message, verbatim.
            actual_seconds: Duration the service measured.
            limit_seconds: Maximum the capability accepts.
            **kwargs: Passed through to VpeApiError.
        """
        super().__init__(message, **kwargs)
        self.actual_seconds = actual_seconds
        self.limit_seconds = limit_seconds


class VpeVideoTooShortError(VpeInputRejectedError):
    """The input clip is below the capability's minimum duration."""

    def __init__(
        self,
        message: str,
        *,
        actual_seconds: float | None = None,
        limit_seconds: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own message, verbatim.
            actual_seconds: Duration the service measured.
            limit_seconds: Minimum the capability accepts.
            **kwargs: Passed through to VpeApiError.
        """
        super().__init__(message, **kwargs)
        self.actual_seconds = actual_seconds
        self.limit_seconds = limit_seconds


class VpeFrameRateError(VpeInputRejectedError):
    """The input is not exactly 24 fps."""

    def __init__(
        self,
        message: str,
        *,
        expected_fps: int | None = None,
        actual_fps: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own message, verbatim.
            expected_fps: Frame rate VPE requires, always 24 in the docs.
            actual_fps: Frame rate the service measured.
            **kwargs: Passed through to VpeApiError.
        """
        super().__init__(message, **kwargs)
        self.expected_fps = expected_fps
        self.actual_fps = actual_fps


class VpeAspectRatioMismatchError(VpeInputRejectedError):
    """The declared aspectRatio contradicts the video's real dimensions."""

    def __init__(
        self,
        message: str,
        *,
        requested_aspect_ratio: str = "",
        actual_width: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own message, verbatim.
            requested_aspect_ratio: The parameter value that was sent, e.g.
                ``ASPECT_RATIO_9_16``.
            actual_width: Width the service measured, when the message says.
            **kwargs: Passed through to VpeApiError.
        """
        super().__init__(message, **kwargs)
        self.requested_aspect_ratio = requested_aspect_ratio
        self.actual_width = actual_width


class VpeUnsupportedResolutionError(VpeInputRejectedError):
    """The input frame size is not one the capability accepts."""

    def __init__(
        self,
        message: str,
        *,
        actual_width: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own message, verbatim.
            actual_width: Width the service rejected.
            **kwargs: Passed through to VpeApiError.
        """
        super().__init__(message, **kwargs)
        self.actual_width = actual_width


class VpeMisreportedInputError(VpeInputRejectedError):
    """A high-load reply that preflight says is really a bad input.

    Deliberately not a subclass of VpeServiceOverloadedError: a caller that
    branches on capacity would retry this forever, and the whole point is
    that no amount of retrying fixes an input that is the wrong frame rate.
    """

    def __init__(
        self,
        message: str,
        *,
        preflight: PreflightSnapshot,
        **kwargs: Any,
    ) -> None:
        """Initialises the error.

        Args:
            message: The API's own high-load message, verbatim.
            preflight: The snapshot whose deviations explain the failure.
            **kwargs: Passed through to VpeApiError.
        """
        super().__init__(message, **kwargs)
        self.preflight = preflight


class VpeServiceOverloadedError(VpeApiError):
    """VPE has no capacity right now.

    The only genuinely retryable API failure, and the one the user guide
    flags as intermittent but entirely blocking while it lasts.
    """

    retryable = True


# Suggested resolutions, transcribed from the user guide's troubleshooting
# section. Kept verbatim so a doc update is a one-line diff here, and phrased
# as the guide phrases them rather than being reinterpreted.
_REMEDY_AUTH = (
    "Refresh application default credentials and present an OAuth 2 access"
    " token (gcloud auth application-default print-access-token)."
)
_REMEDY_BUCKET = (
    "The input and output buckets must be in the allowlisted project, and"
    " the three VPE service agents need roles/storage.admin on them."
)
_REMEDY_TOO_LONG = "Shorten or chunk the video to be less than 8 seconds."
_REMEDY_TOO_SHORT = "Increase the video duration to the minimum of 4 seconds."
_REMEDY_FPS = "Change the video frames per second to exactly 24."
_REMEDY_ASPECT = (
    "Match the aspect ratio declared in the parameter configuration to the"
    " provided input video file."
)
_REMEDY_WIDTH = "Rightsize the input video file to 720p."
_REMEDY_HIGH_LOAD = (
    "Wait a few minutes and retry the request. If the error persists, share"
    " the operation ID with your Google team."
)
_REMEDY_INVALID_ARGUMENT = (
    "Check the input media against the capability's documented frame rate,"
    " frame count and frame size before resubmitting."
)

# Patterns match only the part of each documented message that survives the
# doc renderer's right-margin clipping: the permission body stops at "does n"
# and the high-load body at "cannot process yo", so neither can be matched on
# a full sentence. Numbers are captured where present because "your clip is
# 15s, the limit is 8s" is a far more useful thing to show than the code.
_TOO_LONG_RE = re.compile(
    r"duration\s+(?P<actual>[\d.]+)\s+seconds?\s+exceeds\s+the\s+maximum"
    r"\s+duration\s+(?P<limit>[\d.]+)",
    re.IGNORECASE,
)
_TOO_SHORT_RE = re.compile(
    r"duration\s+(?P<actual>[\d.]+)\s+seconds?\s+is\s+less\s+than\s+the"
    r"\s+minimum\s+duration\s+(?P<limit>[\d.]+)",
    re.IGNORECASE,
)
_FPS_RE = re.compile(
    r"fps\s+mismatch\.?\s*expected:\s*(?P<expected>\d+)\s+got:\s*"
    r"(?P<actual>\d+)",
    re.IGNORECASE,
)
_ASPECT_RE = re.compile(
    r"request\s+aspect\s+ratio\s+(?P<ratio>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_WIDTH_RE = re.compile(r"width\s+(?P<width>\d+)", re.IGNORECASE)
_UNSUPPORTED_WIDTH_RE = re.compile(
    r"unsupported\s+video\s+width\s+(?P<width>\d+)",
    re.IGNORECASE,
)
_HIGH_LOAD_RE = re.compile(r"experiencing\s+high\s+load", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"invalid\s+authentication\s+credentials",
    re.IGNORECASE,
)


def _as_int(value: str | None) -> int | None:
    """Parses a captured number, tolerating a clipped or absent match.

    Args:
        value: A regex capture, possibly None.

    Returns:
        The integer value, or None if there was nothing usable to parse.
    """
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _as_float(value: str | None) -> float | None:
    """Parses a captured duration, tolerating a clipped or absent match.

    Args:
        value: A regex capture, possibly None.

    Returns:
        The float value, or None if there was nothing usable to parse.
    """
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def extract_error_body(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    """Pulls the error object out of either VPE failure envelope.

    Args:
        payload: A parsed response body - a rejected request's
            ``{"error": {...}}`` or a completed operation's ``{"name": ...,
            "done": true, "error": {...}}``.

    Returns:
        The error object (empty if the payload reports no failure) and the
        operation name (empty for a rejected request, which never got one).
    """
    error = payload.get("error")
    name = payload.get("name") or ""
    if not isinstance(error, Mapping):
        return {}, str(name)
    return error, str(name)


def _first_reason(error: Mapping[str, Any]) -> str:
    """Returns the first ``details[].reason`` in an error body.

    Args:
        error: The error object from a VPE response.

    Returns:
        The reason string, e.g. ``ACCESS_TOKEN_TYPE_UNSUPPORTED``, or "".
    """
    details = error.get("details")
    if not isinstance(details, list):
        return ""
    for detail in details:
        if isinstance(detail, Mapping) and detail.get("reason"):
            return str(detail["reason"])
    return ""


def classify_error(
    payload: Mapping[str, Any],
    *,
    http_status: int | None = None,
    preflight: PreflightSnapshot | None = None,
) -> VpeApiError:
    """Turns a VPE failure body into a typed, actionable error.

    Matching is by message text first and status code second, because the
    codes do not discriminate: duration, frame rate, aspect ratio and frame
    size rejections all arrive as ``code: 3``, and only the text says which.

    Args:
        payload: The parsed response body, in either failure envelope.
        http_status: Transport status, used when the body omits a code.
        preflight: What preflight measured before submitting. Supplied only
            so a high-load reply on a known off-spec input can be reported
            as the input problem the docs say it may really be.

    Returns:
        The most specific VpeApiError subclass the body supports, falling
        back to VpeApiError itself for an undocumented failure.
    """
    error, operation_name = extract_error_body(payload)
    message = str(error.get("message") or "")
    status = str(error.get("status") or "")
    reason = _first_reason(error)
    code = error.get("code")
    if not isinstance(code, int):
        code = http_status

    common: dict[str, Any] = {
        "code": code,
        "status": status,
        "operation_name": operation_name,
        "reason": reason,
        "raw": payload,
    }

    too_long = _TOO_LONG_RE.search(message)
    if too_long:
        return VpeVideoTooLongError(
            message,
            actual_seconds=_as_float(too_long.group("actual")),
            limit_seconds=_as_float(too_long.group("limit")),
            remedy=_REMEDY_TOO_LONG,
            **common,
        )

    too_short = _TOO_SHORT_RE.search(message)
    if too_short:
        return VpeVideoTooShortError(
            message,
            actual_seconds=_as_float(too_short.group("actual")),
            limit_seconds=_as_float(too_short.group("limit")),
            remedy=_REMEDY_TOO_SHORT,
            **common,
        )

    fps = _FPS_RE.search(message)
    if fps:
        return VpeFrameRateError(
            message,
            expected_fps=_as_int(fps.group("expected")),
            actual_fps=_as_int(fps.group("actual")),
            remedy=_REMEDY_FPS,
            **common,
        )

    aspect = _ASPECT_RE.search(message)
    if aspect:
        width = _WIDTH_RE.search(message)
        return VpeAspectRatioMismatchError(
            message,
            requested_aspect_ratio=aspect.group("ratio"),
            actual_width=_as_int(width.group("width")) if width else None,
            remedy=_REMEDY_ASPECT,
            **common,
        )

    unsupported_width = _UNSUPPORTED_WIDTH_RE.search(message)
    if unsupported_width:
        return VpeUnsupportedResolutionError(
            message,
            actual_width=_as_int(unsupported_width.group("width")),
            remedy=_REMEDY_WIDTH,
            **common,
        )

    if (
        _HIGH_LOAD_RE.search(message)
        or code == VpeStatusCode.RESOURCE_EXHAUSTED
    ):
        return _classify_high_load(message, preflight, common)

    if (
        _AUTH_RE.search(message)
        or status == "UNAUTHENTICATED"
        or reason == "ACCESS_TOKEN_TYPE_UNSUPPORTED"
        or code
        in (
            VpeStatusCode.UNAUTHENTICATED,
            VpeStatusCode.HTTP_UNAUTHORIZED,
        )
    ):
        return VpeAuthenticationError(message, remedy=_REMEDY_AUTH, **common)

    if code == VpeStatusCode.PERMISSION_DENIED or status == "PERMISSION_DENIED":
        return VpeBucketPermissionError(
            message,
            remedy=_REMEDY_BUCKET,
            **common,
        )

    if code == VpeStatusCode.INVALID_ARGUMENT:
        return VpeInputRejectedError(
            message,
            remedy=_REMEDY_INVALID_ARGUMENT,
            **common,
        )

    return VpeApiError(message, **common)


def _classify_high_load(
    message: str,
    preflight: PreflightSnapshot | None,
    common: Mapping[str, Any],
) -> VpeApiError:
    """Decides whether a high-load reply is really a capacity problem.

    Args:
        message: The API's high-load message, verbatim.
        preflight: What preflight measured before submitting, if anything.
        common: Fields shared by every classified error.

    Returns:
        VpeServiceOverloadedError when the input was in spec, and
        VpeMisreportedInputError when it was not.
    """
    if preflight is not None and preflight.has_deviations:
        return VpeMisreportedInputError(
            message,
            preflight=preflight,
            remedy=(
                "VPE reported high load, but this job's input is off-spec"
                f" ({preflight.summary()}), and VPE is documented to report"
                " input violations as high-load errors. Fix the input"
                " before retrying."
            ),
            **common,
        )
    return VpeServiceOverloadedError(
        message,
        remedy=_REMEDY_HIGH_LOAD,
        **common,
    )
