"""Fakes for the local printing tests: a TLS station (spec §4.2), the cloud, and mDNS packets."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import ssl
import struct
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from catzprint import CatzPrint, LocalConfig
from conftest import KEY, ORG, free_port


def _load_certs() -> Dict[str, Dict[str, str]]:
    path = os.path.join(os.path.dirname(__file__), "fixtures", "local-certs.json")
    with open(path, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if isinstance(v, dict)}


CERTS = _load_certs()
PUBLIC_HOST = CERTS["publicLeaf"]["host"]  # 127-0-0-1.abcdefghij.local.catzprint.com
DEAD_CLOUD = "http://127.0.0.1:1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _send_json(h: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload).encode() if payload is not None else b""
    h.send_response(status)
    if payload is not None:
        h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(raw)))
    h.end_headers()
    h.wfile.write(raw)


def _err(h: BaseHTTPRequestHandler, status: int, code: str, message: str = "") -> None:
    _send_json(h, status, {"error": {"code": code, "message": message or f"Station says {code}."}})


# ---------------------------------------------------------------------------- station


@dataclass
class StationReq:
    method: str
    path: str
    query: str
    auth: str
    body: Any


class _TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    ssl_ctx: ssl.SSLContext
    handshakes: int = 0

    def finish_request(self, request: Any, client_address: Any) -> None:
        try:
            request.settimeout(5)
            conn = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        self.handshakes += 1
        try:
            self.RequestHandlerClass(conn, client_address, self)  # type: ignore[arg-type]
        except OSError:
            pass
        finally:
            conn.close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # pin rejections and timeouts are expected


def server_context(cert: Dict[str, str]) -> ssl.SSLContext:
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    with open(cp, "w") as f:
        f.write(cert["cert"])
    with open(kp, "w") as f:
        f.write(cert["key"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    return ctx


class FakeStation:
    """A CatzPrint station's local API over HTTPS on 127.0.0.1."""

    def __init__(self, cert: Optional[Dict[str, str]] = None) -> None:
        self.cert = cert or CERTS["selfSigned"]
        self.fp = self.cert["fingerprint"]
        self.reqs: List[StationReq] = []
        self.lock = threading.Lock()
        self.accept: Optional[Callable[[str], bool]] = None  # None: any czl1./czd_ token
        self.fail: Optional[Callable[[str], Optional[Tuple[int, str]]]] = None
        self.delay = 0.0
        #: Only fail requests with this method (None: any).
        self.fail_method: Optional[str] = None
        #: POST /local/v1/jobs: record the job, then wait this long before answering (a lost answer).
        self.post_delay = 0.0
        #: GET /local/v1/jobs?idempotency_key=…: wait this long before answering.
        self.lookup_delay = 0.0
        #: Keys whose GET /local/v1/jobs?idempotency_key=… lookup answers 409 duplicate (another
        #: caller's job), as the station does since audit round 5.
        self.foreign_keys: Set[str] = set()
        #: POST /local/v1/jobs: record the job, then answer 201 with an unreadable body.
        self.post_garbage = b""
        #: When set, every key lookup answers this (status, code).
        self.lookup_answer: Optional[Tuple[int, str]] = None
        #: POST /local/v1/jobs: record the job (unless post_record is False), then answer 409 in_progress.
        self.post_in_progress = False
        self.post_record = True
        #: POST /local/v1/jobs: read the request, record nothing and drop the connection (sent, no answer).
        self.post_drop = False
        #: The first N key lookups answer 409 in_progress (the POST is still being processed).
        self.lookup_in_progress = 0
        self.reject_code = "token_expired"
        self.live: Optional[bool] = None
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.by_key: Dict[str, str] = {}
        self._stop = threading.Event()
        st = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                st._serve(self)

            do_GET = _handle
            do_POST = _handle

        self.httpd = _TLSServer(("127.0.0.1", 0), H)
        self.httpd.ssl_ctx = server_context(self.cert)
        self.port = self.httpd.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def requests(self) -> List[StationReq]:
        with self.lock:
            return list(self.reqs)

    def job_requests(self) -> List[StationReq]:
        return [r for r in self.requests() if r.method == "POST" and r.path == "/local/v1/jobs"]

    def lookups(self) -> List[StationReq]:
        return [r for r in self.requests() if r.method == "GET" and r.path == "/local/v1/jobs"]

    def _serve(self, h: BaseHTTPRequestHandler) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        u = urlsplit(h.path)
        path = u.path
        with self.lock:
            self.reqs.append(StationReq(h.command, path, u.query, h.headers.get("Authorization") or "", body))
            delay, fail, accept, live = self.delay, self.fail, self.accept, self.live
        if delay and self._stop.wait(delay):
            return
        if fail is not None and self.fail_method in (None, h.command):
            f = fail(path)
            if f:
                return _err(h, f[0], f[1])
        if h.command == "GET" and path == "/local/v1/info":
            return _send_json(
                h,
                200,
                {
                    "station_id": "st1",
                    "name": "Kitchen PC",
                    "org_id": ORG,
                    "location_id": "loc1",
                    "version": "3.0.0",
                    "port": 7443,
                    "tls_mode": "self_signed",
                    "cert_fingerprint_sha256": self.fp,
                    "hostnames": [],
                    "addresses": ["127.0.0.1"],
                    "pairing_open": True,
                    "time": now_iso(),
                },
            )
        if h.command == "POST" and path == "/local/v1/pair":
            if not isinstance(body, dict) or body.get("code") != "12345678":
                return _err(h, 403, "invalid_code")
            return _send_json(
                h,
                201,
                {
                    "device_id": "d_0123456789abcdef",
                    "token": "czd_paired",
                    "station_id": "st1",
                    "org_id": ORG,
                    "location_id": "loc1",
                },
            )
        tok = (h.headers.get("Authorization") or "").removeprefix("Bearer ")
        ok = accept(tok) if accept is not None else tok.startswith(("czl1.", "czd_"))
        if not ok:
            return _err(h, 401, self.reject_code)
        b: Dict[str, Any] = body if isinstance(body, dict) else {}
        if h.command == "POST" and path == "/local/v1/jobs":
            if self.post_drop:
                h.close_connection = True
                with contextlib.suppress(OSError):
                    h.connection.shutdown(socket.SHUT_RDWR)
                return
            key = b.get("idempotency_key") or ""
            with self.lock:
                if key and key in self.by_key:
                    j = self.jobs[self.by_key[key]]
                    return _send_json(h, 200, j)
                jid = f"lj_{len(self.jobs) + 1:016x}"
                waited = "wait" in parse_qs(u.query)
                j = {
                    "id": jid,
                    "idempotency_key": key,
                    "status": "printed" if waited else "queued",
                    "detail": "",
                    "printer_id": "p-kitchen",
                    "printer_name": "Kitchen",
                    "role": b.get("role") or "",
                    "location_id": "loc1",
                    "copies": b.get("copies") or 1,
                    "title": b.get("title") or "",
                    "reference": b.get("reference") or "",
                    "created_at": now_iso(),
                    "finished_at": now_iso() if waited else None,
                    "printed_locally": True,
                    "via": "local",
                    "cloud_job_id": None,
                }
                if live is not None:
                    j["live"] = live
                self.jobs[jid] = j
                if key:
                    self.by_key[key] = jid
            if self.post_in_progress:
                if not self.post_record:
                    with self.lock:
                        self.jobs.pop(jid, None)
                        self.by_key.pop(key, None)
                return _err(h, 409, "in_progress", "A job with this idempotency key is being processed.")
            if self.post_delay and self._stop.wait(self.post_delay):
                return
            if self.post_garbage:
                h.send_response(201)
                h.send_header("Content-Length", str(len(self.post_garbage)))
                h.end_headers()
                h.wfile.write(self.post_garbage)
                return
            return _send_json(h, 201, j)
        if h.command == "GET" and path == "/local/v1/jobs":
            if self.lookup_delay and self._stop.wait(self.lookup_delay):
                return
            key = (parse_qs(u.query).get("idempotency_key") or [""])[0]
            if self.lookup_answer is not None:
                return _err(h, *self.lookup_answer)
            with self.lock:
                busy = self.lookup_in_progress > 0
                if busy:
                    self.lookup_in_progress -= 1
            if busy:
                return _err(h, 409, "in_progress", "Still being processed.")
            if key in self.foreign_keys:
                return _err(h, 409, "duplicate", "This idempotency key was already used for another job.")
            with self.lock:
                jid2 = self.by_key.get(key)
                found = self.jobs.get(jid2) if jid2 else None
            if found is None:
                return _err(h, 404, "not_found")
            return _send_json(h, 200, found)
        if h.command == "GET" and path.startswith("/local/v1/jobs/"):
            with self.lock:
                j2 = self.jobs.get(unquote(path[len("/local/v1/jobs/") :]))
                if j2 is not None:
                    j2.update(status="printed", finished_at=now_iso())
            if j2 is None:
                return _err(h, 404, "not_found")
            return _send_json(h, 200, j2)
        if h.command == "GET" and path == "/local/v1/status":
            return _send_json(
                h,
                200,
                {
                    "station": {
                        "id": "st1",
                        "name": "Kitchen PC",
                        "cloud_connected": False,
                        "last_cloud_contact": "",
                        "version": "3.0.0",
                        "local_enabled": True,
                    },
                    "printers": [
                        {"id": "p-kitchen", "name": "Kitchen", "role": "kitchen", "status": "ready", "paper": "ok"}
                    ],
                    "jobs_waiting": 0,
                    "unsynced": 3,
                    "allowance": {"expires_at": "2026-11-14T00:00:00Z", "prints_left": 1997},
                },
            )
        return _err(h, 404, "not_found")


