"""Pieces shared by the sync and async clients (no I/O here)."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote, urlsplit

from ._errors import AuthenticationError, CatzPrintError, error_from_response
from ._version import __version__

logger = logging.getLogger("catzprint")

DEFAULT_BASE_URL = "https://api-v1.catzprint.com"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
USER_AGENT = f"catzprint-python/{__version__}"
RETRY_STATUSES = frozenset({429, 502, 503, 504})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Largest response body the SDK reads; a bigger answer is an error, not an OOM.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

JSONDict = Dict[str, Any]
Content = Union[bytes, bytearray, memoryview]


def check_base_url(url: str) -> str:
    """Validate the API base URL and return it without a trailing slash.

    The API key is sent with every request, so the URL must be ``https://``.
    Plain ``http://`` is accepted only for a local server (tests, dev proxies).
    """
    url = (url or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError as e:
        raise ValueError(f"The CatzPrint base URL isn't a valid URL: {url!r}") from e
    if not host:
        raise ValueError(f"The CatzPrint base URL isn't a valid URL: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("The CatzPrint base URL must not contain credentials.")
    if parts.query or parts.fragment:
        raise ValueError("The CatzPrint base URL must not contain a query or fragment.")
    local = host in _LOCAL_HOSTS or host.endswith(".localhost")
    if not (parts.scheme == "https" or (parts.scheme == "http" and local)):
        raise ValueError(
            "The CatzPrint base URL must use https:// (http:// is allowed only for localhost), "
            "so the API key isn't sent in clear text."
        )
    return url


class ResponseTooLarge(Exception):
    """Internal: the response body exceeded MAX_RESPONSE_BYTES."""


def _declared_too_large(headers: Mapping[str, str]) -> bool:
    try:
        return int(headers.get("content-length", "") or -1) > MAX_RESPONSE_BYTES
    except ValueError:
        return False


def read_limited(headers: Mapping[str, str], chunks: Iterator[bytes]) -> bytes:
    """Join a response body, refusing more than MAX_RESPONSE_BYTES (after decompression)."""
    if _declared_too_large(headers):
        raise ResponseTooLarge()
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        if len(buf) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge()
    return bytes(buf)


async def aread_limited(headers: Mapping[str, str], chunks: AsyncIterator[bytes]) -> bytes:
    """Async twin of :func:`read_limited`."""
    if _declared_too_large(headers):
        raise ResponseTooLarge()
    buf = bytearray()
    async for chunk in chunks:
        buf += chunk
        if len(buf) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge()
    return bytes(buf)


def too_large_error(status: int) -> CatzPrintError:
    return CatzPrintError(
        f"The API response is larger than {MAX_RESPONSE_BYTES} bytes.", status=status, code="response_too_large"
    )


def redact_key(key: str) -> str:
    """``cz_live_qfOD…`` -> ``cz_live_****``. Never shows the secret part."""
    for prefix in ("cz_live_", "cz_test_"):
        if key.startswith(prefix):
            return prefix + "****"
    return "****"


@dataclass
class RetryPolicy:
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base: float = 0.5
    max_backoff: float = 8.0
    max_retry_after: float = 60.0

    def delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Seconds to wait before retry number ``attempt + 1`` (exponential backoff + jitter)."""
        if retry_after is not None:
            return max(0.0, retry_after)
        d: float = min(self.max_backoff, self.backoff_base * (2**attempt))
        return d / 2 + random.uniform(0, d / 2)

    def should_retry(self, attempt: int, retryable: bool, retry_after: Optional[float] = None) -> bool:
        if not retryable or attempt >= self.max_retries:
            return False
        return not (retry_after is not None and retry_after > self.max_retry_after)


