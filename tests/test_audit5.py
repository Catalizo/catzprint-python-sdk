"""Regression tests for SDK addendum 3 (security audit round 5): R3, R4, C1-C6 (Python, sync + async).

``test_audit5_*`` are the auditor's proofs (scratchpad/audit5/sdk/go/zz_audit5_test.go) ported to Python.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable, List

import pytest

from catzprint import (
    AsyncCatzPrint,
    CatzPrint,
    CatzPrintError,
    ConflictError,
    FileTokenStore,
    OutcomeUnknownError,
    RateLimitError,
    StationUnreachableError,
    StoredToken,
    ValidationError,
)
from conftest import KEY, ORG
from localfakes import DEAD_CLOUD, FakeCloud, FakeStation, dead_url, local_cfg, local_client

BEING_CREATED = (409, "duplicate", "A job with this idempotency key is being created.")
ALREADY_USED = (409, "duplicate", "This idempotency key was already used for another job.")


def pins(*sts: FakeStation) -> Any:
    return {s.url: s.fp for s in sts}


def sleeps_of(c: Any) -> List[float]:
    out: List[float] = []
    c._sleep = out.append
    return out


async def _nosleep(_: float) -> None:
    return None


def aclient(cloud_url: Any, **kw: Any) -> AsyncCatzPrint:
    c = AsyncCatzPrint(KEY, base_url=cloud_url or DEAD_CLOUD, org_id=ORG, max_retries=0, local=local_cfg(**kw))
    c._sleep = _nosleep
    return c


# ---------------------------------------------------------------------------- auditor's proofs (C2 / R1)


@pytest.mark.parametrize("second", [503, 502])
def test_audit5_cloud_500_then_503_goes_local(cloud: FakeCloud, station: FakeStation, second: int) -> None:
    cloud.job_plan = [(500, "internal"), (second, "x")]
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    with pytest.raises(OutcomeUnknownError) as ei:
        c.jobs.create(text="x", idempotency_key="order-1-kitchen-r1")
    assert station.job_requests() == [], "DOUBLE PRINT RISK: sent to the station after an ambiguous 500"
    assert ei.value.idempotency_key == "order-1-kitchen-r1" and len(cloud.jobs) == 2


def test_audit5_local_first_500_then_503(cloud: FakeCloud) -> None:
    dead = dead_url()
    cloud.job_plan = [(500, "x"), (503, "x")]
    c = local_client(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32})
    with pytest.raises(OutcomeUnknownError):  # not wrapped in StationUnreachableError
        c.jobs.create(text="x", idempotency_key="order-2")


@pytest.mark.parametrize("status", [500, 501, 505, 599])
def test_c2_any_other_5xx_is_ambiguous_one_retry(cloud: FakeCloud, station: FakeStation, status: int) -> None:
    cloud.job_plan = [(status, "x")]
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    assert c.jobs.create(text="x", idempotency_key="k").via == "cloud"
    assert [j["idempotency_key"] for j in cloud.jobs] == ["k", "k"] and station.requests() == []


# ---------------------------------------------------------------------------- R3 probe outcomes


def test_r3_probe_409_duplicate_is_an_answer(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2
    a.foreign_keys = {"k409"}
    c = local_client(
        cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3
    )
    with pytest.raises(ConflictError) as ei:
        c.jobs.create(text="x", idempotency_key="k409")
    assert ei.value.code == "duplicate"
    assert len(a.lookups()) == 1 and b.job_requests() == [] and cloud.jobs == []


def test_r3_probe_404_without_not_found_code_is_unknown(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2
    a.lookup_answer = (404, "no_route")  # e.g. an old station or a proxy: not "never recorded"
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3)
    with pytest.raises(OutcomeUnknownError):
        c.jobs.create(text="x", idempotency_key="k")
    assert len(a.lookups()) == 3 and b.job_requests() == []


def test_r3_probe_404_not_found_continues(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2
    a.lookup_answer = (404, "not_found")
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3)
    job = c.jobs.create(text="x", idempotency_key="k")
    assert job.local is not None and job.local.station_url == b.url


async def test_r3_async_probe_409(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2
    a.foreign_keys = {"k"}
    async with aclient(
        cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3
    ) as c:
        with pytest.raises(ConflictError):
            await c.jobs.create(text="x", idempotency_key="k")
    assert b.job_requests() == []


# ---------------------------------------------------------------------------- R4 cloud 409 being created


def test_r4_ambiguous_then_being_created_waits_and_resends(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [(500, "x"), BEING_CREATED, BEING_CREATED]
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    sleeps = sleeps_of(c)
    job = c.jobs.create(text="x", idempotency_key="k")
    assert job.via == "cloud" and len(cloud.jobs) == 4 and sleeps == [1.0, 1.0]
    assert all(j == cloud.jobs[0] for j in cloud.jobs), "re-sends must be identical"
    assert station.requests() == []


def test_r4_still_409_after_three_resends_is_the_answer(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [BEING_CREATED] * 5
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    sleeps = sleeps_of(c)
    with pytest.raises(ConflictError) as ei:
        c.jobs.create(text="x")
    assert ei.value.code == "duplicate" and not isinstance(ei.value, OutcomeUnknownError)
    assert len(cloud.jobs) == 4 and sleeps == [1.0] * 3 and station.requests() == []


@pytest.mark.parametrize("resend", [(503, "unavailable"), (500, "x"), ("hang", 2)])
def test_r4_resend_not_delivered_or_ambiguous_is_unknown(cloud: FakeCloud, station: FakeStation, resend: Any) -> None:
    cloud.job_plan = [BEING_CREATED, resend]
    c = local_client(
        cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), cloud_timeout=0.3
    )
    sleeps_of(c)
    with pytest.raises(OutcomeUnknownError):
        c.jobs.create(text="x")
    assert station.requests() == []


def test_r4_cloud_mode_being_created_resent(cloud: FakeCloud) -> None:
    cloud.job_plan = [BEING_CREATED]
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    sleeps = sleeps_of(c)
    assert c.print_text("x", idempotency_key="k").id == "cloud-1"
    assert sleeps == [1.0] and [j["idempotency_key"] for j in cloud.jobs] == ["k", "k"]
    # Without a key, the generated one is reused for the re-send.
    cloud.job_plan = [BEING_CREATED]
    c.print_text("y")
    assert cloud.jobs[2]["idempotency_key"] == cloud.jobs[3]["idempotency_key"]


def test_r4_resend_is_byte_identical_even_when_sealed(cloud: FakeCloud) -> None:
    pytest.importorskip("nacl")
    cloud.job_plan = [BEING_CREATED]
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    sleeps_of(c)
    c.print_text("secret", sealed=True)
    assert len(cloud.jobs) == 2 and cloud.jobs[0] == cloud.jobs[1]
    assert cloud.jobs[0]["sealed_content"] and cloud.jobs[0]["idempotency_key"]


def test_r4_key_used_for_another_job_is_an_answer(cloud: FakeCloud, station: FakeStation) -> None:
    # Q7(a) (round 6): any 409 duplicate is re-sent 3 times like the other SDKs; still 409 -> the answer.
    cloud.job_plan = [ALREADY_USED] * 4
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    sleeps = sleeps_of(c)
    with pytest.raises(ConflictError):
        c.jobs.create(text="x")
    assert len(cloud.jobs) == 4 and sleeps == [1.0] * 3 and station.requests() == []


async def test_r4_async(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [(500, "x"), BEING_CREATED]
    async with aclient(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        assert (await c.jobs.create(text="x")).via == "cloud"
        cloud.job_plan = [BEING_CREATED, (503, "x")]
        with pytest.raises(OutcomeUnknownError):
            await c.jobs.create(text="y")
        cloud.job_plan = [BEING_CREATED] * 4
        with pytest.raises(ConflictError):
            await c.jobs.create(text="z")
    assert len(cloud.jobs) == 9 and station.requests() == []


# ---------------------------------------------------------------------------- C1 local_first cloud 4xx


@pytest.mark.parametrize(
    "status,code,cls", [(422, "no_printer", ValidationError), (429, "rate_limited", RateLimitError)]
)
def test_c1_local_first_cloud_4xx_is_returned_itself(cloud: FakeCloud, status: int, code: str, cls: type) -> None:
    dead = dead_url()
    cloud.job_status, cloud.job_code = status, code
    c = local_client(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32})
    with pytest.raises(CatzPrintError) as ei:
        c.jobs.create(text="x")
    assert type(ei.value) is cls and not isinstance(ei.value, StationUnreachableError)


async def test_c1_async(cloud: FakeCloud) -> None:
    dead = dead_url()
    cloud.job_status, cloud.job_code = 422, "no_printer"
    async with aclient(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32}) as c:
        with pytest.raises(ValidationError):
            await c.jobs.create(text="x")


# ---------------------------------------------------------------------------- C3 org lookup


def test_c3_org_lookup_failure_is_not_delivered(cloud: FakeCloud, station: FakeStation) -> None:
    # No org_id: GET /v1/key fails (the fake has no such route) before the job POST is sent.
    c = CatzPrint(
        KEY,
        base_url=cloud.url,
        max_retries=0,
        local=local_cfg(mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), token="czd_x"),
    )
    assert c.jobs.create(text="x").via == "local" and cloud.jobs == []
    dead = dead_url()
    c2 = CatzPrint(
        KEY,
        base_url=cloud.url,
        max_retries=0,
        local=local_cfg(mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32}, token="czd_x"),
    )
    with pytest.raises(StationUnreachableError) as ei:
        c2.jobs.create(text="x")
    assert isinstance(ei.value.cloud_error, CatzPrintError) and cloud.jobs == []


async def test_c3_async(cloud: FakeCloud, station: FakeStation) -> None:
    async with AsyncCatzPrint(
        KEY,
        base_url=cloud.url,
        max_retries=0,
        local=local_cfg(mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), token="czd_x"),
    ) as c:
        assert (await c.jobs.create(text="x")).via == "local"


# ---------------------------------------------------------------------------- C4 effective uid


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid"), reason="POSIX")
def test_c4_store_owner_check_uses_effective_uid(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    path = os.path.join(str(tmp_path), "t.json")
    FileTokenStore(path).save("k", StoredToken(token="czl1.a"))
    real = os.geteuid()
    monkeypatch.setattr(os, "getuid", lambda: real)  # the real uid still matches…
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)  # …but the effective one doesn't
    with pytest.raises(CatzPrintError) as ei:
        FileTokenStore(path).load("k")
    assert ei.value.code == "token_store_insecure"


# ---------------------------------------------------------------------------- C5 unreadable 2xx


@pytest.mark.parametrize("garbage", [b"not json", b"{}", b'{"id": ""}', b"[1]"])
def test_c5_station_unreadable_2xx_probes_by_key(
    cloud: FakeCloud, stations: Callable[..., FakeStation], garbage: bytes
) -> None:
    a, b = stations(), stations()
    a.post_garbage = garbage
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b))
    job = c.jobs.create(text="x", idempotency_key="k5")
    assert job.local is not None and job.local.station_url == a.url and job.idempotency_key == "k5"
    assert len(a.lookups()) == 1 and b.job_requests() == []


def test_c5_station_unreadable_2xx_unknown(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_garbage = b"not json"
    a.lookup_answer = (500, "internal")
    c = local_client(cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b))
    with pytest.raises(OutcomeUnknownError):
        c.jobs.create(text="x")
    assert b.job_requests() == [] and cloud.jobs == []


async def test_c5_async_station(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_garbage = b"not json"
    async with aclient(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints=pins(a, b)) as c:
        job = await c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == a.url and b.job_requests() == []


def test_c5_cloud_unreadable_2xx_is_ambiguous(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [("garbage",)]
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    assert c.jobs.create(text="x").via == "cloud" and len(cloud.jobs) == 2
    cloud.job_plan = [("garbage",), ("garbage",)]
    with pytest.raises(OutcomeUnknownError):
        c.jobs.create(text="y")
    assert station.requests() == []
    # Mode cloud: a readable error instead of a KeyError.
    c2 = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    cloud.job_plan = [("garbage",)]
    with pytest.raises(CatzPrintError) as ei:
        c2.print_text("z")
    assert ei.value.code == "invalid_response"


# ---------------------------------------------------------------------------- C6 cancellation (cloud)


async def test_c6_cancel_during_cloud_post_is_outcome_unknown(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [("hang", 3)]
    async with aclient(
        cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), cloud_timeout=10
    ) as c:
        task = asyncio.ensure_future(c.jobs.create(text="x", idempotency_key="c6c"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(OutcomeUnknownError) as ei:
            await task
    assert isinstance(ei.value.cause, asyncio.CancelledError) and ei.value.url == cloud.url
    assert station.requests() == []
