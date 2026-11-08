"""Local printing (sync client): modes, fallback rules, tokens, pinning, store, timeouts, helpers.

Ported from the Go SDK's local_test.go (the reference implementation).
"""

from __future__ import annotations

import base64
import os
import stat
import sys
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from catzprint import (
    AuthenticationError,
    CatzPrint,
    CatzPrintError,
    FileTokenStore,
    LocalConfig,
    MemoryTokenStore,
    OutOfPrintsError,
    PermissionDeniedError,
    PinMismatchError,
    PrinterError,
    RateLimitError,
    ServerError,
    StationUnreachableError,
    StoredToken,
    ValidationError,
    is_cover_open,
    is_offline_allowance_exhausted,
    is_out_of_paper,
    is_printer_offline,
    is_station_unreachable,
)
from catzprint._local_core import utcnow
from catzprint._local_transport import NO_PIN_CODE
from conftest import KEY, ORG
from localfakes import (
    CERTS,
    PUBLIC_HOST,
    FakeCloud,
    FakeStation,
    dead_url,
    endpoint,
    local_client,
    token_jti,
)


def kitchen(key: str) -> Dict[str, Any]:
    return {
        "role": "kitchen",
        "template_id": "tpl-kitchen",
        "data": {"table": "12"},
        "reference": "order-1042",
        "idempotency_key": key,
    }


def pins(st: FakeStation) -> Dict[str, str]:
    return {st.url: st.fp}


# ---------------------------------------------------------------------------- modes


def test_local_mode_prints_on_station(cloud: FakeCloud, station: FakeStation) -> None:
    c = local_client(
        cloud.url, mode="local", location_id="loc1", station_urls=[station.url], fingerprints=pins(station)
    )
    job = c.jobs.create(role="kitchen", commands=[{"type": "text", "text": "ALLERGY"}, {"type": "cut"}], copies=2)
    assert job.via == "local" and job.printed_locally and job.id.startswith("lj_")
    assert job.status == "queued" and job.copies == 2 and job.live
    assert job.local is not None and job.local.station_url == station.url and job.printer_name == "Kitchen"
    reqs = station.job_requests()
    assert len(reqs) == 1
    r = reqs[0]
    key = r.body["idempotency_key"]
    assert key.startswith("sdk-") and len(key) == 40 and job.idempotency_key == key
    assert r.auth.startswith("Bearer czl1.")
    assert r.body["kind"] == "commands" and r.body["role"] == "kitchen" and r.body["location_id"] == "loc1"
    assert cloud.counts() == (1, 0)
    assert cloud.mints[0] == {"device_name": "Hub", "location_ids": ["loc1"]}
    # The second job reuses the cached token.
    c.jobs.create(text="hi")
    assert cloud.counts()[0] == 1


def test_local_job_body_matches_spec(cloud: FakeCloud, station: FakeStation) -> None:
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station), wait=5)
    job = c.jobs.create(content=b"\x1b@hi", title="Raw", options={"cut": True})
    assert job.status == "printed" and job.printed_at is not None
    r = station.job_requests()[0]
    assert r.query == "wait=5"
    assert r.body["kind"] == "escpos" and r.body["content"] == base64.b64encode(b"\x1b@hi").decode()
    assert r.body["title"] == "Raw" and r.body["options"] == {"cut": True}
    for k in ("file_name", "scheduled_for", "sealed_content"):
        assert k not in r.body


def test_local_first_falls_back_when_station_unreachable(cloud: FakeCloud) -> None:
    dead = dead_url()
    c = local_client(cloud.url, mode="local_first", station_urls=[dead], fingerprints={dead: "ab" * 32})
    job = c.jobs.create(**kitchen("order-1042-kitchen-r1"))
    assert job.via == "cloud" and job.id == "cloud-1" and not job.printed_locally
    assert cloud.last_job()["idempotency_key"] == "order-1042-kitchen-r1"