class ClientConfig:
    """Resolved client settings. The API key is kept out of ``repr``."""

    def __init__(
        self,
        api_key: Optional[str],
        base_url: Optional[str],
        org_id: Optional[str],
        timeout: float,
        max_retries: int,
        backoff_base: float,
        max_backoff: float,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("CATZPRINT_API_KEY", "")
        key = (key or "").strip()
        if not key:
            raise AuthenticationError(
                "No API key. Pass api_key=... or set the CATZPRINT_API_KEY environment variable.",
                code="missing_api_key",
            )
        if not key.startswith(("cz_live_", "cz_test_")):
            logger.warning("CatzPrint API keys start with cz_live_ or cz_test_; check the key you passed.")
        self._api_key = key
        url = base_url or os.environ.get("CATZPRINT_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = check_base_url(url)
        self.org_id = org_id
        self.timeout = timeout
        self.retry = RetryPolicy(max_retries=max(0, max_retries), backoff_base=backoff_base, max_backoff=max_backoff)

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def is_test_key(self) -> bool:
        return self._api_key.startswith("cz_test_")

    def headers(self, auth: bool = True) -> Dict[str, str]:
        h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def __repr__(self) -> str:
        return (
            f"ClientConfig(api_key={redact_key(self._api_key)!r}, base_url={self.base_url!r}, org_id={self.org_id!r})"
        )


def seg(value: str) -> str:
    """Escape one path segment."""
    if not isinstance(value, str) or not value:
        raise ValueError("IDs must be non-empty strings.")
    return quote(value, safe="")


def decode_body(content: bytes, content_type: str) -> Any:
    if not content:
        return None
    if "json" in content_type or content[:1] in (b"{", b"["):
        try:
            return json.loads(content.decode("utf-8"))
        except ValueError:  # bad UTF-8, bad JSON, or an over-long integer literal
            pass
    return content.decode("utf-8", errors="replace")


def api_error(status: int, content: bytes, headers: Mapping[str, str]) -> CatzPrintError:
    return error_from_response(status, decode_body(content, headers.get("content-type", "")), headers)


def format_time(value: Union[datetime, str]) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        raise ValueError("scheduled_for must be timezone-aware (e.g. datetime.now(timezone.utc)).")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def detect_kind(content: bytes, file_name: Optional[str]) -> str:
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"\x89PNG") or content.startswith(b"\xff\xd8\xff"):
        return "image"
    return "document"


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def list_params(
    *,
    status: Union[str, Sequence[str], None] = None,
    printer_id: Optional[str] = None,
    station_id: Optional[str] = None,
    location_id: Optional[str] = None,
    source: Optional[str] = None,
    live: Optional[bool] = None,
    q: Optional[str] = None,
    since: Union[datetime, str, None] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, str]:
    p: Dict[str, str] = {}
    if status:
        p["status"] = status if isinstance(status, str) else ",".join(status)
    for k, v in (
        ("printer_id", printer_id),
        ("station_id", station_id),
        ("location_id", location_id),
        ("source", source),
        ("q", q),
        ("cursor", cursor),
    ):
        if v:
            p[k] = v
    if live is not None:
        p["live"] = "true" if live else "false"
    if since is not None:
        p["since"] = format_time(since)
    if limit is not None:
        p["limit"] = str(int(limit))
    return p


class JobDraft:
    """A validated ``jobs.create`` request that still may need sealing."""

    def __init__(self, body: JSONDict, seal_payload: Optional[bytes]) -> None:
        self.body = body
        self.seal_payload = seal_payload

    def finish(self, sealed_ciphertext: Optional[bytes] = None) -> JSONDict:
        body = dict(self.body)
        if sealed_ciphertext is not None:
            body["sealed_content"] = base64.b64encode(sealed_ciphertext).decode("ascii")
        return body


def build_job(
    *,
    printer_id: Optional[str] = None,
    location_id: Optional[str] = None,
    role: Optional[str] = None,
    kind: Optional[str] = None,
    title: Optional[str] = None,
    reference: Optional[str] = None,
    copies: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    scheduled_for: Union[datetime, str, None] = None,
    options: Optional[Mapping[str, Any]] = None,
    text: Optional[str] = None,
    commands: Optional[Sequence[Mapping[str, Any]]] = None,
    template_id: Optional[str] = None,
    data: Optional[Mapping[str, Any]] = None,
    content: Optional[Content] = None,
    file_name: Optional[str] = None,
    sealed: bool = False,
) -> JobDraft:
    """Validate the arguments of ``jobs.create`` and build the JSON body."""
    sources = [
        n
        for n, v in (("text", text), ("commands", commands), ("template_id", template_id), ("content", content))
        if v is not None
    ]
    if len(sources) != 1:
        raise ValueError("Give exactly one content source: text, commands, template_id (+ data) or content.")
    if data is not None and template_id is None:
        raise ValueError("data is only used with template_id.")
    body: JSONDict = {}
    for k, v in (
        ("printer_id", printer_id),
        ("location_id", location_id),
        ("role", role),
        ("title", title),
        ("reference", reference),
        ("file_name", file_name),
    ):
        if v is not None:
            body[k] = v
    if copies is not None:
        body["copies"] = int(copies)
    if scheduled_for is not None:
        body["scheduled_for"] = format_time(scheduled_for)
    if options is not None:
        body["options"] = dict(options)
    # Always send an idempotency key, so retrying a create is safe.
    body["idempotency_key"] = idempotency_key or new_idempotency_key()

    seal_payload: Optional[bytes] = None
    if text is not None:
        body["kind"] = kind or "text"
        if sealed:
            seal_payload = text.encode("utf-8")
        else:
            body["content"] = text  # text jobs take the text itself, not base64
    elif commands is not None:
        if sealed:
            raise ValueError("Receipt commands can't be sealed; seal text or file content instead.")
        body["kind"] = kind or "commands"
        body["commands"] = [dict(c) for c in commands]
    elif template_id is not None:
        if sealed:
            raise ValueError("Template jobs can't be sealed; seal text or file content instead.")
        body["kind"] = kind or "template"
        body["template_id"] = template_id
        body["data"] = dict(data or {})
    else:
        assert content is not None
        raw = bytes(content)
        body["kind"] = kind or detect_kind(raw, file_name)
        if sealed:
            seal_payload = raw
        elif body["kind"] == "text":
            body["content"] = raw.decode("utf-8")
        else:
            body["content"] = base64.b64encode(raw).decode("ascii")
    return JobDraft(body, seal_payload)


def read_file_arg(
    value: Union[Content, str, os.PathLike[str]], file_name: Optional[str]
) -> Tuple[bytes, Optional[str]]:
    """Accept bytes, or a path (str / PathLike) to read."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), file_name
    path = os.fspath(value)
    with open(path, "rb") as f:
        data = f.read()
    return data, file_name or os.path.basename(path)
