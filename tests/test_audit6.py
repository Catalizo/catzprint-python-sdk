"""Regression tests for SDK addendum 4 (security audit round 6): Q1, Q2, Q4, Q7(a) (Python, sync + async)."""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, Callable, List

import pytest

from catzprint import (
    AsyncCatzPrint,
    CatzPrint,
    CatzPrintError,
    ConflictError,
    FileTokenStore,
    LocalPairing,
    LocalToken,
    MemoryTokenStore,
    OutcomeUnknownError,
    StoredToken,
)
from conftest import KEY, ORG
from localfakes import DEAD_CLOUD, FakeCloud, FakeStation, dead_url, local_cfg, local_client

BEING_CREATED = (409, "duplicate", "A job with this idempotency key is being created.")
PDF = b"%PDF-1.4 fake"


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


# ---------------------------------------------------------------------------- Q1 409 in_progress


def test_q1_probe_in_progress_then_found(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2  # the POST's answer is lost
    a.lookup_in_progress = 1  # the first probe: still being processed
    c = local_client(
        cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3
    )
    job = c.jobs.create(text="x", idempotency_key="q1")
    assert job.local is not None and job.local.station_url == a.url
    assert len(a.lookups()) == 2 and b.job_requests() == [] and cloud.jobs == []


def test_q1_probe_in_progress_forever_is_unknown(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_delay = 2
    a.lookup_in_progress = 99
    c = local_client(
        cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b), request_timeout=0.3
    )
    with pytest.raises(OutcomeUnknownError):
        c.jobs.create(text="x")
    assert len(a.lookups()) == 3 and b.job_requests() == [] and cloud.jobs == []


def test_q1_post_in_progress_probes_until_done(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_in_progress = True  # the job is recorded; the POST itself answers 409 in_progress
    c = local_client(cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b))
    job = c.jobs.create(text="x", idempotency_key="q1b")
    assert job.local is not None and job.local.station_url == a.url and job.idempotency_key == "q1b"
    assert len(a.lookups()) == 1 and b.job_requests() == [] and cloud.jobs == []


def test_q1_post_in_progress_404_is_not_yet(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    """After a 409 in_progress, a 404 not_found means "not recorded yet", not "never": no fallback."""
    a, b = stations(), stations()
    a.post_in_progress = True
    a.post_record = False
    c = local_client(cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b))
    with pytest.raises(OutcomeUnknownError) as ei:
        c.jobs.create(text="x", idempotency_key="q1c")
    assert ei.value.url == a.url and len(a.lookups()) == 3 and b.job_requests() == [] and cloud.jobs == []


def test_q1_post_in_progress_then_duplicate(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_in_progress = True
    a.foreign_keys = {"q1d"}
    c = local_client(cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b))
    with pytest.raises(ConflictError) as ei:
        c.jobs.create(text="x", idempotency_key="q1d")
    assert ei.value.code == "duplicate" and b.job_requests() == [] and cloud.jobs == []


async def test_q1_async(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    a.post_in_progress = True
    async with aclient(cloud.url, mode="local_first", station_urls=[a.url, b.url], fingerprints=pins(a, b)) as c:
        job = await c.jobs.create(text="x")
        assert job.local is not None and job.local.station_url == a.url
        a.post_record = False
        with pytest.raises(OutcomeUnknownError):
            await c.jobs.create(text="y")
    assert b.job_requests() == [] and cloud.jobs == []


# ---------------------------------------------------------------------------- Q4 non-local jobs


@pytest.mark.parametrize("mode", ["local_first", "cloud_first"])
def test_q4_non_local_jobs_use_the_cloud_mode_create(cloud: FakeCloud, station: FakeStation, mode: str) -> None:
    cloud.job_plan = [BEING_CREATED]
    c = local_client(cloud.url, mode=mode, station_urls=[station.url], fingerprints=pins(station))
    sleeps = sleeps_of(c)
    job = c.print_pdf(PDF, idempotency_key="pdf-1")
    assert job.via == "cloud" and sleeps == [1.0] and [j["idempotency_key"] for j in cloud.jobs] == ["pdf-1"] * 2
    assert station.requests() == []


def test_q4_cloud_first_non_local_job_keeps_client_retries(cloud: FakeCloud, station: FakeStation) -> None:
    """Like mode cloud: the client's retries and timeout, not the cloud_first single attempt."""
    cloud.job_plan = [(503, "unavailable")]
    c = CatzPrint(
        KEY,
        base_url=cloud.url,
        org_id=ORG,
        max_retries=1,
        local=local_cfg(mode="cloud_first", station_urls=[station.url], fingerprints=pins(station)),
    )
    c._sleep = lambda s: None
    assert c.print_pdf(PDF).via == "cloud" and len(cloud.jobs) == 2 and station.requests() == []


async def test_q4_async(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [BEING_CREATED]
    async with aclient(cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station)) as c:
        assert (await c.print_pdf(PDF)).via == "cloud"
    assert len(cloud.jobs) == 2 and station.requests() == []


# ---------------------------------------------------------------------------- Q7(a) any 409 duplicate


@pytest.mark.parametrize("message", ["This idempotency key was already used for another job.", "Duplicate."])
def test_q7a_any_409_duplicate_is_resent(cloud: FakeCloud, message: str) -> None:
    dead = dead_url()
    cloud.job_plan = [(409, "duplicate", message)]
    c = local_client(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32})
    sleeps = sleeps_of(c)
    assert c.jobs.create(text="x", idempotency_key="k").via == "cloud"
    assert sleeps == [1.0] and len(cloud.jobs) == 2
    # Mode cloud too.
    cloud.job_plan = [(409, "duplicate", message)] * 4
    c2 = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    sleeps2 = sleeps_of(c2)
    with pytest.raises(ConflictError):
        c2.print_text("y")
    assert sleeps2 == [1.0] * 3


def test_q7a_other_409_codes_are_answers(cloud: FakeCloud) -> None:
    cloud.job_plan = [(409, "invalid_state", "Nope.")]
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0)
    with pytest.raises(ConflictError):
        c.print_text("x")
    assert len(cloud.jobs) == 1


