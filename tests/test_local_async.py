"""Local printing with AsyncCatzPrint: the same rules as the sync client."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Callable, Optional

import pytest

from catzprint import (
    AsyncCatzPrint,
    AuthenticationError,
    CatzPrintError,
    FileTokenStore,
    LocalConfig,
    PinMismatchError,
    PrinterError,
    ServerError,
    StationUnreachableError,
    ValidationError,
)
from catzprint._local_core import utcnow
from conftest import KEY, ORG
from localfakes import (
    DEAD_CLOUD,
    FakeCloud,
    FakeResponder,
    FakeStation,
    dead_url,
    endpoint,
    local_cfg,
    mdns_answer,
    token_jti,
)


async def _nosleep(_: float) -> None:
    return None


def aclient(cloud_url: Optional[str], **kw: Any) -> AsyncCatzPrint:
    c = AsyncCatzPrint(KEY, base_url=cloud_url or DEAD_CLOUD, org_id=ORG, max_retries=0, local=local_cfg(**kw))
    c._sleep = _nosleep
    return c


def pins(st: FakeStation) -> Any:
    return {st.url: st.fp}


async def test_async_local_mode(cloud: FakeCloud, station: FakeStation) -> None:
    async with aclient(
        cloud.url, mode="local", location_id="loc1", station_urls=[station.url], fingerprints=pins(station)
    ) as c:
        job = await c.jobs.create(role="kitchen", text="x", copies=2)
        assert job.via == "local" and job.id.startswith("lj_") and job.copies == 2
        key = station.job_requests()[0].body["idempotency_key"]
        assert key.startswith("sdk-") and cloud.mints[0]["location_ids"] == ["loc1"]
        await c.jobs.create(text="again")
        assert cloud.counts() == (1, 0)
        got = await c.jobs.wait_for(job.id, interval=0.01)
        assert got.status == "printed" and got.via == "local"
        st = await c.local.status()
        assert st.unsynced == 3 and st.station_url == station.url


async def test_async_local_first_fallback_same_key(cloud: FakeCloud) -> None:
    dead = dead_url()
    async with aclient(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32}) as c:
        job = await c.jobs.create(text="x", idempotency_key="order-1042-kitchen-r1")
        assert job.via == "cloud" and cloud.last_job()["idempotency_key"] == "order-1042-kitchen-r1"


@pytest.mark.parametrize(
    "status,code", [(503, "printer_offline"), (422, "no_printer"), (402, "offline_allowance_exhausted")]
)
async def test_async_no_fallback(cloud: FakeCloud, station: FakeStation, status: int, code: str) -> None:
    station.fail = lambda path: (status, code) if path == "/local/v1/jobs" else None
    async with aclient(cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        with pytest.raises(CatzPrintError) as ei:
            await c.jobs.create(text="x")
    assert ei.value.code == code and cloud.counts()[1] == 0
    if status == 503:
        assert isinstance(ei.value, PrinterError)


@pytest.mark.parametrize("status,code", [(502, "bad_gateway"), (504, "gateway_timeout"), (503, "local_disabled")])
async def test_async_fallback(cloud: FakeCloud, station: FakeStation, status: int, code: str) -> None:
    station.fail = lambda path: (status, code) if path == "/local/v1/jobs" else None
    async with aclient(cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        job = await c.jobs.create(text="x", idempotency_key="k")
    assert job.via == "cloud" and cloud.last_job()["idempotency_key"] == "k"


async def test_async_cloud_first(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_status, cloud.job_code = 503, "unavailable"
    async with aclient(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        job = await c.jobs.create(text="x")
    assert (
        job.via == "local" and station.job_requests()[0].body["idempotency_key"] == cloud.last_job()["idempotency_key"]
    )
    cloud.job_status, cloud.job_code = 422, "no_printer"
    async with aclient(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        with pytest.raises(ValidationError):
            await c.jobs.create(text="x")
    assert len(station.job_requests()) == 1


async def test_async_cloud_first_both_down(cloud: FakeCloud) -> None:
    cloud.job_status, cloud.job_code = 503, "unavailable"
    dead = dead_url()
    async with aclient(cloud.url, mode="cloud_first", station_urls=[dead], fingerprints={dead: "0" * 64}) as c:
        with pytest.raises(StationUnreachableError) as ei:
            await c.jobs.create(text="x")
    assert isinstance(ei.value.cloud_error, ServerError) and ei.value.urls == [dead]


async def test_async_pin_mismatch_before_request(cloud: FakeCloud, station: FakeStation) -> None:
    async with aclient(cloud.url, mode="local", station_urls=[station.url], fingerprints={station.url: "ab" * 32}) as c:
        with pytest.raises(StationUnreachableError) as ei:
            await c.jobs.create(text="x")
    assert isinstance(ei.value.attempts[0].error, PinMismatchError) and station.requests() == []


async def test_async_remint_rules(cloud: FakeCloud, station: FakeStation, tmp_path: Any) -> None:
    station.accept = lambda tok: token_jti(tok) == "jti-2"
    async with aclient(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station)) as c:
        assert (await c.jobs.create(text="x")).via == "local"
    assert cloud.counts()[0] == 2
    station.accept = lambda tok: False
    station.reject_code = "device_revoked"
    async with aclient(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station)) as c:
        with pytest.raises(AuthenticationError) as ei:
            await c.jobs.create(text="x")
    assert ei.value.code == "device_revoked" and cloud.counts()[0] == 3  # just the one mint
    station.reject_code = "token_expired"
    store = FileTokenStore(os.path.join(str(tmp_path), "t.json"))
    for _ in range(3):
        async with aclient(
            cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station), token_store=store
        ) as c:
            with pytest.raises(AuthenticationError):
                await c.jobs.create(text="x")
    assert cloud.counts()[0] == 5  # one mint + one re-mint for the store key


async def test_async_refresh_on_use(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.stations = [endpoint(station)]
    async with aclient(cloud.url, mode="local", discover=True, discover_timeout=0.05) as c:
        now = utcnow()
        c._local.state.now = lambda: now
        await c.jobs.create(text="x")
        assert cloud.endpoints == 0
        c._local.state.now = lambda: now + timedelta(hours=2)
        await c.jobs.create(text="y")
        assert cloud.endpoints == 1


async def test_async_cancellation_never_falls_back(cloud: FakeCloud, station: FakeStation) -> None:
    """C6: cancelled after the job was fully written -> OutcomeUnknownError wrapping the cancellation."""
    from catzprint import OutcomeUnknownError

    station.delay = 5
    async with aclient(
        cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station), request_timeout=10
    ) as c:
        await c.local.token()
        task = asyncio.ensure_future(c.jobs.create(text="x", idempotency_key="c6"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(OutcomeUnknownError) as ei:
            await task
    assert isinstance(ei.value.cause, asyncio.CancelledError) and ei.value.idempotency_key == "c6"
    assert ei.value.url == station.url and cloud.counts()[1] == 0


async def test_async_cancellation_before_write_is_plain_cancel(cloud: FakeCloud) -> None:
    """C6: cancelled before anything was written (the TLS handshake never completes) -> CancelledError."""
    import socket as _s

    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    url = f"https://127.0.0.1:{srv.getsockname()[1]}"
    try:
        async with aclient(
            cloud.url, mode="local_first", station_urls=[url], fingerprints={url: "ab" * 32}, connect_timeout=10
        ) as c:
            await c.local.token()
            task = asyncio.ensure_future(c.jobs.create(text="x"))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        srv.close()
    assert cloud.counts()[1] == 0


async def test_async_helpers_and_pair(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.stations = [endpoint(station)]
    async with aclient(cloud.url, mode="local", location_id="loc1", discover=True, discover_timeout=0.05) as c:
        await c.local.refresh()  # the endpoint list (with the fingerprint) comes from the cloud
        info = await c.local.info(station.url)
        assert info.station_id == "st1"
        p = await c.local.pair(station.url, "12345678", "Till 3")
        assert p.token == "czd_paired"
        found = await c.local.discover()
        assert found[0].from_cloud and found[0].urls == [station.url]
        lt = await c.local_tokens.create(device_name="Till 9", ttl_days=7)
        await c.local_tokens.revoke(lt.token_id)
        assert cloud.revoked == [lt.token_id]
        assert (await c.local_endpoints.list())[0].station_id == "st1"
        await c.local.refresh()
        with pytest.raises(CatzPrintError) as ei:
            await c.jobs.get("lj_missing")
        assert ei.value.code == "not_found"
    async with aclient(None, mode="cloud") as c2:
        with pytest.raises(ValidationError):
            await c2.local.pair(station.url, "12345678")


async def test_async_mdns_trust(station: FakeStation, responders: Callable[[bytes], FakeResponder]) -> None:
    resp = responders(mdns_answer("Kitchen PC", "st1", ORG, "loc1", station.port, station.fp, "127.0.0.1"))
    async with aclient(
        None, mode="local", location_id="loc1", discover=True, discover_timeout=0.2, token="czd_paired"
    ) as c:
        c._local.state.mdns_addr = resp.addr
        with pytest.raises(StationUnreachableError):
            await c.jobs.create(text="x")
    assert station.requests() == []
    async with aclient(
        None,
        mode="local",
        location_id="loc1",
        discover=True,
        discover_timeout=0.2,
        token="czd_paired",
        fingerprints={"k": station.fp},
    ) as c:
        c._local.state.mdns_addr = resp.addr
        job = await c.jobs.create(text="x")
        assert job.local is not None and job.local.station_url == station.url


async def test_async_paired_device_without_api_key(station: FakeStation, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATZPRINT_API_KEY", "")
    async with AsyncCatzPrint(
        local=LocalConfig(mode="local", station_urls=[station.url], fingerprints=pins(station), token="czd_paired")
    ) as c:
        assert (await c.jobs.create(text="x")).live
        with pytest.raises(AuthenticationError) as ei:
            await c.printers.list()
        assert ei.value.code == "missing_api_key"