FALLBACK_CASES: List[Tuple[int, str, bool, Optional[Callable[[BaseException], bool]]]] = [
    (502, "bad_gateway", True, None),
    (504, "gateway_timeout", True, None),
    (503, "local_disabled", True, None),
    (503, "printer_offline", False, is_printer_offline),
    (503, "out_of_paper", False, is_out_of_paper),
    (503, "cover_open", False, is_cover_open),
    (503, "printer_error", False, lambda e: isinstance(e, PrinterError)),
    (422, "no_printer", False, lambda e: isinstance(e, ValidationError)),
    (400, "invalid", False, lambda e: isinstance(e, ValidationError)),
    (422, "unsupported_locally", False, lambda e: isinstance(e, ValidationError)),
    (422, "test_raw_not_allowed", False, lambda e: isinstance(e, ValidationError)),
    (402, "offline_allowance_exhausted", False, is_offline_allowance_exhausted),
    (402, "test_limit", False, lambda e: isinstance(e, OutOfPrintsError)),
    (403, "device_not_approved", False, lambda e: isinstance(e, PermissionDeniedError)),
    (403, "forbidden_location", False, lambda e: isinstance(e, PermissionDeniedError)),
    (409, "duplicate", False, lambda e: isinstance(e, CatzPrintError) and e.status == 409),
    (429, "rate_limited", False, lambda e: isinstance(e, RateLimitError)),
]


@pytest.mark.parametrize("status,code,fallback,check", FALLBACK_CASES)
def test_local_first_fallback_rules(
    cloud: FakeCloud,
    station: FakeStation,
    status: int,
    code: str,
    fallback: bool,
    check: Optional[Callable[[BaseException], bool]],
) -> None:
    def fail(path: str) -> Optional[Tuple[int, str]]:
        return (status, code) if path == "/local/v1/jobs" else None

    station.fail = fail
    c = local_client(cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station))
    key = f"k-{code}"
    if fallback:
        job = c.jobs.create(**kitchen(key))
        assert job.via == "cloud" and cloud.counts()[1] == 1 and cloud.last_job()["idempotency_key"] == key
        return
    with pytest.raises(CatzPrintError) as ei:
        c.jobs.create(**kitchen(key))
    e = ei.value
    assert cloud.counts()[1] == 0, "must not fall back"
    assert not is_station_unreachable(e) and e.code == code and check is not None and check(e)


def test_503_without_code_falls_back(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    import json
    from http.server import BaseHTTPRequestHandler

    st = stations()
    orig = st._serve

    def serve(h: BaseHTTPRequestHandler) -> None:
        if h.path.startswith("/local/v1/jobs"):
            raw = json.dumps({"error": {"message": "busy"}}).encode()
            h.send_response(503)
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)
            return
        orig(h)

    st._serve = serve  # type: ignore[method-assign]
    c = local_client(cloud.url, mode="local_first", station_urls=[st.url], fingerprints=pins(st))
    assert c.jobs.create(text="x", idempotency_key="k503").via == "cloud"
    assert cloud.last_job()["idempotency_key"] == "k503"


def test_401_remints_once_then_succeeds(cloud: FakeCloud, station: FakeStation) -> None:
    station.accept = lambda tok: token_jti(tok) == "jti-2"  # only the second minted token
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station))
    job = c.jobs.create(**kitchen("k1"))
    assert job.via == "local"
    assert cloud.counts()[0] == 2 and len(station.job_requests()) == 2


@pytest.mark.parametrize("mode", ["local_first", "local"])
def test_401_twice_falls_back_or_fails(cloud: FakeCloud, station: FakeStation, mode: str) -> None:
    station.accept = lambda tok: False
    c = local_client(cloud.url, mode=mode, station_urls=[station.url], fingerprints=pins(station))
    if mode == "local_first":
        job = c.jobs.create(**kitchen("k401"))
        assert job.via == "cloud" and cloud.last_job()["idempotency_key"] == "k401"
    else:
        with pytest.raises(AuthenticationError) as ei:
            c.jobs.create(**kitchen("k401"))
        assert ei.value.code == "token_expired" and cloud.counts()[1] == 0
    assert cloud.counts()[0] == 2 and len(station.job_requests()) == 2, "re-mint must happen exactly once"


