"""HTTPS to CatzPrint stations, with certificate pinning checked before any request byte is sent.

Stations are reached with plain sockets + :mod:`ssl` (never through a proxy) and a small HTTP/1.1
client (one request per connection, ``Connection: close``):

* IP URLs, ``.local`` names and self-signed stations: the chain isn't checked against CAs; instead
  the leaf certificate's SHA-256 must equal the pinned fingerprint. No pin: never contacted.
* Public hostnames (from the cloud endpoint list): normal verification (CA chain + hostname) **and**
  the certificate's SubjectPublicKeyInfo SHA-256 must equal ``public_spki_sha256``. A dashed-IP name
  ``<a-b-c-d>.<label>.<zone>`` under the station's own label is dialled at that IP directly.

The TLS handshake completes, the pin is compared, and only then is the request written.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import ssl
import time
from typing import Any, Dict, Mapping, Optional, Tuple, TypeVar
from urllib.parse import urlencode, urlsplit

from ._errors import APIConnectionError, CatzPrintError, PinMismatchError
from ._local_core import MAX_LOCAL_RESPONSE_BYTES, Target, dashed_ip, parse_ip

__all__ = ["NO_PIN_CODE", "ResponseTooLarge", "StationTransport", "spki_sha256"]

#: Error code of the error recorded for an IP / self-signed URL with no known fingerprint.
NO_PIN_CODE = "no_pinned_fingerprint"
_NO_PIN_MESSAGE = (
    "No pinned certificate fingerprint for this address (IP and self-signed stations are only trusted with "
    "the fingerprint from the cloud endpoint list, mDNS for a listed station, or LocalConfig.fingerprints)."
)
_MAX_HEAD = 64 * 1024


_E = TypeVar("_E", bound=BaseException)


def mark_delivered(err: _E) -> _E:
    """Mark an error as DELIVERED_NO_ANSWER: the request was fully written, then no usable answer came
    (timeout, reset, unreadable or too large body, redirect). The station may have acted on it."""
    err.delivered = True  # type: ignore[attr-defined]
    return err


def is_delivered(err: Optional[BaseException]) -> bool:
    """True for a DELIVERED_NO_ANSWER error (see :func:`mark_delivered`)."""
    return bool(getattr(err, "delivered", False))


class ResponseTooLarge(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("The station's answer is too large.")
        self.status = status


class _BadResponse(Exception):
    pass


# ---------------------------------------------------------------------------- DER


def _tlv(der: bytes, off: int) -> Tuple[int, int, int]:
    """(tag, start of value, end of value) of the DER element at ``off``."""
    if off + 2 > len(der):
        raise ValueError("truncated DER")
    tag = der[off]
    if tag & 0x1F == 0x1F:
        raise ValueError("multi-byte DER tags are not used in certificates")
    n = der[off + 1]
    off += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or off + k > len(der):
            raise ValueError("bad DER length")
        n = int.from_bytes(der[off : off + k], "big")
        off += k
    if off + n > len(der):
        raise ValueError("truncated DER")
    return tag, off, off + n


def spki_der(cert_der: bytes) -> bytes:
    """The SubjectPublicKeyInfo (DER) of an X.509 certificate (DER)."""
    tag, cstart, _ = _tlv(cert_der, 0)
    if tag != 0x30:
        raise ValueError("not a certificate")
    tag, off, tbs_end = _tlv(cert_der, cstart)  # tbsCertificate
    if tag != 0x30:
        raise ValueError("not a certificate")
    tag, _, end = _tlv(cert_der, off)
    if tag == 0xA0:  # [0] EXPLICIT version
        off = end
    # serialNumber, signature, issuer, validity, subject
    for _ in range(5):
        _, _, off = _tlv(cert_der, off)
    tag, _, end = _tlv(cert_der, off)
    if tag != 0x30 or end > tbs_end:
        raise ValueError("no SubjectPublicKeyInfo")
    return cert_der[off:end]


def spki_sha256(cert_der: bytes) -> str:
    """Hex SHA-256 of a certificate's SubjectPublicKeyInfo (what ``public_spki_sha256`` pins)."""
    return hashlib.sha256(spki_der(cert_der)).hexdigest()


# ---------------------------------------------------------------------------- HTTP/1.1 response


