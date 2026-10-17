"""Exceptions raised by the CatzPrint SDK."""

from __future__ import annotations

import builtins
import email.utils
import time
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "APIConnectionError",
    "AuthenticationError",
    "CatzPrintError",
    "ConflictError",
    "NotFoundError",
    "OutOfPrintsError",
    "PermissionDeniedError",
    "RateLimitError",
    "ServerError",
    "SignatureVerificationError",
    "ValidationError",
    "WaitTimeoutError",
    "error_from_response",
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
