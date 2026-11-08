"""Regression tests for SDK addendum 2 (security audit round 4): SDK-1, -2, -4, -6, -7, -8, -9.

The ``test_audit4_*`` tests are the auditor's proofs (scratchpad/audit4/sdk) ported to Python: each one
failed before the fix (printed twice, minted on every job, sent a whitespace key…).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from typing import Any, Callable

import pytest

from catzprint import (
    AsyncCatzPrint,
    CatzPrint,
    CatzPrintError,
    FileTokenStore,
    LocalConfig,
    LocalPairing,
    LocalToken,
    OutcomeUnknownError,
    ServerError,
    SignatureVerificationError,
    StationUnreachableError,
    StoredToken,
    ValidationError,
    is_outcome_unknown,
    webhooks,
)
from catzprint._base import decode_body
from catzprint._local import interpret
from catzprint._local_core import token_claims
from conftest import KEY, ORG
from localfakes import DEAD_CLOUD, FakeCloud, FakeStation, local_cfg, local_client

DEEP = b"[" * 200_000 + b"]" * 200_000


def pins(*sts: FakeStation) -> Any:
    return {s.url: s.fp for s in sts}


async def _nosleep(_: float) -> None:
    return None


def aclient(cloud_url: Any, **kw: Any) -> AsyncCatzPrint:
    c = AsyncCatzPrint(KEY, base_url=cloud_url or DEAD_CLOUD, org_id=ORG, max_retries=0, local=local_cfg(**kw))
    c._sleep = _nosleep
    return c


# ---------------------------------------------------------------------------- SDK-1 local path


def test_audit4_ambiguous_timeout_second_station(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    """Auditor's proof: station A got the job but answered too late; the SDK used to post it to station B
    too (printed twice). Now A is asked by key and its job is the result; B never sees it."""
    a, b = stations(), stations()
    a.post_delay = 2
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3)
    job = c.jobs.create(text="x", idempotency_key="order-7")
    assert len(a.job_requests()) == 1 and b.job_requests() == []
    assert job.local is not None and job.local.station_url == a.url and job.idempotency_key == "order-7"
    assert [r.query for r in a.lookups()] == ["idempotency_key=order-7"]
    assert cloud.counts()[1] == 0


def test_not_delivered_tries_next_station_without_lookup(
    cloud: FakeCloud, stations: Callable[..., FakeStation]
) -> None:
    a, b = stations(), stations()
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints={a.url: "ab" * 32, b.url: b.fp})
    job = c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == b.url
    assert a.requests() == []  # pin mismatch: nothing written, nothing asked


def test_delivered_then_404_by_key_tries_next(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.fail = lambda path: (307, "moved") if path == "/local/v1/jobs" else None
    a.fail_method = "POST"  # the job POST is redirected (delivered, no answer); the station never recorded it
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b))
    job = c.jobs.create(text="x", idempotency_key="k404")
    assert job.local is not None and job.local.station_url == b.url
    assert len(a.lookups()) == 1 and len(b.job_requests()) == 1


@pytest.mark.parametrize("mode", ["local", "local_first", "cloud_first"])
def test_delivered_no_answer_is_outcome_unknown_without_fallback(
    cloud: FakeCloud, stations: Callable[..., FakeStation], mode: str
) -> None:
    a, b = stations(), stations()
    a.post_delay = 5
    a.lookup_delay = 5
    if mode == "cloud_first":
        cloud.job_status, cloud.job_code = 503, "unavailable"
    c = local_client(cloud.url, mode=mode, station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3)
    t0 = time.monotonic()
    with pytest.raises(OutcomeUnknownError) as ei:
        c.jobs.create(text="x", idempotency_key="order-8")
    e = ei.value
    assert is_outcome_unknown(e) and e.code == "outcome_unknown" and e.url == a.url and e.idempotency_key == "order-8"
    assert e.message == "The station may have printed this job. Retry later with the same idempotency key."
    assert isinstance(e.cause, CatzPrintError) and e.__cause__ is e.cause
    assert b.job_requests() == []
    assert len(a.lookups()) == 3
    assert time.monotonic() - t0 < 5.5  # 3 lookups within min(5 s, budget)
    assert cloud.counts()[1] == (1 if mode == "cloud_first" else 0)  # no cloud fallback after it


def test_lookup_answer_late_but_in_time(cloud: FakeCloud, station: FakeStation) -> None:
    """A slow first lookup times out, the second one finds the job."""
    station.post_delay = 2
    c = local_client(
        cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station), request_timeout=0.3
    )
    calls = {"n": 0}
    orig = station._serve

    def serve(h: Any) -> None:
        if h.command == "GET" and h.path.startswith("/local/v1/jobs?"):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(1)
                return
        orig(h)

    station._serve = serve  # type: ignore[method-assign]
    job = c.jobs.create(text="x", idempotency_key="k2")
    assert job.via == "local" and calls["n"] == 2 and cloud.counts()[1] == 0


# ---------------------------------------------------------------------------- SDK-1 cloud path


def test_cloud_500_retried_once_with_same_key(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [(500, "internal")]
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    job = c.jobs.create(text="x", idempotency_key="k500")
    assert job.via == "cloud" and [j["idempotency_key"] for j in cloud.jobs] == ["k500", "k500"]
    assert station.requests() == []


@pytest.mark.parametrize("second", [(503, "unavailable"), (500, "internal"), ("hang", 2)])
def test_cloud_ambiguous_twice_is_outcome_unknown_no_local(cloud: FakeCloud, station: FakeStation, second: Any) -> None:
    cloud.job_plan = [(500, "internal"), second]
    c = local_client(
        cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), cloud_timeout=0.3
    )
    with pytest.raises(OutcomeUnknownError) as ei:
        c.jobs.create(text="x", idempotency_key="kc")
    assert ei.value.url == cloud.url and ei.value.idempotency_key == "kc"
    assert len(cloud.jobs) == 2 and station.requests() == []


def test_cloud_ambiguous_then_4xx_answer_is_raised(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [(500, "internal"), (422, "no_printer")]
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    with pytest.raises(ValidationError):
        c.jobs.create(text="x")
    assert station.requests() == []


def test_local_first_cloud_leg_rules(cloud: FakeCloud) -> None:
    from localfakes import dead_url

    dead = dead_url()
    # NOT_DELIVERED / 502-504 on the cloud leg -> StationUnreachable with the cloud error.
    cloud.job_plan = [(502, "bad_gateway")]
    c = local_client(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32})
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    assert isinstance(ei.value.cloud_error, ServerError) and len(cloud.jobs) == 1
    # Ambiguous twice on the cloud leg -> OutcomeUnknown.
    cloud.job_plan = [("hang", 2), ("hang", 2)]
    c2 = CatzPrint(
        KEY,
        base_url=cloud.url,
        org_id=ORG,
        timeout=0.3,
        max_retries=0,
        local=local_cfg(mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32}),
    )
    with pytest.raises(OutcomeUnknownError):
        c2.jobs.create(text="x", idempotency_key="lf")
    assert [j["idempotency_key"] for j in cloud.jobs[1:]] == ["lf", "lf"]


# ---------------------------------------------------------------------------- SDK-1 async


async def test_async_ambiguous_timeout_second_station(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2
    async with aclient(
        cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3
    ) as c:
        job = await c.jobs.create(text="x", idempotency_key="order-7")
    assert job.local is not None and job.local.station_url == a.url and b.job_requests() == []


async def test_async_delivered_404_then_next(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.fail = lambda path: (307, "moved") if path == "/local/v1/jobs" else None
    a.fail_method = "POST"
    async with aclient(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b)) as c:
        job = await c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == b.url and len(a.lookups()) == 1


async def test_async_delivered_no_answer_outcome_unknown(
    cloud: FakeCloud, stations: Callable[..., FakeStation]
) -> None:
    a, b = stations(), stations()
    a.post_delay = a.lookup_delay = 5
    async with aclient(
        cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3
    ) as c:
        with pytest.raises(OutcomeUnknownError) as ei:
            await c.jobs.create(text="x", idempotency_key="ak")
    assert ei.value.url == a.url and b.job_requests() == [] and cloud.counts()[1] == 0 and len(a.lookups()) == 3


async def test_async_cloud_rules(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [(500, "internal")]
    async with aclient(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        assert (await c.jobs.create(text="x")).via == "cloud"
        cloud.job_plan = [(500, "internal"), (503, "unavailable")]
        with pytest.raises(OutcomeUnknownError):
            await c.jobs.create(text="y")
        cloud.job_plan = [(503, "unavailable")]
        assert (await c.jobs.create(text="z")).via == "local"
    assert len(cloud.jobs) == 5 and len(station.job_requests()) == 1


# ---------------------------------------------------------------------------- SDK-2 keys


def test_audit4_whitespace_key(cloud: FakeCloud) -> None:
    """Auditor's proof: a whitespace-only key was sent as is (and retried as "no key" by the server)."""
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    c.jobs.create(text="x", idempotency_key="  ")
    key = cloud.last_job()["idempotency_key"]
    assert key.strip() and len(key) == 36 and not key.startswith("sdk-")  # cloud mode: the usual auto key


