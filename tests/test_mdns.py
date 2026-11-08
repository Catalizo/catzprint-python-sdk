"""mDNS discovery: query, parsing, browse, and trust rules. Ported from the Go SDK's mdns_test.go."""

from __future__ import annotations

import struct
import threading
import time
from typing import Callable

import pytest

from catzprint import PinMismatchError, StationUnreachableError
from catzprint._local_core import mdns_targets
from catzprint._mdns import MDNS_SERVICE, MDNSCollector, browse, build_query
from conftest import ORG
from localfakes import (
    SERVICE,
    FakeCloud,
    FakeResponder,
    FakeStation,
    build_response,
    dead_url,
    local_client,
    mdns_answer,
)


def test_mdns_query() -> None:
    q = build_query()
    _id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", q[:12])
    assert flags == 0 and (qd, an, ns, ar) == (1, 0, 0, 0)
    name = b"".join(bytes([len(p)]) + p.encode() for p in MDNS_SERVICE.rstrip(".").split(".")) + b"\0"
    assert q[12 : 12 + len(name)] == name
    qtype, qclass = struct.unpack("!HH", q[12 + len(name) :])
    assert qtype == 12 and qclass & 0x8000  # PTR, unicast-response bit


def test_mdns_parse() -> None:
    fp = "a1" * 32
    col = MDNSCollector()
    col.add(mdns_answer("Kitchen PC", "st1", "org1", "loc1", 7443, fp, "192.168.1.20"), "192.168.1.20")
    got = col.stations()
    assert len(got) == 1
    s = got[0]
    assert s.instance == "Kitchen PC (CatzPrint)" and s.station_id == "st1" and s.org_id == "org1"
    assert s.location_id == "loc1" and s.port == 7443 and s.fingerprint == fp and s.tls_mode == "self_signed"
    assert s.addresses == ["192.168.1.20"] and s.hostname == ""
    ts = mdns_targets(s)
    assert [t.url for t in ts] == ["https://192.168.1.20:7443"] and ts[0].fingerprint == fp


def test_mdns_parse_split_packets_public_host_and_source_fallback() -> None:
    fp = "0f" * 32
    inst = "Bar (CatzPrint)." + SERVICE
    col = MDNSCollector()
    # PTR alone, then SRV+TXT in another packet without an A record: the packet's source address is used.
    col.add(build_response([(SERVICE, 12, inst)], []), "10.0.0.9")
    assert col.stations() == []
    col.add(
        build_response(
            [
                (inst, 33, (7000, "catzprint-st2.local.")),
                (
                    inst,
                    16,
                    [
                        "v=1",
                        "id=st2",
                        "org=org1",
                        "loc=loc1",
                        "port=7443",
                        "FP=" + fp.upper(),
                        "tls=public",
                        "host=10-0-0-9.abcdefghij.local.catzprint.com",
                    ],
                ),
            ],
            [],
        ),
        "10.0.0.9",
    )
    got = col.stations()
    assert len(got) == 1 and got[0].port == 7443 and got[0].fingerprint == fp and got[0].addresses == ["10.0.0.9"]
    # The announced hostname is parsed but never becomes a target.
    assert got[0].hostname == "10-0-0-9.abcdefghij.local.catzprint.com"
    ts = mdns_targets(got[0])
    assert [t.url for t in ts] == ["https://10.0.0.9:7443"] and ts[0].fingerprint == fp


@pytest.mark.parametrize(
    "name,msg",
    [
        ("wrong version", mdns_answer("A", "st1", "o", "l", 7443, "aa" * 32, "192.168.1.2", "v=2")),
        ("bad fingerprint", mdns_answer("B", "st1", "o", "l", 7443, "zz", "192.168.1.2")),
        ("missing id", mdns_answer("C", "", "o", "l", 7443, "aa" * 32, "192.168.1.2")),
        ("truncated garbage", mdns_answer("D", "st1", "o", "l", 7443, "aa" * 32, "192.168.1.2")[:40]),
        ("not a response", build_query()),
        ("pointer loop", b"\x00\x00\x84\x00\x00\x01\x00\x00\x00\x00\x00\x00\xc0\x0c\x00\x0c\x00\x01"),
        ("empty", b""),
    ],
)
def test_mdns_parse_rejects_bad_announcements(name: str, msg: bytes) -> None:
    col = MDNSCollector()
    col.add(msg, "192.168.1.2")
    assert col.stations() == [], name


def test_mdns_ignores_foreign_service() -> None:
    other = build_response(
        [
            ("_http._tcp.local.", 12, "x._http._tcp.local."),
            ("x._http._tcp.local.", 16, ["v=1", "id=evil"]),
            ("x._http._tcp.local.", 33, (80, "x.local.")),
        ],
        [],
    )
    col = MDNSCollector()
    col.add(other, "192.168.1.3")
    assert col.stations() == []


