"""Shared test helpers: a tiny scripted HTTP server running in a thread."""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

ORG = "11111111-1111-4111-8111-111111111111"
KEY = "cz_test_unitTESTsecretVALUE_0123456789abcdef"


@dataclass
class Recorded:
    method: str
    path: str
    query: str
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


Response = Tuple[int, Dict[str, str], Any]
Handler = Callable[[Recorded], Response]


@dataclass
class FakeServer:
    url: str
    requests: List[Recorded] = field(default_factory=list)
    # route key "METHOD /path" -> list of responses (consumed in order; last one repeats) or a handler
    routes: Dict[str, Any] = field(default_factory=dict)

    def on(self, method: str, path: str, *responses: Any) -> None:
        if len(responses) == 1 and callable(responses[0]):
            self.routes[f"{method} {path}"] = responses[0]
        else:
            self.routes[f"{method} {path}"] = list(responses)

    def calls(self, method: str, path: str) -> List[Recorded]:
        return [r for r in self.requests if r.method == method and r.path == path]

    def respond(self, rec: Recorded) -> Response:
        self.requests.append(rec)
        route = self.routes.get(f"{rec.method} {rec.path}")
        if route is None:
            return 404, {}, {"error": {"code": "not_found", "message": "No route in fake server."}}
        if callable(route):
            result: Response = route(rec)
            return result
        if len(route) > 1:
            return route.pop(0)  # type: ignore[no-any-return]
        return route[0]  # type: ignore[no-any-return]


def _make_handler(server: FakeServer) -> type:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path, _, query = self.path.partition("?")
            rec = Recorded(self.command, path, query, {k.lower(): v for k, v in self.headers.items()}, body)
            status, headers, payload = server.respond(rec)
            if status == -1:  # simulate a dropped connection
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            raw = (
                payload if isinstance(payload, bytes) else json.dumps(payload).encode() if payload is not None else b""
            )
            self.send_response(status)
            if payload is not None and not isinstance(payload, bytes):
                self.send_header("Content-Type", "application/json")
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _handle
        do_POST = _handle

    return H


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fs = FakeServer(url="")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fs))
    fs.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    try:
        yield fs
    finally:
        httpd.shutdown()
        httpd.server_close()


def job_json(
    job_id: str = "22222222-2222-4222-8222-222222222222", status: str = "queued", **extra: Any
) -> Dict[str, Any]:
    j: Dict[str, Any] = {
        "id": job_id,
        "live": False,
        "source": "api",
        "kind": "escpos",
        "status": status,
        "status_detail": "",
        "title": "",
        "reference": "",
        "copies": 1,
        "printer_id": "33333333-3333-4333-8333-333333333333",
        "printer_name": "Front counter",
        "station_id": None,
        "station_name": None,
        "location_id": None,
        "location_name": None,
        "size": 42,
        "attempts": 0,
        "billed_as": "test",
        "created_by": None,
        "reprint_of": None,
        "scheduled_for": None,
        "created_at": "2026-10-12T09:14:03.123456789Z",
        "sent_at": None,
        "printed_at": None,
        "finished_at": None,
    }
    j.update(extra)
    return j


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def fixture_path() -> Optional[str]:
    return os.environ.get("CATZPRINT_TEST_FIXTURE") or None