# ---------------------------------------------------------------------------- cloud


def make_local_token(jti: str, live: bool = True) -> str:
    p = json.dumps(
        {"v": 1, "kid": "k1", "jti": jti, "org": ORG, "key": "key1", "loc": None, "dev": "hub", "live": live}
    ).encode()
    return "czl1." + base64.urlsafe_b64encode(p).rstrip(b"=").decode() + ".c2ln"


def token_jti(tok: str) -> str:
    parts = tok.split(".")
    if len(parts) != 3:
        return ""
    p = parts[1]
    v = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    return str(v.get("jti", ""))


class FakeCloud:
    """The CatzPrint API endpoints local printing uses: local-tokens, local-endpoints, jobs."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mints: List[Dict[str, Any]] = []
        self.jobs: List[Dict[str, Any]] = []
        self.endpoints = 0
        self.revoked: List[str] = []
        self.stations: Optional[List[Dict[str, Any]]] = None
        self.job_status = 0
        self.job_code = ""
        self.down = False
        #: Scripted answers to POST /jobs, consumed in order: (status, code), or ("hang", seconds) to
        #: record the job and answer only after that long, ("drop",) to read the job and close the
        #: connection without an answer, ("seal400",) for a 400 naming sealed_content. Empty: job_status / 201.
        self.job_plan: List[Tuple[Any, ...]] = []
        #: GET /v1/public/server-key requests served.
        self.key_fetches = 0
        self.expires_in = timedelta(days=30)
        cloud = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                cloud._serve(self)

            do_GET = _handle
            do_POST = _handle
            do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def counts(self) -> Tuple[int, int]:
        with self.lock:
            return len(self.mints), len(self.jobs)

    def last_job(self) -> Dict[str, Any]:
        with self.lock:
            return self.jobs[-1] if self.jobs else {}

    def _serve(self, h: BaseHTTPRequestHandler) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        body = json.loads(raw) if raw else {}
        path = urlsplit(h.path).path
        base = f"/v1/orgs/{ORG}"
        hang = 0.0
        with self.lock:
            if self.down:
                return _err(h, 503, "unavailable", "Down.")
            if h.command == "POST" and path == f"{base}/local-tokens":
                self.mints.append(body)
                jti = f"jti-{len(self.mints)}"
                exp = (datetime.now(timezone.utc) + self.expires_in).isoformat().replace("+00:00", "Z")
                return _send_json(
                    h,
                    201,
                    {"token": make_local_token(jti), "token_id": jti, "expires_at": exp, "stations": self.stations},
                )
            if h.command == "DELETE" and path.startswith(f"{base}/local-tokens/"):
                self.revoked.append(path[len(f"{base}/local-tokens/") :])
                return _send_json(h, 204, None)
            if h.command == "GET" and path == "/v1/public/server-key":
                self.key_fetches += 1
                return _send_json(h, 200, {"public_key": base64.b64encode(b"\x09" * 32).decode()})
            if h.command == "GET" and path == f"{base}/local-endpoints":
                self.endpoints += 1
                return _send_json(h, 200, {"stations": self.stations})
            if h.command == "POST" and path == f"{base}/jobs":
                self.jobs.append(body)
                step = self.job_plan.pop(0) if self.job_plan else None
                if step is not None and step[0] == "garbage":
                    h.send_response(201)
                    h.send_header("Content-Length", "8")
                    h.end_headers()
                    h.wfile.write(b"not json")
                    return
                if step is not None and step[0] == "drop":
                    h.close_connection = True
                    with contextlib.suppress(OSError):
                        h.connection.shutdown(socket.SHUT_RDWR)
                    return
                if step is not None and step[0] == "seal400":
                    msg = "Couldn't decrypt."
                    err = {"code": "invalid", "message": msg, "fields": {"sealed_content": msg}}
                    return _send_json(h, 400, {"error": err})
                if step is not None and step[0] != "hang":
                    msg = str(step[2]) if len(step) > 2 else "Cloud says no."
                    return _err(h, int(step[0]), str(step[1]), msg)
                if step is not None:
                    hang = float(step[1])
                elif self.job_status:
                    return _err(h, self.job_status, self.job_code, "Cloud says no.")
                if not hang:
                    return _send_json(
                        h,
                        201,
                        {"job": {"id": "cloud-1", "status": "queued", "idempotency_key": body.get("idempotency_key")}},
                    )
        if h.command == "POST" and path == f"{base}/jobs":
            threading.Event().wait(hang)  # outside the lock: the answer comes too late
            with contextlib.suppress(OSError):
                _send_json(h, 201, {"job": {"id": "cloud-1", "status": "queued"}})
            return
        return _err(h, 404, "not_found", "Nope.")


def endpoint(st: FakeStation, **extra: Any) -> Dict[str, Any]:
    """A LocalEndpoint for a fake station (self-signed, pinned)."""
    ep: Dict[str, Any] = {
        "station_id": "st1",
        "name": "Kitchen PC",
        "location_id": "loc1",
        "connected": True,
        "local_enabled": True,
        "port": st.port,
        "addresses": ["127.0.0.1"],
        "urls": [st.url],
        "tls_mode": "self_signed",
        "cert_fingerprint_sha256": st.fp,
        "updated_at": now_iso(),
    }
    ep.update(extra)
    return ep


def dead_url() -> str:
    return f"https://127.0.0.1:{free_port()}"


def local_cfg(**kw: Any) -> LocalConfig:
    kw.setdefault("connect_timeout", 1.0)
    kw.setdefault("request_timeout", 2.0)
    kw.setdefault("cloud_timeout", 2.0)
    kw.setdefault("device_name", "Hub")
    if not kw.get("location_id"):
        kw.setdefault("all_locations", True)
    return LocalConfig(**kw)


def local_client(cloud_url: Optional[str], **kw: Any) -> CatzPrint:
    c = CatzPrint(KEY, base_url=cloud_url or DEAD_CLOUD, org_id=ORG, max_retries=0, local=local_cfg(**kw))
    c._sleep = lambda s: None
    return c


@pytest.fixture
def cloud() -> Iterator[FakeCloud]:
    fc = FakeCloud()
    try:
        yield fc
    finally:
        fc.close()


@pytest.fixture
def stations() -> Iterator[Callable[..., FakeStation]]:
    made: List[FakeStation] = []

    def make(cert: Optional[Dict[str, str]] = None) -> FakeStation:
        s = FakeStation(cert)
        made.append(s)
        return s

    try:
        yield make
    finally:
        for s in made:
            s.close()


@pytest.fixture
def station(stations: Callable[..., FakeStation]) -> FakeStation:
    return stations()


# ---------------------------------------------------------------------------- mDNS

SERVICE = "_catzprint._tcp.local."
RR = Tuple[str, int, Any]  # (name, type, value)


class _Builder:
    """A DNS message writer with name compression (like real responders)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.names: Dict[str, int] = {}

    def name(self, n: str) -> bytes:
        out = bytearray()
        labels = n.rstrip(".").split(".")
        for i in range(len(labels)):
            suffix = ".".join(labels[i:]).lower()
            if suffix in self.names:
                out += struct.pack("!H", 0xC000 | self.names[suffix])
                return bytes(out)
            pos = len(self.buf) + len(out)
            if pos < 0x3FFF:
                self.names[suffix] = pos
            b = labels[i].encode()
            out.append(len(b))
            out += b
        out.append(0)
        return bytes(out)