def test_401_with_cloud_down_falls_back_to_cloud_error(cloud: FakeCloud, station: FakeStation) -> None:
    station.accept = lambda tok: False
    c = local_client(cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station))
    c.local.token()
    cloud.down = True
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(**kitchen("k"))
    assert isinstance(ei.value.cloud_error, ServerError) and ei.value.__cause__ is ei.value.cloud_error
    assert len(station.job_requests()) == 1
    assert "fresh token couldn't be minted" in str(ei.value.attempts[0].error)


# ---------------------------------------------------------------------------- cloud_first


def test_cloud_first_503_goes_local_with_the_same_key(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.job_status, cloud.job_code = 503, "unavailable"
    c = CatzPrint(
        KEY,
        base_url=cloud.url,
        org_id=ORG,
        max_retries=3,  # cloud_first makes ONE cloud attempt regardless
        local=LocalConfig(
            mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), all_locations=True
        ),
    )
    c._sleep = lambda s: None
    job = c.jobs.create(text="x")
    assert job.via == "local"
    key = cloud.last_job()["idempotency_key"]
    assert key.startswith("sdk-") and station.job_requests()[0].body["idempotency_key"] == key
    assert cloud.counts()[1] == 1, "cloud_first must not retry the cloud before falling back"


def test_cloud_first_network_error_goes_local(station: FakeStation) -> None:
    c = local_client(
        None, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), token="czd_paired"
    )
    job = c.jobs.create(text="x", idempotency_key="order-7")
    r = station.job_requests()[0]
    assert job.via == "local" and r.body["idempotency_key"] == "order-7" and r.auth == "Bearer czd_paired"


def test_cloud_first_uses_cloud_timeout_and_never_goes_local_after_no_answer(
    cloud: FakeCloud, station: FakeStation
) -> None:
    """cloud_first: the create reached the cloud but no answer came within cloud_timeout -> one retry with
    the same key, then OutcomeUnknownError; the stations are not tried (the cloud may have queued it)."""
    from catzprint import OutcomeUnknownError

    cloud.job_plan = [("hang", 2), ("hang", 2)]
    c = local_client(
        cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station), cloud_timeout=0.3
    )
    t0 = time.monotonic()
    with pytest.raises(OutcomeUnknownError) as ei:
        c.jobs.create(text="x", idempotency_key="order-9")
    assert time.monotonic() - t0 < 1.8
    assert ei.value.idempotency_key == "order-9" and ei.value.url == cloud.url
    assert [j["idempotency_key"] for j in cloud.jobs] == ["order-9", "order-9"]
    assert station.requests() == []


@pytest.mark.parametrize(
    "status,code", [(422, "no_printer"), (402, "out_of_prints"), (429, "rate_limited"), (401, "invalid_api_key")]
)
def test_cloud_first_4xx_stays(cloud: FakeCloud, station: FakeStation, status: int, code: str) -> None:
    cloud.job_status, cloud.job_code = status, code
    c = local_client(cloud.url, mode="cloud_first", station_urls=[station.url], fingerprints=pins(station))
    with pytest.raises(CatzPrintError) as ei:
        c.jobs.create(text="x")
    assert ei.value.code == code and station.requests() == []


def test_cloud_first_both_down(cloud: FakeCloud) -> None:
    cloud.job_status, cloud.job_code = 503, "unavailable"
    dead = dead_url()
    c = local_client(cloud.url, mode="cloud_first", station_urls=[dead], fingerprints={dead: "0" * 64})
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    assert isinstance(ei.value.cloud_error, ServerError) and ei.value.urls == [dead]
    assert ei.value.code == "station_unreachable"


