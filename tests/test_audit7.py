"""Regression tests for SDK addendum 5 (security audit round 7), Python sync + async: S1 (re-seal after a
sealed_content 400, also after an ambiguous attempt: the amb400/seal400 matrix scenarios), S2 (server-key
TTL), S4 (POST 409 in_progress then probe 404: the ip404 scenario) and S6 (placeholder set)."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, List, Optional

import pytest

from catzprint import AsyncCatzPrint, CatzPrint, OutcomeUnknownError, ValidationError
from catzprint._tokenstore import is_redaction_placeholder
from conftest import KEY, ORG
from localfakes import FakeCloud, FakeStation, local_cfg

MODES = ["cloud", "local_first", "cloud_first"]
PDF = b"%PDF-1.4 fake"


async def _nosleep(_: float) -> None:
    return None


def sync_client(cloud: FakeCloud, mode: str, st: FakeStation) -> CatzPrint:
    """max_retries=2 like the matrix's MAXR=2 (mode cloud retries inside the HTTP client)."""
    local = None if mode == "cloud" else local_cfg(mode=mode, station_urls=[st.url], fingerprints={st.url: st.fp})
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=2, local=local)
    c._sleep = lambda s: None
    return c


def async_client(cloud: FakeCloud, mode: str, st: FakeStation) -> AsyncCatzPrint:
    local = None if mode == "cloud" else local_cfg(mode=mode, station_urls=[st.url], fingerprints={st.url: st.fp})
    c = AsyncCatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=2, local=local)
    c._sleep = _nosleep
    return c


def create(variant: str, cloud: FakeCloud, mode: str, st: FakeStation, **kw: Any) -> Any:
    """Run jobs.create with the sync or async client; returns the job or the raised exception."""
    if variant == "sync":
        c = sync_client(cloud, mode, st)
        try:
            return c.jobs.create(**kw)
        except Exception as e:
            return e
        finally:
            c.close()

    async def run() -> Any:
        c = async_client(cloud, mode, st)
        try:
            return await c.jobs.create(**kw)
        except Exception as e:
            return e
        finally:
            await c.aclose()

    return asyncio.run(run())


def posts(cloud: FakeCloud) -> List[dict]:
    with cloud.lock:
        return list(cloud.jobs)


VARIANTS = ["sync", "async"]


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", MODES)
def test_s1_amb400_reseals_and_resends(variant: str, mode: str, cloud: FakeCloud, station: FakeStation) -> None:
    # Before: ValidationError (the caller would print again with a new key).
    cloud.job_plan = [("drop",), ("seal400",)]
    job = create(variant, cloud, mode, station, text="card", sealed=True, idempotency_key="amb-1")
    assert not isinstance(job, Exception), job
    p = posts(cloud)
    assert len(p) == 3 and cloud.key_fetches == 2
    assert p[0]["sealed_content"] == p[1]["sealed_content"] != p[2]["sealed_content"]
    assert p[2]["idempotency_key"] == "amb-1"
    assert station.requests() == []


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("last", ["seal400", "drop"])
def test_s1_amb400_repeat_is_outcome_unknown(
    variant: str, mode: str, last: str, cloud: FakeCloud, station: FakeStation
) -> None:
    cloud.job_plan = [("drop",), ("seal400",)] + [(last,)] * 4
    err = create(variant, cloud, mode, station, text="card", sealed=True, idempotency_key="amb-2")
    assert isinstance(err, OutcomeUnknownError), err
    assert station.requests() == []


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", MODES)
def test_s1_seal400_reseals_once(variant: str, mode: str, cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [("seal400",)]
    job = create(variant, cloud, mode, station, text="card", sealed=True)
    assert not isinstance(job, Exception), job
    assert len(posts(cloud)) == 2 and cloud.key_fetches == 2


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", MODES)
def test_s1_seal400_repeat_is_the_validation_error(
    variant: str, mode: str, cloud: FakeCloud, station: FakeStation
) -> None:
    cloud.job_plan = [("seal400",)] * 3
    err = create(variant, cloud, mode, station, text="card", sealed=True)
    assert isinstance(err, ValidationError) and not isinstance(err, OutcomeUnknownError), err
    assert len(posts(cloud)) == 2 and station.requests() == []


def test_s1_unsealed_400_not_resent(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [("seal400",)] * 3
    err = create("sync", cloud, "cloud", station, content=PDF)
    assert isinstance(err, ValidationError) and len(posts(cloud)) == 1


def test_s1_sealed_400_after_409_is_unknown(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_plan = [(409, "duplicate", "A job with this idempotency key is being created.")] + [("seal400",)] * 4
    err = create("sync", cloud, "cloud", station, text="x", sealed=True)
    assert isinstance(err, OutcomeUnknownError), err


def test_s2_server_key_ttl(cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG)
    c.server_public_key()
    c.server_public_key()
    assert cloud.key_fetches == 1
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 3601)
    c.server_public_key()
    assert cloud.key_fetches == 2
    c.close()


def test_s2_server_key_ttl_async(cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        c = AsyncCatzPrint(KEY, base_url=cloud.url, org_id=ORG)
        await c.server_public_key()
        await c.server_public_key()
        assert cloud.key_fetches == 1
        real = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real() + 3601)
        await c.server_public_key()
        assert cloud.key_fetches == 2
        await c.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", ["local", "local_first", "cloud_first"])
def test_s4_in_progress_then_404_is_unknown(
    variant: str, mode: str, cloud: FakeCloud, stations: Callable[..., FakeStation]
) -> None:
    a, b = stations(), stations()
    a.post_in_progress, a.post_record = True, False  # the probe answers 404 not_found
    if mode == "cloud_first":
        cloud.job_status, cloud.job_code = 503, "unavailable"
    cfg = local_cfg(mode=mode, station_urls=[a.url, b.url], fingerprints={a.url: a.fp, b.url: b.fp})
    err: Optional[BaseException] = None
    if variant == "sync":
        c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0, local=cfg)
        try:
            c.jobs.create(text="x", idempotency_key="ip404")
        except Exception as e:
            err = e
        c.close()
    else:

        async def run() -> Optional[BaseException]:
            ac = AsyncCatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0, local=cfg)
            try:
                await ac.jobs.create(text="x", idempotency_key="ip404")
            except Exception as e:
                return e
            finally:
                await ac.aclose()
            return None

        err = asyncio.run(run())
    assert isinstance(err, OutcomeUnknownError), err
    assert b.requests() == []
    if mode == "local_first":
        assert posts(cloud) == []