def test_whitespace_key_local_modes_get_sdk_key(cloud: FakeCloud, station: FakeStation) -> None:
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station))
    c.jobs.create(text="x", idempotency_key=" \t ")
    assert station.job_requests()[0].body["idempotency_key"].startswith("sdk-")


@pytest.mark.parametrize("bad", [" order-1", "order-1 ", "order\n1", "order\x001", "order\x7f1", "\u0085k"])
def test_keys_with_whitespace_edges_or_control_chars_rejected(cloud: FakeCloud, bad: str) -> None:
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG)
    with pytest.raises(ValidationError) as ei:
        c.jobs.create(text="x", idempotency_key=bad)
    assert ei.value.code == "invalid" and "idempotency_key" in ei.value.fields
    assert cloud.jobs == []


async def test_async_key_validation(cloud: FakeCloud) -> None:
    async with AsyncCatzPrint(KEY, base_url=cloud.url, org_id=ORG) as c:
        with pytest.raises(ValidationError):
            await c.jobs.create(text="x", idempotency_key="k ")
        await c.jobs.create(text="x", idempotency_key="")
    assert len(cloud.jobs) == 1 and cloud.jobs[0]["idempotency_key"]


# ---------------------------------------------------------------------------- SDK-4 secrets


def test_audit4_tokens_in_formatting(station: FakeStation) -> None:
    secret_d, secret_l = "czd_SECRETDEVICETOKEN", "czl1.SECRET.SIG"
    cfg = LocalConfig(mode="local", token=secret_d, station_urls=[station.url], fingerprints=pins(station))
    st = StoredToken(token=secret_l)
    lt = LocalToken.from_dict({"token": "czl1.SECRET2.SIG", "token_id": "j"})
    lp = LocalPairing.from_dict({"device_id": "d", "token": secret_d})
    c = local_client(None, mode="local", token=secret_d, station_urls=[station.url], fingerprints=pins(station))
    station.accept = lambda tok: False
    station.reject_code = "device_revoked"
    with pytest.raises(CatzPrintError) as ei:
        c.jobs.create(text="x")
    for obj in (cfg, st, lt, lp, c, c._local.state.cfg, ei.value):
        for text in (repr(obj), str(obj), f"{obj!r:}"):
            assert "SECRET" not in text, (type(obj), text)
    assert lt.token == "czl1.SECRET2.SIG" and lp.token == secret_d  # still available explicitly