# ---------------------------------------------------------------------------- Q2 serialization


def test_q2_results_serialize_with_the_token() -> None:
    lt = LocalToken.from_dict({"token": "czl1.REAL.SIG", "token_id": "j1", "expires_at": "2030-01-01T00:00:00Z"})
    lp = LocalPairing.from_dict({"device_id": "d_1", "token": "czd_REAL", "station_id": "st1"})
    st = StoredToken(token="czl1.STORED.SIG", token_id="j2")
    # Serialized for a browser POS / persisted: the token is there.
    assert dataclasses.asdict(lt)["token"] == "czl1.REAL.SIG"
    assert json.loads(json.dumps(dataclasses.asdict(lt), default=str))["token"] == "czl1.REAL.SIG"
    assert dataclasses.asdict(lp)["token"] == "czd_REAL" and json.loads(json.dumps(dataclasses.asdict(lp)))["token"]
    assert st.to_dict()["token"] == "czl1.STORED.SIG" and dataclasses.asdict(st)["token"] == "czl1.STORED.SIG"
    assert StoredToken.from_dict(json.loads(json.dumps(st.to_dict()))) == st
    # Only repr/str/format redact.
    for obj in (lt, lp, st):
        for text in (repr(obj), str(obj), f"{obj}", f"{obj!r}"):
            assert "REAL" not in text and "STORED" not in text


# The shared placeholder set (S6, addendum 5).
@pytest.mark.parametrize("placeholder", ["****", "czl1.****", "[REDACTED]", "czd_hidden"])
def test_q2_placeholder_token_in_store_is_no_token(
    cloud: FakeCloud, station: FakeStation, tmp_path: Any, placeholder: str
) -> None:
    path = os.path.join(str(tmp_path), "t.json")
    store = FileTokenStore(path)
    c0 = local_client(
        cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station), token_store=store
    )
    key = c0._local.state.store_key
    with open(path, "w") as f:
        json.dump({key: {"token": placeholder, "expires_at": "2099-01-01T00:00:00Z"}}, f)
    os.chmod(path, 0o600)
    with pytest.raises(CatzPrintError):
        store.load(key)
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station), token_store=store)
    c.jobs.create(text="x")
    assert station.job_requests()[-1].auth != f"Bearer {placeholder}" and cloud.counts()[0] == 1
    assert isinstance(c.local.store_error, CatzPrintError)
    # Any store (not only the file store) is checked.
    mem = MemoryTokenStore()
    c2 = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station), token_store=mem)
    from datetime import datetime, timezone

    mem.save(
        c2._local.state.store_key, StoredToken(token=placeholder, expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
    )
    c2.jobs.create(text="y")
    assert station.job_requests()[-1].auth != f"Bearer {placeholder}" and cloud.counts()[0] == 2