def build_response(answers: List[RR], additionals: List[RR], response: bool = True) -> bytes:
    b = _Builder()
    b.buf += struct.pack("!HHHHHH", 0, 0x8400 if response else 0, 0, len(answers), 0, len(additionals))
    for name, rtype, value in answers + additionals:
        b.buf += b.name(name)
        hdr_at = len(b.buf)
        b.buf += b"\0" * 10
        start = len(b.buf)
        if rtype == 12:  # PTR
            b.buf += b.name(value)
        elif rtype == 33:  # SRV
            port, target = value
            b.buf += struct.pack("!HHH", 0, 0, port)
            b.buf += b.name(target)
        elif rtype == 16:  # TXT
            for s in value:
                e = s.encode()
                b.buf += bytes([len(e)]) + e
        elif rtype == 1:  # A
            b.buf += socket.inet_aton(value)
        elif rtype == 28:  # AAAA
            b.buf += socket.inet_pton(socket.AF_INET6, value)
        rdlen = len(b.buf) - start
        b.buf[hdr_at : hdr_at + 10] = struct.pack("!HHIH", rtype, 1 | 0x8000, 120, rdlen)
    return bytes(b.buf)


def mdns_answer(
    name: str, sid: str, org: str, loc: str, port: int, fp: str, ip: str, *extra_txt: str, tls: str = "self_signed"
) -> bytes:
    """A complete station announcement as §4.8 describes it (extra TXT entries go first)."""
    inst = f"{name} (CatzPrint).{SERVICE}"
    host = f"catzprint-{sid or 'x'}.local."
    txt = [
        *extra_txt,
        "v=1",
        f"id={sid}",
        f"org={org}",
        f"loc={loc}",
        f"port={port}",
        f"fp={fp}",
        f"tls={tls}",
        "host=",
    ]
    return build_response(
        [(SERVICE, 12, inst)],
        [(inst, 33, (port, host)), (inst, 16, txt), (host, 1, ip), (host, 28, "::")],
    )


class FakeResponder:
    """Answers every CatzPrint PTR query with ``reply``, unicast to the sender."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.addr: Tuple[str, int] = self.sock.getsockname()
        self.queries = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                data, frm = self.sock.recvfrom(2048)
            except OSError:
                return
            if SERVICE[:-1].encode().split(b".")[0] in data:
                self.queries += 1
                try:
                    self.sock.sendto(self.reply, frm)
                except OSError:
                    return

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def responders() -> Iterator[Callable[[bytes], FakeResponder]]:
    made: List[FakeResponder] = []

    def make(reply: bytes) -> FakeResponder:
        r = FakeResponder(reply)
        made.append(r)
        return r

    try:
        yield make
    finally:
        for r in made:
            r.close()