# ---------------------------------------------------------------------------- SDK-6 refresh window


def test_audit4_short_ttl_mints_on_every_job(cloud: FakeCloud, station: FakeStation) -> None:
    """Auditor's proof: with token_ttl_days=3 every job re-minted (3 days left < 7)."""
    from datetime import timedelta

    cloud.expires_in = timedelta(days=3)
    c = local_client(cloud.url, mode="local", token_ttl_days=3, station_urls=[station.url], fingerprints=pins(station))
    for _ in range(10):
        c.jobs.create(text="x")
    assert cloud.counts()[0] == 1
    assert cloud.mints[0]["ttl_days"] == 3


def test_refresh_window_min_7_days_half_ttl() -> None:
    from datetime import timedelta

    from catzprint._local_core import LocalState, resolve_config

    def window(**kw: Any) -> timedelta:
        return LocalState(resolve_config(LocalConfig(**kw)), KEY, lambda: ORG).refresh_window("")

    assert window() == timedelta(days=7)
    assert window(token_ttl_days=30) == timedelta(days=7)
    assert window(token_ttl_days=3) == timedelta(days=1, hours=12)
    assert window(token_ttl_days=1) == timedelta(hours=12)


# ---------------------------------------------------------------------------- SDK-7 packaging


def test_sdist_does_not_ship_tests() -> None:
    try:
        import tomllib  # type: ignore[import-not-found,unused-ignore]
    except ImportError:  # pragma: no cover - Python < 3.11
        pytest.skip("tomllib needs Python 3.11")
    with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as f:
        cfg = tomllib.load(f)
    sdist = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert not any(p.startswith("tests") for p in sdist["include"])
    assert "tests" in sdist.get("exclude", [])


