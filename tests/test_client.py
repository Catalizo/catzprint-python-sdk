"""Unit tests for the HTTP layer: errors, retries, idempotency, org lookup, pagination, waiting."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import List

import pytest

import catzprint
from catzprint import (
    APIConnectionError,
    AsyncCatzPrint,
    AuthenticationError,
    CatzPrint,
    CatzPrintError,
    ConflictError,
    NotFoundError,
    OutOfPrintsError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    ValidationError,
    WaitTimeoutError,
)
from catzprint._base import RetryPolicy, build_job
from catzprint._errors import error_from_response
from conftest import KEY, ORG, FakeServer, Recorded, free_port, job_json

JOBS = f"/v1/orgs/{ORG}/jobs"


def make(server: FakeServer, sleeps: List[float], **kw: object) -> CatzPrint:
    c = CatzPrint(KEY, base_url=server.url, org_id=ORG, **kw)  # type: ignore[arg-type]
    c._sleep = sleeps.append
    return c


def err(status: int, code: str, message: str = "msg", **extra: object) -> tuple:  # type: ignore[type-arg]
    e = {"code": code, "message": message}
    e.update(extra)  # type: ignore[arg-type]
    return status, {}, {"error": e}


# ---------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    "status,code,cls",
    [
        (401, "unauthenticated", AuthenticationError),
        (403, "forbidden", PermissionDeniedError),
        (404, "not_found", NotFoundError),
        (400, "invalid", ValidationError),
        (422, "no_printer", ValidationError),
        (402, "out_of_prints", OutOfPrintsError),
        (409, "invalid_state", ConflictError),
        (429, "rate_limited", RateLimitError),
        (500, "internal", ServerError),
        (503, "unavailable", ServerError),
        (418, "teapot", CatzPrintError),
    ],
)
def test_error_mapping(status: int, code: str, cls: type) -> None:
    e = error_from_response(status, {"error": {"code": code, "message": "Nope."}})
    assert type(e) is cls
    assert e.status == status and e.code == code and e.message == "Nope."


def test_out_of_prints_by_code_only() -> None:
    e = error_from_response(403, {"error": {"code": "out_of_prints", "message": "x"}})
    assert isinstance(e, OutOfPrintsError)


def test_validation_fields_and_retry_after() -> None:
    e = error_from_response(
        400, {"error": {"code": "invalid", "message": "Unknown printer.", "fields": {"printer_id": "Unknown printer."}}}
    )
    assert isinstance(e, ValidationError)
    assert e.fields == {"printer_id": "Unknown printer."}
    assert "printer_id" in str(e)
    r = error_from_response(429, {"error": {"code": "rate_limited", "message": "Slow down", "retry_after_seconds": 7}})
    assert r.retry_after == 7
    h = error_from_response(429, {"error": {"code": "rate_limited", "message": "x"}}, {"retry-after": "3"})
    assert h.retry_after == 3


def test_non_json_error_body() -> None:
    e = error_from_response(502, "<html>Bad gateway</html>")
    assert isinstance(e, ServerError) and e.code is None and "Bad gateway" in e.message


def test_spec_aliases() -> None:
    assert catzprint.PermissionError is PermissionDeniedError
    assert catzprint.ConnectionError is APIConnectionError
    assert catzprint.TimeoutError is WaitTimeoutError
    assert issubclass(WaitTimeoutError, TimeoutError)


def test_api_error_raised_from_server(server: FakeServer) -> None:
    server.on("GET", f"{JOBS}/x", *[err(404, "not_found", "Job not found.")])
    with make(server, []) as c, pytest.raises(NotFoundError) as ei:
        c.jobs.get("x")
    assert ei.value.message == "Job not found."


# ---------------------------------------------------------------- key handling


def test_key_never_in_repr(server: FakeServer) -> None:
    c = make(server, [])
    assert KEY not in repr(c)
    assert "cz_test_****" in repr(c)
    assert KEY not in repr(c._config)


def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATZPRINT_API_KEY", raising=False)
    with pytest.raises(AuthenticationError):
        CatzPrint()


def test_env_config(monkeypatch: pytest.MonkeyPatch, server: FakeServer) -> None:
    monkeypatch.setenv("CATZPRINT_API_KEY", KEY)
    monkeypatch.setenv("CATZPRINT_BASE_URL", server.url + "/")
    c = CatzPrint()
    assert c.base_url == server.url and c.is_test_key


def test_headers_and_org_lookup_cached(server: FakeServer) -> None:
    server.on(
        "GET",
        "/v1/key",
        (200, {}, {"id": "k1", "org_id": ORG, "org_name": "Acme", "live": False, "scopes": ["jobs:write"]}),
    )
    server.on(
        "GET",
        f"/v1/orgs/{ORG}/printers",
        (200, {}, {"printers": [{"id": "p1", "name": "Front", "capabilities": ["escpos"]}]}),
    )
    c = CatzPrint(KEY, base_url=server.url)
    info = c.key()
    assert info.org_name == "Acme" and info.scopes == ["jobs:write"] and not info.live
    printers = c.printers.list()
    c.printers.list()
    assert printers[0].capabilities == ["escpos"]
    assert len(server.calls("GET", "/v1/key")) == 1
    req = server.requests[0]
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["user-agent"] == "catzprint-python/2.1.3"


# ---------------------------------------------------------------- retries


def test_get_retried_on_503_then_succeeds(server: FakeServer) -> None:
    server.on(
        "GET",
        f"{JOBS}/j1",
        err(503, "unavailable"),
        err(502, "bad_gateway"),
        (
            200,
            {},
            {
                "job": job_json("j1"),
                "events": [{"at": "2026-10-12T09:14:03Z", "status": "queued", "detail": "", "actor": "API key"}],
            },
        ),
    )
    sleeps: List[float] = []
    job = make(server, sleeps).jobs.get("j1")
    assert job.id == "j1" and job.events[0].status == "queued"
    assert len(server.calls("GET", f"{JOBS}/j1")) == 3
    assert len(sleeps) == 2
    # exponential backoff with jitter: attempt 0 in [0.25, 0.5], attempt 1 in [0.5, 1.0]
    assert 0.25 <= sleeps[0] <= 0.5 and 0.5 <= sleeps[1] <= 1.0


def test_gives_up_after_max_retries(server: FakeServer) -> None:
    server.on("GET", f"{JOBS}/j1", err(504, "timeout"))
    sleeps: List[float] = []
    with pytest.raises(ServerError):
        make(server, sleeps).jobs.get("j1")
    assert len(server.calls("GET", f"{JOBS}/j1")) == 3 and len(sleeps) == 2


def test_500_not_retried(server: FakeServer) -> None:
    server.on("GET", f"{JOBS}/j1", err(500, "internal"))
    with pytest.raises(ServerError):
        make(server, []).jobs.get("j1")
    assert len(server.calls("GET", f"{JOBS}/j1")) == 1


def test_429_respects_retry_after(server: FakeServer) -> None:
    server.on(
        "GET",
        f"/v1/orgs/{ORG}/locations",
        (429, {"Retry-After": "2"}, {"error": {"code": "rate_limited", "message": "x"}}),
        err(429, "rate_limited", retry_after_seconds=5),
        (200, {}, {"locations": [{"id": "l1", "name": "Shop", "stations": 2}]}),
    )
    sleeps: List[float] = []
    locs = make(server, sleeps).locations.list()
    assert locs[0].stations == 2
    assert sleeps == [2.0, 5.0]


def test_429_with_long_retry_after_is_raised(server: FakeServer) -> None:
    server.on("GET", f"/v1/orgs/{ORG}/locations", err(429, "rate_limited", retry_after_seconds=3600))
    sleeps: List[float] = []
    with pytest.raises(RateLimitError) as ei:
        make(server, sleeps).locations.list()
    assert ei.value.retry_after == 3600 and sleeps == []


def test_create_retried_with_same_idempotency_key(server: FakeServer) -> None:
    server.on(
        "POST",
        JOBS,
        err(503, "unavailable"),
        err(429, "rate_limited", retry_after_seconds=1),
        (201, {}, {"job": job_json()}),
    )
    sleeps: List[float] = []
    job = make(server, sleeps).jobs.create(text="hi", printer_id="p1")
    assert job.status == "queued"
    calls = server.calls("POST", JOBS)
    assert len(calls) == 3
    keys = {c.json()["idempotency_key"] for c in calls}
    assert len(keys) == 1
    key = keys.pop()
    assert len(key) == 36  # generated UUIDv4
    assert calls[0].json() == {"printer_id": "p1", "kind": "text", "content": "hi", "idempotency_key": key}


def test_create_uses_caller_idempotency_key(server: FakeServer) -> None:
    server.on("POST", JOBS, err(502, "bad_gateway"), (201, {}, {"job": job_json()}))
    make(server, []).jobs.create(text="hi", idempotency_key="order-1042")
    assert [c.json()["idempotency_key"] for c in server.calls("POST", JOBS)] == ["order-1042"] * 2


def test_post_without_idempotency_never_retried(server: FakeServer) -> None:
    path = f"{JOBS}/j1/cancel"
    server.on("POST", path, err(503, "unavailable"))
    sleeps: List[float] = []
    with pytest.raises(ServerError):
        make(server, sleeps).jobs.cancel("j1")
    assert len(server.calls("POST", path)) == 1 and sleeps == []


def test_network_error_retried_for_get_then_raises() -> None:
    sleeps: List[float] = []
    c = CatzPrint(KEY, base_url=f"http://127.0.0.1:{free_port()}", org_id=ORG, timeout=2)
    c._sleep = sleeps.append
    with pytest.raises(APIConnectionError):
        c.printers.list()
    assert len(sleeps) == 2


def test_network_error_not_retried_for_plain_post() -> None:
    sleeps: List[float] = []
    c = CatzPrint(KEY, base_url=f"http://127.0.0.1:{free_port()}", org_id=ORG, timeout=2)
    c._sleep = sleeps.append
    with pytest.raises(APIConnectionError):
        c.jobs.retry("j1")
    assert sleeps == []


def test_dropped_connection_on_create_is_retried(server: FakeServer) -> None:
    server.on("POST", JOBS, (-1, {}, None), (201, {}, {"job": job_json()}))
    sleeps: List[float] = []
    make(server, sleeps).jobs.create(text="hi")
    calls = server.calls("POST", JOBS)
    assert len(calls) == 2 and calls[0].json()["idempotency_key"] == calls[1].json()["idempotency_key"]


def test_max_retries_zero(server: FakeServer) -> None:
    server.on("GET", f"{JOBS}/j1", err(503, "unavailable"))
    with pytest.raises(ServerError):
        make(server, [], max_retries=0).jobs.get("j1")
    assert len(server.calls("GET", f"{JOBS}/j1")) == 1


def test_retry_policy_bounds() -> None:
    p = RetryPolicy(max_retries=5, backoff_base=1, max_backoff=4)
    for attempt in range(6):
        d = p.delay(attempt)
        assert d <= 4 and d >= min(4, 2**attempt) / 2
    assert p.delay(0, retry_after=9) == 9
    assert not p.should_retry(5, True) and not p.should_retry(0, False) and p.should_retry(0, True)


# ---------------------------------------------------------------- jobs features


def test_list_iter_paginates(server: FakeServer) -> None:
    def handler(rec: Recorded) -> tuple:  # type: ignore[type-arg]
        if "cursor=c2" in rec.query:
            return 200, {}, {"items": [job_json("j3")], "next_cursor": "", "counts": {}}
        return 200, {}, {"items": [job_json("j1"), job_json("j2")], "next_cursor": "c2", "counts": {}}

    server.on("GET", JOBS, handler)
    c = make(server, [])
    ids = [j.id for j in c.jobs.iter(status=["queued", "sent"], page_size=2)]
    assert ids == ["j1", "j2", "j3"]
    assert "status=queued%2Csent" in server.requests[0].query and "limit=2" in server.requests[0].query
    page = c.jobs.list(limit=2)
    assert page.has_more and len(page) == 2
    assert [j.id for j in c.jobs.iter(max_items=1)] == ["j1"]


def test_wait_for_polls_until_final(server: FakeServer) -> None:
    server.on(
        "GET",
        f"{JOBS}/j1",
        (200, {}, {"job": job_json("j1", "queued")}),
        (200, {}, {"job": job_json("j1", "sent")}),
        (200, {}, {"job": job_json("j1", "printed", printed_at="2026-10-12T09:15:00Z")}),
    )
    sleeps: List[float] = []
    job = make(server, sleeps).jobs.wait_for("j1", timeout=30, interval=0.25)
    assert job.status == "printed" and job.is_final and job.printed_at is not None
    assert sleeps == [0.25, 0.25]


def test_wait_for_timeout(server: FakeServer) -> None:
    server.on("GET", f"{JOBS}/j1", (200, {}, {"job": job_json("j1", "queued")}))
    c = make(server, [])
    c._sleep = lambda s: None
    with pytest.raises(WaitTimeoutError) as ei:
        c.jobs.wait_for("j1", timeout=0.05, interval=0.01)
    assert ei.value.job.status == "queued"


def test_reprint_and_retry_bodies(server: FakeServer) -> None:
    server.on("POST", f"{JOBS}/j1/reprint", (201, {}, {"job": job_json("j2", reprint_of="j1")}))
    server.on("POST", f"{JOBS}/j1/retry", (200, {}, {"job": job_json("j1")}))
    c = make(server, [])
    j = c.jobs.reprint("j1", printer_id="p2", copies=2)
    assert j.reprint_of == "j1"
    assert server.calls("POST", f"{JOBS}/j1/reprint")[0].json() == {"printer_id": "p2", "copies": 2}
    c.jobs.retry("j1")


def test_model_tolerates_unknown_fields_and_parses_times() -> None:
    j = catzprint.Job.from_dict(job_json(future_field=1, scheduled_for="2026-10-12T10:00:00+02:00"))
    assert j.raw["future_field"] == 1
    assert j.created_at == datetime(2026, 10, 12, 9, 14, 3, 123456, tzinfo=timezone.utc)
    assert j.scheduled_for is not None and j.scheduled_for.astimezone(timezone.utc).hour == 8


# ---------------------------------------------------------------- request building


def test_build_job_validation() -> None:
    with pytest.raises(ValueError):
        build_job()
    with pytest.raises(ValueError):
        build_job(text="a", commands=[{"type": "cut"}])
    with pytest.raises(ValueError):
        build_job(commands=[{"type": "cut"}], sealed=True)
    with pytest.raises(ValueError):
        build_job(text="a", scheduled_for=datetime(2026, 1, 1))


def test_build_job_shapes() -> None:
    pdf = b"%PDF-1.4 test"
    b = build_job(content=pdf, file_name="a.pdf", copies=2, idempotency_key="k").body
    assert b == {
        "file_name": "a.pdf",
        "copies": 2,
        "idempotency_key": "k",
        "kind": "pdf",
        "content": base64.b64encode(pdf).decode(),
    }
    assert build_job(content=b"\x89PNG....").body["kind"] == "image"
    assert build_job(content=b"PK\x03\x04", file_name="x.docx").body["kind"] == "document"
    t = build_job(template_id="t1", data={"total": "9.50"}, role="receipt").body
    assert t["kind"] == "template" and t["data"] == {"total": "9.50"} and t["role"] == "receipt"
    s = build_job(text="x", scheduled_for=datetime(2026, 1, 1, 12, tzinfo=timezone.utc)).body
    assert s["scheduled_for"] == "2026-01-01T12:00:00Z"
    d = build_job(text="secret", sealed=True)
    assert "content" not in d.body and d.seal_payload == b"secret"
    cmds = build_job(commands=[catzprint.commands.text("Hi", bold=True), catzprint.commands.cut()]).body
    assert cmds["commands"] == [{"type": "text", "text": "Hi", "bold": True}, {"type": "cut"}]


def test_print_pdf_from_path(server: FakeServer, tmp_path: object) -> None:
    import pathlib

    p = pathlib.Path(str(tmp_path)) / "invoice.pdf"
    p.write_bytes(b"%PDF-1.7 hello")
    server.on("POST", JOBS, (201, {}, {"job": job_json(kind="pdf")}))
    make(server, []).print_pdf(p, printer_id="p1")
    body = server.calls("POST", JOBS)[0].json()
    assert body["file_name"] == "invoice.pdf" and body["kind"] == "pdf"
    assert base64.b64decode(body["content"]) == b"%PDF-1.7 hello"


# ---------------------------------------------------------------- async client


async def test_async_retry_and_idempotency(server: FakeServer) -> None:
    server.on("GET", "/v1/key", (200, {}, {"id": "k", "org_id": ORG, "org_name": "Acme", "live": False, "scopes": []}))
    server.on("POST", JOBS, err(503, "unavailable"), (201, {}, {"job": job_json()}))
    sleeps: List[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    async with AsyncCatzPrint(KEY, base_url=server.url) as c:
        c._sleep = fake_sleep
        job = await c.print_receipt([{"type": "text", "text": "hi"}], printer_id="p1")
        assert job.id
        assert (await c.key()).org_name == "Acme"
    calls = server.calls("POST", JOBS)
    assert len(calls) == 2 and calls[0].json() == calls[1].json() and len(sleeps) == 1
    assert len(server.calls("GET", "/v1/key")) == 1


async def test_async_errors_and_iter(server: FakeServer) -> None:
    server.on("POST", f"{JOBS}/j1/cancel", err(409, "invalid_state", "A printed job can't be changed this way."))
    server.on("GET", JOBS, (200, {}, {"items": [job_json("j1")], "next_cursor": "", "counts": {}}))
    server.on("GET", f"{JOBS}/j1", (200, {}, {"job": job_json("j1", "cancelled")}))
    async with AsyncCatzPrint(KEY, base_url=server.url, org_id=ORG) as c:
        with pytest.raises(ConflictError):
            await c.jobs.cancel("j1")
        assert [j.id async for j in c.jobs.iter()] == ["j1"]
        assert (await c.jobs.wait_for("j1")).status == "cancelled"
    assert len(server.calls("POST", f"{JOBS}/j1/cancel")) == 1


async def test_async_connection_error() -> None:
    async def nosleep(_: float) -> None:
        return None

    c = AsyncCatzPrint(KEY, base_url=f"http://127.0.0.1:{free_port()}", org_id=ORG, timeout=2)
    c._sleep = nosleep
    with pytest.raises(APIConnectionError):
        await c.templates.list()
    await c.aclose()


# -- transport hardening ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "http://api-v1.catzprint.com",
        "http://10.0.0.5:8080",
        "ftp://x.test",
        "not a url",
        "https://u:p@x.test",
        "https://x.test/?a=1",
    ],
)
def test_base_url_must_be_https(bad: str) -> None:
    with pytest.raises(ValueError):
        CatzPrint(KEY, base_url=bad)


@pytest.mark.parametrize(
    "ok", ["https://api.example.test", "http://localhost:8080", "http://127.0.0.1:1", "http://[::1]:3000"]
)
def test_base_url_allows_https_and_local_http(ok: str) -> None:
    CatzPrint(KEY, base_url=ok).close()


def test_redirects_are_not_followed(server: FakeServer) -> None:
    server.on("GET", f"/v1/orgs/{ORG}/printers", (302, {"Location": "https://evil.example/steal"}, None))
    with pytest.raises(CatzPrintError) as ei:
        make(server, []).printers.list()
    assert ei.value.code == "unexpected_redirect"
    assert len(server.requests) == 1


def test_oversized_response_is_refused(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    import catzprint._base as base

    monkeypatch.setattr(base, "MAX_RESPONSE_BYTES", 1000)
    server.on("GET", f"/v1/orgs/{ORG}/printers", (200, {}, {"printers": [], "pad": "x" * 5000}))
    with pytest.raises(CatzPrintError) as ei:
        make(server, []).printers.list()
    assert ei.value.code == "response_too_large"


def test_wait_for_zero_interval_does_not_spin(server: FakeServer) -> None:
    server.on(
        "GET",
        f"{JOBS}/j1",
        (200, {}, {"job": job_json(status="queued")}),
        (200, {}, {"job": job_json(status="printed")}),
    )
    sleeps: List[float] = []
    make(server, sleeps).jobs.wait_for("j1", timeout=30, interval=0)
    assert sleeps == [1.0]