def test_s6_placeholder_set() -> None:
    shared = [
        "…",
        "***",
        "*****",
        "[redacted]",
        "REDACTED",
        "hidden",
        "czl1.…",
        "czl1.***",
        "czd_[redacted]",
        "czd_hidden",
    ]
    for p in shared:
        assert is_redaction_placeholder(p), p
    for p in ["czl1.abc.def", "czd_abc", "**", "czl1.**", "redacted-token", ""]:
        assert not is_redaction_placeholder(p), p


# ---------------------------------------------------------------------------- addendum 6 (audit round 8)


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", ["local", "local_first"])
def test_t2_probe_in_progress_then_404_is_unknown(
    variant: str, mode: str, cloud: FakeCloud, stations: Callable[..., FakeStation]
) -> None:
    """dip404: the POST was sent but not recorded (no answer), the first probe says in_progress, the next 404.
    Before: the 404 counted as "never recorded" and the job went to the next station / the cloud."""
    a, b = stations(), stations()
    a.post_drop = True
    a.lookup_in_progress = 1
    cfg = local_cfg(mode=mode, station_urls=[a.url, b.url], fingerprints={a.url: a.fp, b.url: b.fp})

    async def arun() -> Optional[BaseException]:
        ac = AsyncCatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0, local=cfg)
        try:
            await ac.jobs.create(text="x", idempotency_key="dip404")
        except Exception as e:
            return e
        finally:
            await ac.aclose()
        return None

    err: Optional[BaseException] = None
    if variant == "sync":
        c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG, max_retries=0, local=cfg)
        try:
            c.jobs.create(text="x", idempotency_key="dip404")
        except Exception as e:
            err = e
        c.close()
    else:
        err = asyncio.run(arun())
    assert isinstance(err, OutcomeUnknownError), err
    assert b.requests() == [] and posts(cloud) == []


def test_t3_older_key_fetch_does_not_reinstall_a_dropped_key(cloud: FakeCloud) -> None:
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG)
    real = c._request

    def slow(*a: Any, **kw: Any) -> Any:
        res = real(*a, **kw)
        c._drop_server_key()  # a sealed_content rejection lands while this fetch is in flight
        return res

    c._request = slow  # type: ignore[method-assign]
    assert c.server_public_key()
    assert c._server_key is None, "the stale fetch re-installed the dropped key"
    c._request = real  # type: ignore[method-assign]
    c.server_public_key()
    assert c._server_key is not None
    c.close()


def test_t3_older_key_fetch_async(cloud: FakeCloud) -> None:
    async def run() -> None:
        c = AsyncCatzPrint(KEY, base_url=cloud.url, org_id=ORG)
        real = c._request

        async def slow(*a: Any, **kw: Any) -> Any:
            res = await real(*a, **kw)
            c._drop_server_key()
            return res

        c._request = slow  # type: ignore[method-assign]
        assert await c.server_public_key()
        assert c._server_key is None
        await c.aclose()

    asyncio.run(run())
