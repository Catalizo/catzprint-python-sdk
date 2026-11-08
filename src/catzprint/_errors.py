"""Exceptions raised by the CatzPrint SDK."""

from __future__ import annotations

import builtins
import email.utils
import time
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "APIConnectionError",
    "AuthenticationError",
    "CatzPrintError",
    "ConflictError",
    "NotFoundError",
    "OutOfPrintsError",
    "OutcomeUnknownError",
    "PermissionDeniedError",
    "PinMismatchError",
    "PrinterError",
    "RateLimitError",
    "ServerError",
    "SignatureVerificationError",
    "StationAttempt",
    "StationUnreachableError",
    "ValidationError",
    "WaitTimeoutError",
    "error_from_response",
    "is_cover_open",
    "is_offline_allowance_exhausted",
    "is_out_of_paper",
    "is_outcome_unknown",
    "is_printer_offline",
    "is_station_unreachable",
]


class CatzPrintError(Exception):
    """Base class for every error raised by the SDK.

    API errors carry the HTTP ``status``, the stable machine-readable ``code``,
    a plain-English ``message``, per-field ``fields`` messages (validation) and
    ``retry_after`` (seconds) when the API asked the caller to wait.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        fields: Optional[Mapping[str, str]] = None,
        retry_after: Optional[float] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.fields: Dict[str, str] = dict(fields or {})
        self.retry_after = retry_after
        self.body = body

    def __str__(self) -> str:
        parts = []
        if self.status is not None:
            parts.append(str(self.status))
        if self.code:
            parts.append(self.code)
        prefix = " ".join(parts)
        text = f"{prefix}: {self.message}" if prefix else self.message
        if self.fields:
            text += " (" + ", ".join(f"{k}: {v}" for k, v in sorted(self.fields.items())) + ")"
        return text

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, "
            f"message={self.message!r}, fields={self.fields!r}, retry_after={self.retry_after!r})"
        )


class AuthenticationError(CatzPrintError):
    """401: the API key is missing, wrong, revoked or expired."""


class PermissionDeniedError(CatzPrintError):
    """403: the key is valid but lacks the scope (or the job is live and the key is a test key)."""


class NotFoundError(CatzPrintError):
    """404: the job, printer, location or template doesn't exist in this org."""


class ValidationError(CatzPrintError):
    """400/422: the request was rejected; ``fields`` maps field names to messages."""


class OutOfPrintsError(CatzPrintError):
    """402 (code ``out_of_prints``): the organization has no prints left this period."""


class ConflictError(CatzPrintError):
    """409: e.g. the job is in a state that can't be cancelled, or the printer refuses jobs."""


class RateLimitError(CatzPrintError):
    """429: too many requests; ``retry_after`` says how long to wait."""


class ServerError(CatzPrintError):
    """5xx: CatzPrint had a problem. Safe to retry idempotent requests later."""


class PrinterError(CatzPrintError):
    """503 from a CatzPrint station whose printer can't print right now.

    ``code`` is ``printer_offline``, ``out_of_paper``, ``cover_open`` or ``printer_error``. The job was
    not accepted; fix the printer and send it again (the SDK never falls back to the cloud for these).
    """


class APIConnectionError(CatzPrintError, builtins.ConnectionError):
    """The request never got an HTTP answer (DNS, refused connection, TLS, timeout)."""


class WaitTimeoutError(CatzPrintError, builtins.TimeoutError):
    """``jobs.wait_for`` gave up before the job reached a final status.

    ``job`` holds the last state that was seen.
    """

    def __init__(self, message: str, *, job: Any = None) -> None:
        super().__init__(message, code="wait_timeout")
        self.job = job


class SignatureVerificationError(CatzPrintError):
    """A webhook's ``CatzPrint-Signature`` header didn't verify."""

    def __init__(self, message: str, *, header: Optional[str] = None) -> None:
        super().__init__(message, code="signature_verification_failed")
        self.header = header


class PinMismatchError(CatzPrintError):
    """A station presented a certificate whose SHA-256 fingerprint isn't the pinned one.

    ``spki`` is True when the public-key (SubjectPublicKeyInfo) pin of a public hostname failed.
    No request bytes were sent to that station.
    """

    def __init__(self, want: str, got: str, *, spki: bool = False) -> None:
        what = "public key (SPKI) fingerprint" if spki else "certificate fingerprint"
        super().__init__(f"The station's {what} {got} doesn't match the pinned {want}.", code="pin_mismatch")
        self.want = want
        self.got = got
        self.spki = spki


class StationAttempt:
    """One station URL the SDK tried, and why it failed."""

    __slots__ = ("error", "url")

    def __init__(self, url: str, error: BaseException) -> None:
        self.url = url
        self.error = error

    def __repr__(self) -> str:
        return f"StationAttempt(url={self.url!r}, error={self.error!r})"