def test_unsupported_locally(cloud: FakeCloud, station: FakeStation) -> None:
    pdf = b"%PDF-1.4 fake"
    local = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station))
    with pytest.raises(ValidationError) as ei:
        local.print_pdf(pdf)
    assert ei.value.code == "unsupported_locally"
    lf = local_client(cloud.url, mode="local_first", station_urls=[station.url], fingerprints=pins(station))
    assert lf.print_pdf(pdf).via == "cloud" and station.requests() == []
    # Scheduled and sealed jobs go through the cloud too.
    with pytest.raises(ValidationError):
        local.jobs.create(text="x", scheduled_for="2030-01-01T00:00:00Z")
    assert lf.jobs.create(text="x", scheduled_for="2030-01-01T00:00:00Z").via == "cloud"
    assert station.requests() == []


def test_station_order_tries_next_on_unreachable(cloud: FakeCloud, station: FakeStation) -> None:
    dead = dead_url()
    c = local_client(
        cloud.url,
        mode="local",
        station_urls=[dead, station.url],
        fingerprints={dead: station.fp, station.url: station.fp},
    )
    job = c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == station.url


def test_cloud_mode_unchanged(cloud: FakeCloud) -> None:
    c = CatzPrint(KEY, base_url=cloud.url, org_id=ORG)
    job = c.print_text("x")
    key = cloud.last_job()["idempotency_key"]
    assert job.via == "cloud" and not key.startswith("sdk-") and len(key) == 36


def test_cloud_job_local_fields() -> None:
    from catzprint import Job

    j = Job.from_dict(
        {"id": "j1", "printed_locally": True, "local_device": "Till 3", "duplicate_of": "j0", "offline": True}
    )
    assert j.printed_locally and j.local_device == "Till 3" and j.duplicate_of == "j0" and j.offline
    assert j.via is None


# ---------------------------------------------------------------------------- pinning


def test_pin_no_fingerprint_never_contacted(cloud: FakeCloud, station: FakeStation) -> None:
    c = local_client(cloud.url, mode="local", station_urls=[station.url])
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    e = ei.value.attempts[0].error
    assert isinstance(e, CatzPrintError) and e.code == NO_PIN_CODE
    assert station.requests() == [] and station.httpd.handshakes == 0


def test_pin_wrong_fingerprint_rejected_before_any_request(cloud: FakeCloud, station: FakeStation) -> None:
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints={station.url: "ab" * 32})
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    pm = ei.value.attempts[0].error
    assert isinstance(pm, PinMismatchError) and pm.got == station.fp and not pm.spki
    assert station.requests() == []


def test_pin_fingerprint_with_colons_upper_case(cloud: FakeCloud, station: FakeStation) -> None:
    fp = ":".join(station.fp[i : i + 2].upper() for i in range(0, 64, 2))
    host = station.url.removeprefix("https://")
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints={host: fp})
    assert c.jobs.create(text="x").via == "local"
    c2 = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints={"127.0.0.1": fp})
    assert c2.jobs.create(text="y").via == "local"