def test_mdns_browse(responders: Callable[[bytes], FakeResponder]) -> None:
    r = responders(mdns_answer("Kitchen PC", "st1", "org1", "loc1", 7443, "ab" * 32, "192.168.1.20"))
    t0 = time.monotonic()
    got = browse(0.3, r.addr)
    el = time.monotonic() - t0
    assert len(got) == 1 and got[0].station_id == "st1"
    assert 0.25 <= el < 2
    assert r.queries >= 2, "one resend shortly after the first query"
    # Cancellation ends the browse early.
    ev = threading.Event()
    threading.Timer(0.05, ev.set).start()
    t0 = time.monotonic()
    browse(5, r.addr, ev)
    assert time.monotonic() - t0 < 1


def test_mdns_discovered_station_is_used_with_its_fingerprint(
    station: FakeStation, responders: Callable[[bytes], FakeResponder]
) -> None:
    resp = responders(mdns_answer("Kitchen PC", "st1", ORG, "loc1", station.port, station.fp, "127.0.0.1"))
    # The cloud is unreachable and the endpoint list was never fetched: an announcement alone is not
    # trusted, even with a fingerprint.
    c0 = local_client(None, mode="local", location_id="loc1", discover=True, discover_timeout=0.2, token="czd_paired")
    c0._local.state.mdns_addr = resp.addr
    with pytest.raises(StationUnreachableError):
        c0.jobs.create(text="x")
    assert station.requests() == []
    # With the fingerprint from the station app configured, mDNS maps it to the station's current address.
    pins = {"kitchen-pc": station.fp}
    c = local_client(
        None,
        mode="local",
        location_id="loc1",
        discover=True,
        discover_timeout=0.2,
        token="czd_paired",
        fingerprints=pins,
    )
    c._local.state.mdns_addr = resp.addr
    job = c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == station.url and job.live
    # A wrong org or location is ignored.
    for ann in (
        mdns_answer("Other org", "st1", "org2", "loc1", station.port, station.fp, "127.0.0.1"),
        mdns_answer("Other loc", "st1", ORG, "loc2", station.port, station.fp, "127.0.0.1"),
    ):
        c2 = local_client(
            None,
            mode="local",
            location_id="loc1",
            discover=True,
            discover_timeout=0.15,
            token="czd_paired",
            fingerprints=pins,
        )
        c2._local.state.mdns_addr = responders(ann).addr
        with pytest.raises(StationUnreachableError):
            c2.jobs.create(text="x")


def test_mdns_unknown_station_ignored_once_endpoint_list_known(
    cloud: FakeCloud, station: FakeStation, responders: Callable[[bytes], FakeResponder]
) -> None:
    # The cloud lists st1 only; an impostor announces "st666" with its own certificate on the LAN.
    cloud.stations = [
        {"station_id": "st1", "local_enabled": True, "urls": [dead_url()], "cert_fingerprint_sha256": "cd" * 32}
    ]
    c = local_client(cloud.url, mode="local", discover=True, discover_timeout=0.15)
    c._local.state.mdns_addr = responders(
        mdns_answer("Impostor", "st666", ORG, "loc1", station.port, station.fp, "127.0.0.1")
    ).addr
    with pytest.raises(StationUnreachableError):
        c.jobs.create(text="x")
    assert station.requests() == []
    # The listed station announcing a new address is used, pinned with the fingerprint from the list
    # (which here doesn't match: rejected).
    c2 = local_client(cloud.url, mode="local", discover=True, discover_timeout=0.15)
    c2._local.state.mdns_addr = responders(
        mdns_answer("Kitchen", "st1", ORG, "loc1", station.port, station.fp, "127.0.0.1")
    ).addr
    with pytest.raises(StationUnreachableError) as ei:
        c2.jobs.create(text="x")
    assert isinstance(ei.value.attempts[1].error, PinMismatchError) and station.requests() == []


def test_mdns_maps_listed_station_to_new_address(
    cloud: FakeCloud, station: FakeStation, responders: Callable[[bytes], FakeResponder]
) -> None:
    """The station moved: the cloud's URL is dead, mDNS gives the new address, pinned with the list's fingerprint."""
    cloud.stations = [
        {
            "station_id": "st1",
            "local_enabled": True,
            "port": station.port,
            "urls": [dead_url()],
            "cert_fingerprint_sha256": station.fp,
        }
    ]
    c = local_client(cloud.url, mode="local", discover=True, discover_timeout=0.15)
    # The announcement claims another port and fingerprint: the list's are used.
    c._local.state.mdns_addr = responders(mdns_answer("Kitchen", "st1", ORG, "loc1", 1, "ee" * 32, "127.0.0.1")).addr
    job = c.jobs.create(text="x")
    assert job.local is not None and job.local.station_url == station.url
    found = c.local.discover()
    assert len(found) == 1 and found[0].from_cloud and found[0].from_mdns and station.url in found[0].urls