# ---------------------------------------------------------------------------- SDK-8 store permissions


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_store_refuses_group_writable_dir_and_file(tmp_path: Any) -> None:
    d = os.path.join(str(tmp_path), "store")
    os.makedirs(d)
    path = os.path.join(d, "t.json")
    FileTokenStore(path).save("k", StoredToken(token="czl1.a"))
    os.chmod(path, 0o666)
    with pytest.raises(CatzPrintError) as ei:
        FileTokenStore(path).load("k")
    assert ei.value.code == "token_store_insecure"
    with pytest.raises(CatzPrintError):
        FileTokenStore(path).save("k", StoredToken(token="czl1.b"))
    os.chmod(path, 0o600)
    os.chmod(d, 0o777)
    with pytest.raises(CatzPrintError) as ei:
        FileTokenStore(path).load("k")
    assert ei.value.code == "token_store_insecure"
    os.chmod(d, 0o700)
    got = FileTokenStore(path).load("k")
    assert got is not None and got.token == "czl1.a"  # never overwritten while insecure


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to chown"
)
def test_store_refuses_file_owned_by_someone_else(tmp_path: Any) -> None:
    path = os.path.join(str(tmp_path), "t.json")
    FileTokenStore(path).save("k", StoredToken(token="czl1.a"))
    os.chown(path, 4242, 4242)
    with pytest.raises(CatzPrintError) as ei:
        FileTokenStore(path).load("k")
    assert ei.value.code == "token_store_insecure"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_insecure_store_is_reported_not_used(cloud: FakeCloud, station: FakeStation, tmp_path: Any) -> None:
    d = os.path.join(str(tmp_path), "s")
    os.makedirs(d)
    os.chmod(d, 0o777)
    path = os.path.join(d, "t.json")
    with open(path, "w") as f:
        json.dump({"planted": {"token": "czl1.evil"}}, f)
    c = local_client(
        cloud.url,
        mode="local",
        station_urls=[station.url],
        fingerprints=pins(station),
        token_store=FileTokenStore(path),
    )
    c.jobs.create(text="x")  # printing still works (token kept in memory)
    err = c.local.store_error
    assert isinstance(err, CatzPrintError) and err.code == "token_store_insecure"
    with open(path) as f:
        assert json.load(f) == {"planted": {"token": "czl1.evil"}}
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o777


# ---------------------------------------------------------------------------- SDK-9 deep JSON


def test_deep_json_is_a_catzprint_error(tmp_path: Any) -> None:
    with pytest.raises(CatzPrintError) as ei:
        decode_body(DEEP, "application/json")
    assert ei.value.code == "invalid_response"
    unreachable, err, data = interpret(200, {"content-type": "application/json"}, DEEP)
    assert isinstance(err, CatzPrintError) and not unreachable and data is None
    path = os.path.join(str(tmp_path), "t.json")
    with open(path, "wb") as f:
        f.write(DEEP)
    os.chmod(path, 0o600)
    with pytest.raises(CatzPrintError):
        FileTokenStore(path).load("k")
    import base64

    p = base64.urlsafe_b64encode(DEEP).rstrip(b"=").decode()
    assert token_claims(f"czl1.{p}.sig") == {}
    secret = "whsec_test"
    header = webhooks.sign(DEEP, secret)
    with pytest.raises(SignatureVerificationError):
        webhooks.verify(DEEP, header, secret)


def test_deep_json_from_the_cloud(cloud: FakeCloud) -> None:
    orig = cloud._serve

    def serve(h: Any) -> None:
        if h.command == "GET":
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(DEEP)))
            h.end_headers()
            h.wfile.write(DEEP)
            return
        orig(h)

    cloud._serve = serve  # type: ignore[method-assign]
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    with pytest.raises(CatzPrintError) as ei:
        c.printers.list()
    assert ei.value.code == "invalid_response"
