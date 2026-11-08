"""Regression tests for the local-printing security review (SPEC addendum 1: A10, A11, A12, A15).

Ported from the Go SDK's local_security_test.go.
"""

from __future__ import annotations

import os
import socket
import ssl
import threading
from datetime import timedelta
from typing import Any, List

import pytest

from catzprint import (
    AuthenticationError,
    CatzPrint,
    FileTokenStore,
    LocalConfig,
    PinMismatchError,
    StationUnreachableError,
    ValidationError,
)
from catzprint._local_core import (
    LocalState,
    Target,
    dashed_ip,
    endpoint_targets,
    mdns_targets,
    private_ip,
    resolve_config,
)
from catzprint._local_models import LocalEndpoint
from catzprint._local_transport import StationTransport, spki_sha256
from catzprint._mdns import MDNSStation
from catzprint._tokenstore import StoredToken
from conftest import KEY, ORG
from localfakes import CERTS, PUBLIC_HOST, FakeCloud, FakeStation, local_client, server_context


def state(**cfg: Any) -> LocalState:
    return LocalState(resolve_config(LocalConfig(**cfg)), KEY, lambda: ORG)


def ep(**kw: Any) -> LocalEndpoint:
    return LocalEndpoint.from_dict(kw)


def test_rev_mdns_hostname_impostor() -> None:
    """An mDNS impostor copying a real station's ID and announcing its own public-cert hostname must not
    produce a hostname target, nor its own fingerprint; with no cloud list an announcement alone
    produces nothing."""
    real_fp, evil_fp = "aa" * 32, "bb" * 32
    ls = state(discover=True, all_locations=True)
    st = StoredToken(
        stations=[
            ep(
                station_id="real-station",
                local_enabled=True,
                tls_mode="public",
                port=7443,
                cert_fingerprint_sha256=real_fp,
                addresses=["192.168.1.20"],
            )
        ],
        stations_updated_at=ls.now(),
    )
    evil = MDNSStation(
        instance="Evil",
        station_id="real-station",
        org_id="",
        location_id="",
        port=7443,
        tls_mode="public",
        fingerprint=evil_fp,
        hostname="192-168-1-66.attackerlbl.local.catzprint.com",
        addresses=["192.168.1.66"],
    )
    targets: List[Target] = []
    for d in ls.accepted_mdns([evil], st):
        targets += mdns_targets(d)
    assert len(targets) == 1
    t = targets[0]
    assert t.url == "https://192.168.1.66:7443" and t.needs_pin() and t.fingerprint == real_fp
    assert "attackerlbl" not in t.url and t.label == "" and t.spki == ""
    # No endpoint list (paired device): the announced fingerprint is not trusted.
    lone = MDNSStation("x", "x", "", "", 7443, evil_fp, "self_signed", "", ["192.168.1.66"])
    assert ls.accepted_mdns([lone], None) == []
    # Public addresses announced over mDNS are dropped.
    pub = MDNSStation(**{**evil.__dict__, "addresses": ["203.0.113.9"]})
    assert ls.accepted_mdns([pub], st) == []
    # A station the cloud lists as local-disabled is ignored.
    st.stations[0].local_enabled = False
    assert ls.accepted_mdns([evil], st) == []


def test_endpoint_targets_filter() -> None:
    fp = "cd" * 32
    ts = endpoint_targets(
        ep(
            station_id="s",
            tls_mode="public",
            local_label="abcdefghij",
            cert_fingerprint_sha256=fp,
            public_spki_sha256="ef" * 32,
            urls=[
                "https://192-168-1-20.abcdefghij.local.catzprint.com:7443",
                "https://192.168.1.20:7443",
                "https://203.0.113.9:7443",
                "https://100.64.0.1:7443",
                "https://[fd00::1]:7443",
                "http://192.168.1.21:7443",
            ],
        ),
        "cloud",
    )
    assert [t.url for t in ts] == [
        "https://192-168-1-20.abcdefghij.local.catzprint.com:7443",
        "https://192.168.1.20:7443",
        "https://[fd00::1]:7443",
    ]
    assert ts[0].label == "abcdefghij" and ts[0].spki == "ef" * 32 and not ts[0].needs_pin() and not ts[0].fingerprint
    assert ts[1].fingerprint == fp and ts[2].fingerprint == fp and ts[1].needs_pin()