class StationUnreachableError(CatzPrintError):
    """No CatzPrint station on the local network could take the request.

    None answered, the TLS check failed, the request timed out, a station answered 502/504 or 503
    ``local_disabled``, or no local token could be obtained. ``code`` is ``station_unreachable``;
    ``attempts`` lists what was tried (``urls`` just the URLs). In the fallback modes (``local_first``,
    ``cloud_first``) ``cloud_error`` is the cloud's error when the cloud failed too (it is also the
    exception's ``__cause__``).
    """

    def __init__(
        self, attempts: Optional[List[StationAttempt]] = None, cloud_error: Optional[BaseException] = None
    ) -> None:
        super().__init__("No CatzPrint station could be reached.", code="station_unreachable")
        self.attempts: List[StationAttempt] = list(attempts or [])
        self.cloud_error = cloud_error

    def add(self, url: str, error: BaseException) -> None:
        self.attempts.append(StationAttempt(url, error))

    @property
    def urls(self) -> List[str]:
        """The station URLs that were tried, in order."""
        return [a.url for a in self.attempts if a.url]

    @property
    def message(self) -> str:
        parts = []
        for a in self.attempts:
            why = str(a.error).rstrip(".")
            parts.append(f"{a.url}: {why}" if a.url else why)
        m = "No CatzPrint station could be reached"
        m += ": " + "; ".join(parts) if parts else " (no station address known: set station_urls or discover)"
        if self.cloud_error is not None:
            m += f"; cloud fallback failed too: {str(self.cloud_error).rstrip('.')}"
        return m + "."

    @message.setter
    def message(self, value: str) -> None:  # set by CatzPrintError.__init__; the text is computed
        pass

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def __repr__(self) -> str:
        return f"StationUnreachableError(urls={self.urls!r}, cloud_error={self.cloud_error!r})"


class OutcomeUnknownError(CatzPrintError):
    """The job was fully sent, but no answer came back and its fate couldn't be confirmed.

    ``code`` is ``outcome_unknown``. The station (``url``) or the cloud may have printed or queued it, so
    the SDK did NOT try anywhere else. Retry later with the same ``idempotency_key``: that is always safe
    (stations and the cloud dedupe on it). ``cause`` (also ``__cause__``) is the last error seen.
    """

    MESSAGE = "The station may have printed this job. Retry later with the same idempotency key."

    def __init__(self, url: str, idempotency_key: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(self.MESSAGE, code="outcome_unknown")
        self.url = url
        self.idempotency_key = idempotency_key
        self.cause = cause
        self.__cause__ = cause

    def __repr__(self) -> str:
        return f"OutcomeUnknownError(url={self.url!r}, idempotency_key={self.idempotency_key!r}, cause={self.cause!r})"


_PRINTER_CODES = frozenset({"printer_offline", "out_of_paper", "cover_open", "printer_error"})


def _code(err: object) -> Optional[str]:
    return err.code if isinstance(err, CatzPrintError) else None


def is_printer_offline(err: object) -> bool:
    """A station refused the job because the routed printer is offline."""
    return _code(err) == "printer_offline"


def is_out_of_paper(err: object) -> bool:
    """The printer is out of paper."""
    return _code(err) == "out_of_paper"


def is_cover_open(err: object) -> bool:
    """The printer's cover is open."""
    return _code(err) == "cover_open"


def is_offline_allowance_exhausted(err: object) -> bool:
    """The station has been offline too long or has used its offline prints (402)."""
    return _code(err) == "offline_allowance_exhausted"


def is_outcome_unknown(err: object) -> bool:
    """The job may or may not have printed (an :class:`OutcomeUnknownError`); retry with the same key."""
    return isinstance(err, OutcomeUnknownError)


def is_station_unreachable(err: object) -> bool:
    """No station could be reached (a :class:`StationUnreachableError`)."""
    return isinstance(err, StationUnreachableError)


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:  # pragma: no cover - older Pythons
        return None
    return max(0.0, dt.timestamp() - time.time())


def error_from_response(status: int, body: Any, headers: Optional[Mapping[str, str]] = None) -> CatzPrintError:
    """Build the right exception for an API error response.

    ``body`` is the decoded JSON (``{"error": {"code", "message", "fields", "retry_after_seconds"}}``)
    or raw text when the response wasn't JSON.
    """
    code: Optional[str] = None
    message = ""
    fields: Dict[str, str] = {}
    retry_after: Optional[float] = None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        raw_code, raw_message = err.get("code"), err.get("message")
        code = raw_code if isinstance(raw_code, str) else None
        message = raw_message if isinstance(raw_message, str) else ""
        raw_fields = err.get("fields")
        if isinstance(raw_fields, dict):
            fields = {str(k): str(v) for k, v in raw_fields.items()}
        raw_param = err.get("param")  # stations (and some API errors) name the field in "param"
        if not fields and isinstance(raw_param, str) and raw_param and message:
            fields = {raw_param: message}
        ras = err.get("retry_after_seconds")
        if isinstance(ras, (int, float)) and not isinstance(ras, bool):
            retry_after = float(ras)
    if retry_after is None and headers is not None:
        retry_after = parse_retry_after(headers.get("retry-after") or headers.get("Retry-After"))
    if not message:
        message = body.strip()[:300] if isinstance(body, str) and body.strip() else f"HTTP {status}"

    cls: type = CatzPrintError
    if code == "out_of_prints" or status == 402:
        cls = OutOfPrintsError
    elif code in _PRINTER_CODES:
        cls = PrinterError
    elif status == 401:
        cls = AuthenticationError
    elif status == 403:
        cls = PermissionDeniedError
    elif status == 404:
        cls = NotFoundError
    elif status in (400, 422):
        cls = ValidationError
    elif status == 409:
        cls = ConflictError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = ServerError
    error: CatzPrintError = cls(message, status=status, code=code, fields=fields, retry_after=retry_after, body=body)
    return error