def test_pin_from_cloud_endpoint_list(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.stations = [endpoint(station)]
    c = local_client(cloud.url, mode="local", station_urls=[station.url])
    job = c.jobs.create(text="x")
    assert job.station_id == "st1"


def test_pin_by_address_and_port_from_cloud_list(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.stations = [endpoint(station, urls=[])]
    c = local_client(cloud.url, mode="local", station_urls=[station.url])
    assert c.jobs.create(text="x").station_id == "st1"


def test_other_certificate_with_listed_fingerprint_rejected(
    cloud: FakeCloud, stations: Callable[..., FakeStation]
) -> None:
    impostor = stations(CERTS["otherSelfSigned"])
    real_fp = CERTS["selfSigned"]["fingerprint"]
    c = local_client(cloud.url, mode="local", station_urls=[impostor.url], fingerprints={impostor.url: real_fp})
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    assert isinstance(ei.value.attempts[0].error, PinMismatchError) and impostor.requests() == []


def _public_station(stations: Callable[..., FakeStation]) -> Tuple[FakeStation, str, Dict[str, Any]]:
    st = stations(CERTS["publicLeaf"])
    host_url = f"https://{PUBLIC_HOST}:{st.port}"
    ep = {
        "station_id": "st1",
        "local_enabled": True,
        "tls_mode": "public",
        "local_label": "abcdefghij",
        "urls": [host_url],
        "public_spki_sha256": CERTS["publicLeaf"]["spki"],
        "port": st.port,
    }
    return st, host_url, ep


def test_public_hostname_verified_spki_pinned_dialled_under_label(
    cloud: FakeCloud, stations: Callable[..., FakeStation]
) -> None:
    st, host_url, ep = _public_station(stations)
    ca = CERTS["ca"]["cert"]
    cloud.stations = [ep]
    c = local_client(cloud.url, mode="local", discover=True, ca_data=ca, discover_timeout=0.05)
    job = c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == host_url
    assert len(st.job_requests()) == 1


def test_public_hostname_spki_mismatch(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    st, _, ep = _public_station(stations)
    cloud.stations = [dict(ep, public_spki_sha256="ab" * 32)]
    c = local_client(cloud.url, mode="local", discover=True, ca_data=CERTS["ca"]["cert"], discover_timeout=0.05)
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    pm = ei.value.attempts[0].error
    assert isinstance(pm, PinMismatchError) and pm.spki and pm.got == CERTS["publicLeaf"]["spki"]
    assert st.requests() == []


def test_public_hostname_unknown_ca(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    import ssl

    st, _, ep = _public_station(stations)
    cloud.stations = [ep]
    c = local_client(cloud.url, mode="local", discover=True, discover_timeout=0.05)  # system roots only
    with pytest.raises(StationUnreachableError) as ei:
        c.jobs.create(text="x")
    e = ei.value.attempts[0].error
    assert isinstance(e.__cause__, ssl.SSLCertVerificationError) and st.requests() == []


def test_unlisted_hostname_not_dialled_at_its_ip(
    stations: Callable[..., FakeStation], cloud: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured hostname the cloud doesn't list is resolved with DNS, never dialled at the IP in its
    name; a listed one (under its label) is dialled directly."""
    import socket as _s

    st, host_url, ep = _public_station(stations)
    dialled: List[str] = []
    real = _s.create_connection

    def fake_create_connection(addr: Tuple[str, int], *a: Any, **kw: Any) -> _s.socket:
        if addr[1] == st.port:  # (httpx's cloud calls go through here too)
            dialled.append(addr[0])
            if addr[0] == PUBLIC_HOST:
                raise OSError("DNS is down")  # simulate: no public DNS
        return real(addr, *a, **kw)

    monkeypatch.setattr(_s, "create_connection", fake_create_connection)
    c = local_client(None, mode="local", station_urls=[host_url], ca_data=CERTS["ca"]["cert"], token="czd_x")
    with pytest.raises(StationUnreachableError):
        c.jobs.create(text="x")
    assert dialled == [PUBLIC_HOST] and st.requests() == []
    cloud.stations = [ep]
    c2 = local_client(cloud.url, mode="local", discover=True, ca_data=CERTS["ca"]["cert"], discover_timeout=0.05)
    assert c2.jobs.create(text="x").via == "local"
    assert dialled[-1] == "127.0.0.1"


# ---------------------------------------------------------------------------- token store


def test_file_token_store(tmp_path: Any) -> None:
    path = os.path.join(str(tmp_path), "sub", "tokens.json")
    s = FileTokenStore(path)
    assert s.load("a") is None
    exp = (utcnow() + timedelta(hours=1)).replace(microsecond=0)
    s.save("a", StoredToken(token="czl1.a", expires_at=exp))
    s.save("b", StoredToken(token="czl1.b"))
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert os.listdir(os.path.dirname(path)) == ["tokens.json"], "temporary files left behind"
    got = FileTokenStore(path).load("a")
    assert got is not None and got.token == "czl1.a" and got.expires_at == exp
    with open(path) as f:
        assert KEY not in f.read()
    # A corrupt file is reported on load and replaced on save.
    with open(path, "w") as f:
        f.write("{nope")
    with pytest.raises(CatzPrintError):
        s.load("a")
    s.save("a", StoredToken(token="czl1.c"))
    got = s.load("a")
    assert got is not None and got.token == "czl1.c"
    # Deleting a key.
    s.save("a", None)
    assert s.load("a") is None


def test_file_token_store_atomic(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save that fails half-way leaves the old file intact and no temporary file behind."""
    path = os.path.join(str(tmp_path), "tokens.json")
    s = FileTokenStore(path)
    s.save("a", StoredToken(token="czl1.old"))

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(CatzPrintError):
        s.save("a", StoredToken(token="czl1.new"))
    monkeypatch.undo()
    got = s.load("a")
    assert got is not None and got.token == "czl1.old"
    assert os.listdir(str(tmp_path)) == ["tokens.json"]


def test_file_token_store_reads_go_zero_times(tmp_path: Any) -> None:
    path = os.path.join(str(tmp_path), "t.json")
    with open(path, "w") as f:
        f.write(
            '{"k":{"token":"czl1.x","expires_at":"2030-01-01T00:00:00Z","stations_updated_at":"0001-01-01T00:00:00Z",'
            '"last_remint_at":"0001-01-01T00:00:00Z","stations":[{"station_id":"s1","port":7443}]}}'
        )
    got = FileTokenStore(path).load("k")
    assert got is not None and got.stations_updated_at is None and got.last_remint_at is None
    assert got.stations[0].station_id == "s1"


def test_memory_token_store_copies() -> None:
    s = MemoryTokenStore()
    t = StoredToken(token="czl1.a")
    s.save("k", t)
    t.token = "changed"
    got = s.load("k")
    assert got is not None and got.token == "czl1.a"


def test_token_store_survives_restart_and_refreshes(cloud: FakeCloud, station: FakeStation, tmp_path: Any) -> None:
    store = FileTokenStore(os.path.join(str(tmp_path), "tokens.json"))
    cfg: Dict[str, Any] = dict(
        mode="local", location_id="loc1", station_urls=[station.url], fingerprints=pins(station), token_store=store
    )
    cloud.stations = [{"station_id": "st1", "local_enabled": True}]
    c1 = local_client(cloud.url, **cfg)
    c1.jobs.create(text="1")
    assert cloud.counts()[0] == 1
    # Restart: the stored token is used, nothing is minted.
    c2 = local_client(cloud.url, **cfg)
    c2.jobs.create(text="2")
    assert cloud.counts()[0] == 1
    # Less than 7 days left: the old token is used for this job, then renewed on use.
    key = c2._local.state.store_key
    old = store.load(key)
    assert old is not None
    old.expires_at = utcnow() + timedelta(days=3)
    store.save(key, old)
    c3 = local_client(cloud.url, **cfg)
    c3.jobs.create(text="3")
    assert station.job_requests()[-1].auth == "Bearer " + old.token
    assert cloud.counts()[0] == 2
    renewed = store.load(key)
    assert renewed is not None and renewed.token != old.token
    assert renewed.expires_at is not None and renewed.expires_at - utcnow() > timedelta(days=20)
    # Expired: minted before the job.
    renewed.expires_at = utcnow() - timedelta(minutes=1)
    store.save(key, renewed)
    c4 = local_client(cloud.url, **cfg)
    c4.jobs.create(text="4")
    assert station.job_requests()[-1].auth != "Bearer " + renewed.token


def test_failed_refresh_retried_after_5_minutes(cloud: FakeCloud, station: FakeStation) -> None:
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints=pins(station))
    c.local.token()  # mint; the fake cloud returns no endpoint list, so it is stale
    cloud.down = True
    now = utcnow()
    c._local.state.now = lambda: now
    c.jobs.create(text="1")
    c.jobs.create(text="2")
    with cloud.lock:
        assert cloud.endpoints == 0  # the 503 answer isn't counted; check next_try instead
    assert c._local.state.next_try == now + timedelta(minutes=5)
    cloud.down = False
    c.jobs.create(text="3")
    with cloud.lock:
        assert cloud.endpoints == 0, "retried before 5 minutes"
    c._local.state.now = lambda: now + timedelta(minutes=6)
    c.jobs.create(text="4")
    with cloud.lock:
        assert cloud.endpoints == 1


def test_endpoint_list_refreshed_when_stale(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.stations = [endpoint(station)]
    c = local_client(cloud.url, mode="local", discover=True, discover_timeout=0.05)
    now = utcnow()
    c._local.state.now = lambda: now
    # First job: the mint brings the endpoint list, which is used right away.
    job = c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == station.url
    assert cloud.endpoints == 0
    c._local.state.now = lambda: now + timedelta(hours=2)
    c.jobs.create(text="y")
    assert cloud.endpoints == 1


# ---------------------------------------------------------------------------- timeouts


def test_station_unreachable_timeout_before_send_falls_back(cloud: FakeCloud) -> None:
    """A station that never completes the TLS handshake (NOT_DELIVERED) -> cloud with the same key."""
    import socket as _s

    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    url = f"https://127.0.0.1:{srv.getsockname()[1]}"
    try:
        c = local_client(
            cloud.url, mode="local_first", station_urls=[url], fingerprints={url: "ab" * 32}, request_timeout=0.3
        )
        t0 = time.monotonic()
        job = c.jobs.create(text="x", idempotency_key="slow-1")
        assert job.via == "cloud" and cloud.last_job()["idempotency_key"] == "slow-1"
        assert time.monotonic() - t0 < 1.5
    finally:
        srv.close()


def test_connect_timeout_bounds_tls_handshake(cloud: FakeCloud) -> None:
    import socket as _s

    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)  # accepts TCP, never speaks TLS
    url = f"https://127.0.0.1:{srv.getsockname()[1]}"
    try:
        c = local_client(
            cloud.url, mode="local_first", station_urls=[url], fingerprints={url: "ab" * 32}, connect_timeout=0.2
        )
        t0 = time.monotonic()
        assert c.jobs.create(text="x").via == "cloud"
        assert time.monotonic() - t0 < 1.5
    finally:
        srv.close()


# ---------------------------------------------------------------------------- helpers


def test_local_helpers(cloud: FakeCloud, station: FakeStation) -> None:
    cloud.stations = [endpoint(station)]
    c = local_client(cloud.url, mode="local", location_id="loc1", discover=True, discover_timeout=0.05)
    st = c.local.status()
    assert st.unsynced == 3 and st.allowance.prints_left == 1997 and st.station.last_cloud_contact is None
    assert st.printers[0].role == "kitchen" and st.station_url == station.url
    info = c.local.info(station.url)
    assert info.station_id == "st1" and info.pairing_open
    with pytest.raises(ValidationError) as ei:
        c.local.pair(station.url, "1234", "Till")
    assert ei.value.code == "invalid"
    with pytest.raises(CatzPrintError) as ei2:
        c.local.pair(station.url, "00000000", "Till")
    assert ei2.value.code == "invalid_code"
    p = c.local.pair(station.url, "12345678", "Till 3")
    assert p.token == "czd_paired" and p.device_id == "d_0123456789abcdef" and "czd_" not in repr(p)
    pr = station.requests()[-1]
    assert pr.auth == "" and pr.body["device_name"] == "Till 3"

    job = c.jobs.create(text="x")
    got = c.jobs.wait_for(job.id, interval=0.01)
    assert got.status == "printed" and got.via == "local" and got.printed_at is not None
    with pytest.raises(CatzPrintError) as ei3:
        c.jobs.get("lj_missing")
    assert ei3.value.code == "not_found"

    found = c.local.discover()
    assert len(found) == 1 and found[0].from_cloud and found[0].fingerprint == station.fp
    assert found[0].urls[0] == station.url

    lt = c.local_tokens.create(device_name="Till 9", location_ids=["loc1"], ttl_days=7)
    assert lt.token.startswith("czl1.") and lt.stations is not None and len(lt.stations) == 1
    assert cloud.mints[-1] == {"device_name": "Till 9", "location_ids": ["loc1"], "ttl_days": 7}
    assert "czl1" not in repr(lt)
    c.local_tokens.revoke(lt.token_id)
    assert cloud.revoked == [lt.token_id]
    eps = c.local_endpoints.list("loc1")
    assert len(eps) == 1 and eps[0].port == station.port
    with pytest.raises(ValidationError):
        c.local_tokens.create(device_name="x", ttl_days=31)
    assert c.local.mode == "local" and c.local.store_error is None


def test_get_job_tries_other_stations_on_404(cloud: FakeCloud, stations: Callable[..., FakeStation]) -> None:
    a, b = stations(), stations()
    c = local_client(cloud.url, mode="local", station_urls=[a.url, b.url], fingerprints={**pins(a), **pins(b)})
    job = c.jobs.create(text="x")  # goes to a
    c2 = local_client(cloud.url, mode="local", station_urls=[b.url, a.url], fingerprints={**pins(a), **pins(b)})
    got = c2.jobs.get(job.id)  # b says 404, a has it
    assert got.local is not None and got.local.station_url == a.url


def test_local_config_validation() -> None:
    bad: List[Dict[str, Any]] = [
        dict(mode="offline"),
        dict(mode="local"),
        dict(mode="local_first", station_urls=["http://192.168.1.20:7443"], all_locations=True),
        dict(mode="local", station_urls=["https://192.168.1.20:7443/x"], all_locations=True),
        dict(mode="local", station_urls=["https://u:p@192.168.1.20:7443"], all_locations=True),
        dict(
            mode="local",
            station_urls=["https://192.168.1.20:7443"],
            fingerprints={"192.168.1.20": "abc"},
            all_locations=True,
        ),
        dict(mode="local", discover=True, token="cz_live_x"),
        dict(mode="local", discover=True, wait=60, all_locations=True),
        dict(mode="local", discover=True, token_ttl_days=31, all_locations=True),
        dict(mode="local_first", discover=True),  # minting without location_id or all_locations
    ]
    for cfg in bad:
        with pytest.raises(ValueError):
            CatzPrint(KEY, local=LocalConfig(**cfg))
    c = CatzPrint(KEY, local=LocalConfig(mode="local_first", discover=True, location_id="loc1"))
    assert c.local.mode == "local_first"


def test_station_unreachable_error_message() -> None:
    from catzprint import APIConnectionError

    e = StationUnreachableError()
    e.add("https://10.0.0.5:7443", APIConnectionError("connection refused"))
    e.cloud_error = ServerError("Down.", status=503, code="unavailable")
    s = str(e)
    assert "10.0.0.5" in s and "connection refused" in s and "cloud fallback failed" in s
    assert is_station_unreachable(e) and e.urls == ["https://10.0.0.5:7443"]
    assert "no station address known" in str(StationUnreachableError())


def test_error_code_mapping() -> None:
    from catzprint._errors import error_from_response

    def mk(status: int, code: str) -> CatzPrintError:
        return error_from_response(status, {"error": {"code": code, "message": "m", "param": "copies"}})

    assert isinstance(mk(503, "printer_offline"), PrinterError)
    assert isinstance(mk(401, "device_revoked"), AuthenticationError)
    assert isinstance(mk(402, "test_limit"), OutOfPrintsError)
    assert isinstance(mk(422, "test_raw_not_allowed"), ValidationError)
    assert isinstance(mk(402, "offline_allowance_exhausted"), OutOfPrintsError)
    assert mk(400, "invalid").fields == {"copies": "m"}