@pytest.mark.parametrize(
    "ip,ok",
    [
        ("10.1.2.3", True),
        ("172.16.0.1", True),
        ("172.31.255.255", True),
        ("172.32.0.1", False),
        ("192.168.1.20", True),
        ("127.0.0.1", True),
        ("169.254.1.1", True),
        ("fd00::1", True),
        ("[fd12::5]", True),
        ("fe80::1", True),
        ("::1", True),
        ("::ffff:192.168.1.5", True),
        ("100.64.0.1", False),
        ("8.8.8.8", False),
        ("203.0.113.9", False),
        ("2001:db8::1", False),
        ("::ffff:8.8.8.8", False),
        ("0.0.0.0", False),
        ("224.0.0.251", False),
        ("printer.local", False),
        ("", False),
    ],
)
def test_private_address_filter(ip: str, ok: bool) -> None:
    assert private_ip(ip) is ok


@pytest.mark.parametrize(
    "host,label,want",
    [
        ("192-168-1-20.ab3k9x2mqp.local.catzprint.com", "ab3k9x2mqp", "192.168.1.20"),
        ("192-168-1-20.ab3k9x2mqp.local.catzprint.com", "otherlabel", None),
        ("192-168-1-20.ab3k9x2mqp.local.catzprint.com", "", None),
        ("10-0-0-5.x.y", "x", "10.0.0.5"),
        ("127-0-0-1.x.y", "x", "127.0.0.1"),
        ("8-8-8-8.x.local.catzprint.com", "x", None),
        ("169-254-1-1.x.y", "x", None),
        ("192-168-1.x.y", "x", None),
        ("192-168-1-20.com", "com", None),
        ("printer.example.com", "example", None),
    ],
)
def test_dashed_ip(host: str, label: str, want: Any) -> None:
    assert dashed_ip(host, label) == want


def test_mdns_never_produces_hostname_targets() -> None:
    d = MDNSStation("K", "st1", ORG, "loc1", 7443, "aa" * 32, "public", PUBLIC_HOST, ["192.168.1.9", "fd00::9"])
    assert [t.url for t in mdns_targets(d)] == ["https://192.168.1.9:7443", "https://[fd00::9]:7443"]
    assert all(t.needs_pin() and t.fingerprint == "aa" * 32 for t in mdns_targets(d))


# ---------------------------------------------------------------------------- tokens


@pytest.mark.parametrize("code", ["device_revoked", "unauthenticated"])
def test_remint_never_on(cloud: FakeCloud, station: FakeStation, code: str) -> None:
    station.accept = lambda tok: False
    station.reject_code = code
    c = local_client(cloud.url, mode="local", station_urls=[station.url], fingerprints={station.url: station.fp})
    with pytest.raises(AuthenticationError) as ei:
        c.jobs.create(text="x")
    assert ei.value.code == code
    assert cloud.counts()[0] == 1 and len(station.job_requests()) == 1


def test_remint_rate_limit_is_persisted_per_store_key(cloud: FakeCloud, station: FakeStation, tmp_path: Any) -> None:
    station.accept = lambda tok: False
    store = FileTokenStore(os.path.join(str(tmp_path), "t.json"))
    cfg = dict(mode="local", station_urls=[station.url], fingerprints={station.url: station.fp}, token_store=store)
    for _ in range(3):  # three clients (restarts) within 10 minutes
        c = local_client(cloud.url, **cfg)
        with pytest.raises(AuthenticationError) as ei:
            c.jobs.create(text="x")
        assert ei.value.code == "token_expired"
    assert cloud.counts()[0] == 2  # the first mint + one re-mint
    # After 10 minutes one more re-mint is allowed.
    c = local_client(cloud.url, **cfg)
    real_now = c._local.state.now
    c._local.state.now = lambda: real_now() + timedelta(minutes=11)
    with pytest.raises(AuthenticationError):
        c.jobs.create(text="x")
    assert cloud.counts()[0] == 3
    # Another device name is another store key: its own limit.
    c2 = local_client(cloud.url, device_name="Till 2", **cfg)
    with pytest.raises(AuthenticationError):
        c2.jobs.create(text="x")
    assert cloud.counts()[0] == 5


