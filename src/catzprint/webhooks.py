"""Webhook signature verification.

CatzPrint signs every delivery with the endpoint's signing secret
(``whsec_…``) and sends::

    CatzPrint-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + raw body)>

Always verify against the *raw* request body: parsing and re-serialising the
JSON changes the bytes and breaks the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Iterable, List, Optional, Sequence, Tuple, Union

from ._errors import SignatureVerificationError
from ._models import WebhookEvent

__all__ = [
    "DEFAULT_TOLERANCE",
    "SIGNATURE_HEADER",
    "Webhooks",
    "construct_event",
    "sign",
    "verify",
    "verify_signature",
]

SIGNATURE_HEADER = "CatzPrint-Signature"
_HEX_SIG = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{1,15}")
DEFAULT_TOLERANCE = 300

Payload = Union[bytes, bytearray, memoryview, str]
Secrets = Union[str, Sequence[str]]


def _to_bytes(payload: Payload) -> bytes:
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return bytes(payload)


def _compute(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(timestamp.encode("ascii") + b".")
    mac.update(body)
    return mac.hexdigest()


def sign(payload: Payload, secret: str, timestamp: Optional[int] = None) -> str:
    """Compute a ``CatzPrint-Signature`` header value, exactly like the server does.

    Useful for testing your webhook handler locally.
    """
    t = str(int(time.time()) if timestamp is None else int(timestamp))
    return f"t={t},v1={_compute(secret, t, _to_bytes(payload))}"


def _parse_header(header: str) -> Tuple[Optional[str], List[str]]:
    ts: Optional[str] = None
    sigs: List[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "t":
            ts = value
        elif key == "v1" and value:
            value = value.lower()
            # Only well-formed hex digests are compared; anything else (including
            # non-ASCII text, which hmac.compare_digest refuses) can never match.
            if _HEX_SIG.fullmatch(value):
                sigs.append(value)
    return ts, sigs


def verify_signature(
    payload: Payload,
    signature_header: Optional[str],
    secret: Secrets,
    *,
    tolerance: Optional[float] = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> None:
    """Check the signature only; raise :class:`SignatureVerificationError` when it doesn't verify.

    ``secret`` may be a list of secrets (e.g. the old and new one while rotating).
    ``tolerance`` is the allowed clock difference in seconds (``None`` disables the check).
    """
    if not signature_header or not isinstance(signature_header, str):
        raise SignatureVerificationError("Missing CatzPrint-Signature header.", header=signature_header)
    secrets: Iterable[str] = [secret] if isinstance(secret, str) else list(secret)
    secrets = [s for s in secrets if s]
    if not secrets:
        raise SignatureVerificationError("No webhook signing secret configured.", header=signature_header)
    ts, sigs = _parse_header(signature_header)
    if ts is None or not _TIMESTAMP.fullmatch(ts):
        raise SignatureVerificationError("Signature header has no valid timestamp.", header=signature_header)
    if not sigs:
        raise SignatureVerificationError("Signature header has no v1 signature.", header=signature_header)
    if tolerance is not None and tolerance != tolerance:  # NaN must not disable the check
        tolerance = DEFAULT_TOLERANCE
    if tolerance is not None:
        current = time.time() if now is None else now
        if abs(current - int(ts)) > tolerance:
            raise SignatureVerificationError(
                "Signature timestamp is outside the tolerance window (possible replay).",
                header=signature_header,
            )
    body = _to_bytes(payload)
    for s in secrets:
        expected = _compute(s, ts, body)
        for given in sigs:
            if hmac.compare_digest(expected, given):
                return
    raise SignatureVerificationError("Signature doesn't match the payload.", header=signature_header)


def verify(
    payload: Payload,
    signature_header: Optional[str],
    secret: Secrets,
    *,
    tolerance: Optional[float] = DEFAULT_TOLERANCE,
    now: Optional[float] = None,
) -> WebhookEvent:
    """Verify a webhook delivery and return the parsed event.

    Raises :class:`SignatureVerificationError` when the header is missing or
    malformed, the timestamp is outside ``tolerance``, or no ``v1`` signature
    matches.
    """
    verify_signature(payload, signature_header, secret, tolerance=tolerance, now=now)
    try:
        data = json.loads(_to_bytes(payload).decode("utf-8"))
    except ValueError as e:  # bad UTF-8, bad JSON, or an over-long integer literal
        raise SignatureVerificationError("Webhook payload isn't valid JSON.", header=signature_header) from e
    if not isinstance(data, dict):
        raise SignatureVerificationError("Webhook payload isn't a JSON object.", header=signature_header)
    return WebhookEvent.from_dict(data)


construct_event = verify
"""Alias of :func:`verify` (familiar to Stripe users)."""


class Webhooks:
    """Webhook helpers, also available as ``client.webhooks``."""

    SIGNATURE_HEADER = SIGNATURE_HEADER

    @staticmethod
    def verify(
        payload: Payload,
        signature_header: Optional[str],
        secret: Secrets,
        *,
        tolerance: Optional[float] = DEFAULT_TOLERANCE,
        now: Optional[float] = None,
    ) -> WebhookEvent:
        return verify(payload, signature_header, secret, tolerance=tolerance, now=now)

    @staticmethod
    def construct_event(
        payload: Payload,
        signature_header: Optional[str],
        secret: Secrets,
        *,
        tolerance: Optional[float] = DEFAULT_TOLERANCE,
        now: Optional[float] = None,
    ) -> WebhookEvent:
        return verify(payload, signature_header, secret, tolerance=tolerance, now=now)

    @staticmethod
    def verify_signature(
        payload: Payload,
        signature_header: Optional[str],
        secret: Secrets,
        *,
        tolerance: Optional[float] = DEFAULT_TOLERANCE,
        now: Optional[float] = None,
    ) -> None:
        verify_signature(payload, signature_header, secret, tolerance=tolerance, now=now)

    @staticmethod
    def sign(payload: Payload, secret: str, timestamp: Optional[int] = None) -> str:
        return sign(payload, secret, timestamp)