class ResponseParser:
    """Incremental HTTP/1.1 response parser (Content-Length, chunked, or read-until-close)."""

    def __init__(self, limit: int = MAX_LOCAL_RESPONSE_BYTES, head: bool = False) -> None:
        self.limit = limit
        self.head = head
        self.buf = bytearray()
        self.status: Optional[int] = None
        self.headers: Dict[str, str] = {}
        self.body = bytearray()
        self.done = False
        self._mode = ""  # length, chunked, eof
        self._left = 0
        self._chunk = "size"  # size, data, crlf, trailer

    def feed(self, data: bytes) -> None:
        self.buf += data
        self._advance()

    def feed_eof(self) -> None:
        if self.done:
            return
        if self.status is not None and self._mode == "eof":
            self.done = True
            return
        raise _BadResponse("The station closed the connection before the full answer.")

    def _take_body(self, b: bytes) -> None:
        self.body += b
        if len(self.body) > self.limit:
            raise ResponseTooLarge(self.status or 0)

    def _advance(self) -> None:
        while not self.done:
            if self.status is None:
                i = self.buf.find(b"\r\n\r\n")
                if i < 0:
                    if len(self.buf) > _MAX_HEAD:
                        raise _BadResponse("The station's response headers are too large.")
                    return
                head = bytes(self.buf[:i]).decode("latin-1")
                del self.buf[: i + 4]
                lines = head.split("\r\n")
                parts = lines[0].split(" ", 2)
                if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
                    raise _BadResponse("The station didn't answer with HTTP.")
                status = int(parts[1])
                if 100 <= status < 200:
                    continue  # interim answer
                headers: Dict[str, str] = {}
                for line in lines[1:]:
                    k, sep, v = line.partition(":")
                    if not sep:
                        continue
                    k = k.strip().lower()
                    v = v.strip()
                    headers[k] = f"{headers[k]}, {v}" if k in headers else v
                self.status, self.headers = status, headers
                if self.head or status in (204, 304):
                    self.done = True
                elif "chunked" in headers.get("transfer-encoding", "").lower():
                    self._mode = "chunked"
                elif "content-length" in headers:
                    try:
                        n = int(headers["content-length"].split(",")[0].strip())
                    except ValueError:
                        raise _BadResponse("The station sent a bad Content-Length.") from None
                    if n < 0:
                        raise _BadResponse("The station sent a bad Content-Length.")
                    if n > self.limit:
                        raise ResponseTooLarge(status)
                    self._mode, self._left = "length", n
                    if n == 0:
                        self.done = True
                else:
                    self._mode = "eof"
                continue
            if self._mode == "eof":
                b = bytes(self.buf)
                self.buf.clear()
                self._take_body(b)
                return
            if self._mode == "length":
                b = bytes(self.buf[: self._left])
                del self.buf[: len(b)]
                self._left -= len(b)
                self._take_body(b)
                if self._left == 0:
                    self.done = True
                return
            # chunked
            if self._chunk == "size":
                i = self.buf.find(b"\r\n")
                if i < 0:
                    if len(self.buf) > 1024:
                        raise _BadResponse("The station sent a bad chunk.")
                    return
                size = bytes(self.buf[:i]).split(b";", 1)[0].strip()
                del self.buf[: i + 2]
                try:
                    n = int(size, 16)
                except ValueError:
                    raise _BadResponse("The station sent a bad chunk.") from None
                if n < 0:
                    raise _BadResponse("The station sent a bad chunk.")
                if n == 0:
                    self._chunk = "trailer"
                else:
                    if len(self.body) + n > self.limit:
                        raise ResponseTooLarge(self.status or 0)
                    self._chunk, self._left = "data", n
            elif self._chunk == "data":
                if not self.buf:
                    return
                b = bytes(self.buf[: self._left])
                del self.buf[: len(b)]
                self._left -= len(b)
                self._take_body(b)
                if self._left == 0:
                    self._chunk = "crlf"
            elif self._chunk == "crlf":
                if len(self.buf) < 2:
                    return
                if self.buf[:2] != b"\r\n":
                    raise _BadResponse("The station sent a bad chunk.")
                del self.buf[:2]
                self._chunk = "size"
            else:  # trailer: lines until an empty one
                i = self.buf.find(b"\r\n")
                if i < 0:
                    if len(self.buf) > _MAX_HEAD:
                        raise _BadResponse("The station sent bad trailers.")
                    return
                trailer = bytes(self.buf[:i])
                del self.buf[: i + 2]
                if not trailer:
                    self.done = True