def test_never_remint_a_static_token(cloud: FakeCloud, station: FakeStation) -> None:
    station.accept = lambda tok: False
    c = local_client(
        cloud.url, mode="local", station_urls=[station.url], fingerprints={station.url: station.fp}, token="czl1.a.b"
    )
    with pytest.raises(AuthenticationError):
        c.jobs.create(text="x")
    assert cloud.counts()[0] == 0


def test_mint_bound_to_location(cloud: FakeCloud, station: FakeStation) -> None:
    with pytest.raises(ValueError):
        CatzPrint(KEY, local=LocalConfig(mode="local", discover=True))
    c = local_client(
        cloud.url, mode="local", location_id="loc9", station_urls=[station.url], fingerprints={station.url: station.fp}
    )
    c.jobs.create(text="x")
    assert cloud.mints[0]["location_ids"] == ["loc9"]
    assert station.job_requests()[0].body["location_id"] == "loc9"
    # all_locations: the token isn't narrowed.
    c2 = local_client(
        cloud.url, mode="local", all_locations=True, station_urls=[station.url], fingerprints={station.url: station.fp}
    )
    c2.jobs.create(text="x")
    assert "location_ids" not in cloud.mints[1]


def test_store_key_hashes_the_api_key() -> None:
    ls = state(mode="local", discover=True, location_id="loc1", device_name="Till")
    assert KEY not in ls.store_key and ls.store_key.startswith("v1|") and ls.store_key.endswith("|loc1|Till")


# ---------------------------------------------------------------------------- pairing


def test_pair_requires_trust(station: FakeStation) -> None:
    c = local_client(None, mode="cloud")
    with pytest.raises(ValidationError) as ei:
        c.local.pair(station.url, "12345678", "Till")
    assert ei.value.code == "invalid" and station.requests() == []
    with pytest.raises(StationUnreachableError) as ei2:
        c.local.pair(station.url, "12345678", "Till", fingerprint="ab" * 32)
    assert isinstance(ei2.value.attempts[0].error, PinMismatchError) and station.requests() == []
    host = f"https://127-0-0-1.abcdefghij.local.catzprint.com:{station.port}"
    with pytest.raises(ValidationError):
        c.local.pair(host, "12345678", "Till")
    with pytest.raises(ValidationError):  # a fingerprint needs an IP URL
        c.local.pair(host, "12345678", "Till", fingerprint=station.fp)
    with pytest.raises(ValidationError):
        c.local.pair(station.url, "12345678", "Till", fingerprint="nothex")
    p = c.local.pair(station.url, "12345678", "Till", fingerprint=station.fp.upper())
    assert p.token == "czd_paired"


def test_pair_trusts_cloud_list_or_configured_pin(cloud: FakeCloud, station: FakeStation) -> None:
    from localfakes import endpoint

    cloud.stations = [endpoint(station)]
    c = local_client(cloud.url, mode="cloud")
    c.local.refresh()
    assert c.local.pair(station.url, "12345678").token == "czd_paired"
    c2 = local_client(None, mode="cloud", fingerprints={"127.0.0.1": station.fp})
    assert c2.local.pair(station.url, "12345678").token == "czd_paired"


def test_local_job_live(station: FakeStation) -> None:
    c = local_client(
        None, mode="local", station_urls=[station.url], fingerprints={station.url: station.fp}, token="czd_paired"
    )
    j = c.jobs.create(text="x")
    assert j.live
    station.live = False
    j = c.jobs.create(text="y")
    assert not j.live and j.local is not None and j.local.live is False


def test_test_token_job_not_live(cloud: FakeCloud, station: FakeStation) -> None:
    from localfakes import make_local_token

    c = local_client(
        None,
        mode="local",
        station_urls=[station.url],
        fingerprints={station.url: station.fp},
        token=make_local_token("t", live=False),
    )
    assert not c.jobs.create(text="x").live