# ---------------------------------------------------------------------------- transport


class _Plan:
    __slots__ = ("ctx", "dial_host", "host", "netloc", "pin", "port", "spki")

    def __init__(
        self, host: str, port: int, netloc: str, dial_host: str, ctx: ssl.SSLContext, pin: str, spki: str
    ) -> None:
        self.host, self.port, self.netloc, self.dial_host = host, port, netloc, dial_host
        self.ctx, self.pin, self.spki = ctx, pin, spki


def _pinned_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # The chain isn't checked against CAs; instead the leaf certificate must be exactly the pinned one
    # (compared right after the handshake, before anything is written).
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class StationTransport:
    """Builds TLS connections to stations and makes one HTTP request per connection."""

    def __init__(
        self,
        *,
        connect_timeout: float,
        request_timeout: float,
        user_agent: str,
        ca_file: Optional[str] = None,
        ca_data: Optional[str] = None,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.user_agent = user_agent
        self._ca_file = ca_file
        self._ca_data = ca_data
        self._pinned = _pinned_context()
        self._verified: Optional[ssl.SSLContext] = None

    def _verified_context(self) -> ssl.SSLContext:
        if self._verified is None:
            ctx = ssl.create_default_context(cafile=self._ca_file, cadata=self._ca_data)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self._verified = ctx
        return self._verified

    def plan(self, t: Target) -> _Plan:
        """How to connect to ``t``; raises (no request is made) when it can't be trusted."""
        pin = ""
        if t.needs_pin():
            if not t.fingerprint:
                raise CatzPrintError(_NO_PIN_MESSAGE, code=NO_PIN_CODE)
            pin = t.fingerprint
        u = urlsplit(t.url)
        host = (u.hostname or "").lower()
        port = u.port or 443
        dial = host
        if not pin and parse_ip(host) is None and t.label:
            dial = dashed_ip(host, t.label) or host
        ctx = self._pinned if pin else self._verified_context()
        return _Plan(host, port, u.netloc, dial, ctx, pin, "" if pin else t.spki)

    def _request_bytes(
        self, p: _Plan, method: str, path: str, query: Optional[Mapping[str, str]], token: str, body: Any
    ) -> bytes:
        payload = b""
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        target = path + ("?" + urlencode(query) if query else "")
        lines = [
            f"{method} {target} HTTP/1.1",
            f"Host: {p.netloc}",
            f"User-Agent: {self.user_agent}",
            "Accept: application/json",
            "Accept-Encoding: identity",
            "Connection: close",
        ]
        if token:
            lines.append(f"Authorization: Bearer {token}")
        if body is not None:
            lines.append("Content-Type: application/json")
        if body is not None or method in ("POST", "PUT", "PATCH"):
            lines.append(f"Content-Length: {len(payload)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload

    @staticmethod
    def _check_pin(p: _Plan, der: Optional[bytes]) -> None:
        if not der:
            raise APIConnectionError("The station sent no certificate.")
        if p.pin:
            got = hashlib.sha256(der).hexdigest()
            if not hmac.compare_digest(got, p.pin):
                raise PinMismatchError(p.pin, got)
        elif p.spki:
            # Runs after normal verification: the public key must also be the one the cloud reports.
            try:
                got = spki_sha256(der)
            except ValueError as e:
                raise APIConnectionError(f"The station's certificate couldn't be read: {e}") from e
            if not hmac.compare_digest(got, p.spki):
                raise PinMismatchError(p.spki, got, spki=True)

    # -- sync
    def request(
        self,
        t: Target,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        token: str = "",
        body: Any = None,
        extra: float = 0.0,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """One request. Raises APIConnectionError / PinMismatchError / the no-pin error / ResponseTooLarge.

        Errors after the request was fully written are marked delivered (:func:`is_delivered`).
        ``timeout`` (total seconds) replaces ``request_timeout + extra``.
        """
        p = self.plan(t)
        deadline = time.monotonic() + (self.request_timeout + extra if timeout is None else timeout)
        raw: Optional[socket.socket] = None
        sock: Optional[ssl.SSLSocket] = None
        written = False
        try:
            try:
                try:
                    ct = max(0.001, min(self.connect_timeout, deadline - time.monotonic()))
                    raw = socket.create_connection((p.dial_host, p.port), timeout=ct)
                    raw.settimeout(ct)  # bounds the TLS handshake too
                    sock = p.ctx.wrap_socket(raw, server_hostname=p.host)
                    raw = None
                    self._check_pin(p, sock.getpeercert(binary_form=True))
                    sock.settimeout(max(0.001, deadline - time.monotonic()))
                    sock.sendall(self._request_bytes(p, method, path, query, token, body))
                    written = True
                    parser = ResponseParser(head=method == "HEAD")
                    while not parser.done:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise socket.timeout("timed out")
                        sock.settimeout(left)
                        data = sock.recv(65536)
                        if not data:
                            parser.feed_eof()
                            break
                        parser.feed(data)
                except (PinMismatchError, ResponseTooLarge, APIConnectionError):
                    raise
                except _BadResponse as e:
                    raise APIConnectionError(str(e)) from e
                except (socket.timeout, TimeoutError) as e:
                    raise APIConnectionError(f"Timed out talking to the station ({t.url}).") from e
                except ssl.SSLCertVerificationError as e:
                    raise APIConnectionError(f"TLS verification failed: {e.verify_message or e}") from e
                except (OSError, ssl.SSLError, ValueError) as e:
                    raise APIConnectionError(f"{type(e).__name__}: {e}") from e
            except (APIConnectionError, ResponseTooLarge) as e:
                if written:
                    mark_delivered(e)
                raise
        finally:
            if sock is not None:
                sock.close()
            if raw is not None:
                raw.close()
        assert parser.status is not None
        return parser.status, parser.headers, bytes(parser.body)

    # -- async
    async def arequest(
        self,
        t: Target,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        token: str = "",
        body: Any = None,
        extra: float = 0.0,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Async twin of :meth:`request`."""
        p = self.plan(t)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.request_timeout + extra if timeout is None else timeout)
        writer: Optional[asyncio.StreamWriter] = None
        ok = False
        written = False
        try:
            try:
                try:
                    ct = max(0.001, min(self.connect_timeout, deadline - loop.time()))
                    reader, w = await asyncio.wait_for(
                        asyncio.open_connection(
                            p.dial_host, p.port, ssl=p.ctx, server_hostname=p.host, ssl_handshake_timeout=ct
                        ),
                        timeout=ct,
                    )
                    writer = w
                    sslobj = w.get_extra_info("ssl_object")
                    self._check_pin(p, sslobj.getpeercert(binary_form=True) if sslobj is not None else None)
                    w.write(self._request_bytes(p, method, path, query, token, body))
                    await asyncio.wait_for(w.drain(), timeout=max(0.001, deadline - loop.time()))
                    while w.transport.get_write_buffer_size() > 0:  # wait until it is all handed to the OS
                        if loop.time() >= deadline:
                            raise asyncio.TimeoutError()
                        await asyncio.sleep(0.005)
                    written = True
                    parser = ResponseParser(head=method == "HEAD")
                    while not parser.done:
                        left = deadline - loop.time()
                        if left <= 0:
                            raise asyncio.TimeoutError()
                        data = await asyncio.wait_for(reader.read(65536), timeout=left)
                        if not data:
                            parser.feed_eof()
                            break
                        parser.feed(data)
                    ok = True
                except (PinMismatchError, ResponseTooLarge, APIConnectionError):
                    raise
                except _BadResponse as e:
                    raise APIConnectionError(str(e)) from e
                except asyncio.TimeoutError as e:
                    raise APIConnectionError(f"Timed out talking to the station ({t.url}).") from e
                except ssl.SSLCertVerificationError as e:
                    raise APIConnectionError(f"TLS verification failed: {e.verify_message or e}") from e
                except (OSError, ssl.SSLError, ValueError) as e:
                    raise APIConnectionError(f"{type(e).__name__}: {e}") from e
            except (APIConnectionError, ResponseTooLarge, asyncio.CancelledError) as e:
                if written:
                    mark_delivered(e)
                raise
        finally:
            if writer is not None:
                if ok:
                    writer.close()
                else:
                    writer.transport.abort()
        assert parser.status is not None
        return parser.status, parser.headers, bytes(parser.body)