def test_paired_device_without_api_key(station: FakeStation, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATZPRINT_API_KEY", "")
    pins = {station.url: station.fp}
    with pytest.raises(AuthenticationError):
        CatzPrint(
            local=LocalConfig(mode="local_first", station_urls=[station.url], fingerprints=pins, token="czd_paired")
        )
    with pytest.raises(AuthenticationError):
        CatzPrint(local=LocalConfig(mode="local", location_id="l", station_urls=[station.url], fingerprints=pins))
    c = CatzPrint(
        local=LocalConfig(
            mode="local",
            station_urls=[station.url],
            fingerprints=pins,
            token="czd_paired",
            discover=True,
            discover_timeout=0.05,
        )
    )
    j = c.jobs.create(role="kitchen", text="x")
    assert j.via == "local" and j.live
    assert c.local.status().unsynced == 3
    with pytest.raises(AuthenticationError) as ei:
        c.printers.list()
    assert ei.value.code == "missing_api_key"
    with pytest.raises(AuthenticationError) as ei:
        c.jobs.get("not-local")
    assert ei.value.code == "missing_api_key"


# ---------------------------------------------------------------------------- bytes on the wire


class RawTLSServer:
    """Completes TLS handshakes and records how many application bytes each client sent."""

    def __init__(self, cert: Any) -> None:
        self.ctx = server_context(cert)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.received: List[int] = []
        self.done = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            raw, _ = self.sock.accept()
        except OSError:
            return
        n = 0
        try:
            raw.settimeout(3)
            conn = self.ctx.wrap_socket(raw, server_side=True)
            try:
                while True:
                    b = conn.recv(4096)
                    if not b:
                        break
                    n += len(b)
            except (OSError, ssl.SSLError):
                pass
            conn.close()
        except (OSError, ssl.SSLError):
            raw.close()
        self.received.append(n)
        self.done.set()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_pin_checked_before_any_request_byte(asynchronous: bool) -> None:
    import asyncio

    srv = RawTLSServer(CERTS["selfSigned"])
    try:
        tr = StationTransport(connect_timeout=2, request_timeout=3, user_agent="t")
        t = Target(url=f"https://127.0.0.1:{srv.port}", fingerprint="ab" * 32, tls_mode="self_signed")
        with pytest.raises(PinMismatchError) as ei:
            if asynchronous:
                asyncio.run(tr.arequest(t, "POST", "/local/v1/jobs", token="czl1.secret", body={"text": "x"}))
            else:
                tr.request(t, "POST", "/local/v1/jobs", token="czl1.secret", body={"text": "x"})
        assert ei.value.got == CERTS["selfSigned"]["fingerprint"]
        assert srv.done.wait(5)
        assert srv.received == [0], "request bytes (and the token) were sent before the pin check"
    finally:
        srv.sock.close()


def test_spki_of_fixture_certs() -> None:
    for name in ("selfSigned", "publicLeaf", "ca", "otherSelfSigned"):
        der = ssl.PEM_cert_to_DER_cert(CERTS[name]["cert"])
        assert spki_sha256(der) == CERTS[name]["spki"]


def test_redirect_from_station_is_delivered_no_answer(cloud: FakeCloud, station: FakeStation) -> None:
    """A 3xx to a job POST: maybe delivered; the station is asked by key; it doesn't have it (404), so the
    job falls back to the cloud."""
    station.fail = lambda path: (302, "moved") if path == "/local/v1/jobs" else None
    station.fail_method = "POST"
    c = local_client(cloud.url, mode="local_first", station_urls=[station.url], fingerprints={station.url: station.fp})
    assert c.jobs.create(text="x").via == "cloud"
    assert len(station.lookups()) == 1


def test_redact_tokens_in_reprs() -> None:
    cfg = LocalConfig(mode="local", token="czd_secret", station_urls=["https://192.168.1.2:7443"])
    assert "czd_secret" not in repr(cfg)
    assert "czl1" not in repr(StoredToken(token="czl1.x.y"))
