"""Models for local printing (stations on the local network)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from ._models import _b, _i, _os, _s, _strs, parse_time

__all__ = [
    "VIA_CLOUD",
    "VIA_LOCAL",
    "LocalAllowance",
    "LocalEndpoint",
    "LocalInfo",
    "LocalJob",
    "LocalPairing",
    "LocalPrinterStatus",
    "LocalStation",
    "LocalStationState",
    "LocalStatus",
    "LocalToken",
]

#: ``Job.via`` values.
VIA_CLOUD = "cloud"
VIA_LOCAL = "local"


def _dict(d: Mapping[str, Any], k: str) -> Dict[str, Any]:
    v = d.get(k)
    return dict(v) if isinstance(v, dict) else {}


@dataclass
class LocalEndpoint:
    """One station's local-network address as the cloud knows it.

    From ``GET /v1/orgs/{org}/local-endpoints`` and the local-tokens response.
    """

    station_id: str
    name: str
    location_id: str
    connected: bool
    local_enabled: bool
    port: int
    #: The station's private addresses.
    addresses: List[str]
    #: Public-certificate hostnames first, then raw IP URLs.
    urls: List[str]
    #: ``public`` (a publicly trusted certificate for the hostnames) or ``self_signed``.
    tls_mode: str
    #: Hex SHA-256 of the station's certificate (DER); pinned for IP and self-signed URLs.
    cert_fingerprint_sha256: str
    #: Hex SHA-256 of the public certificate's SubjectPublicKeyInfo; pinned for hostnames.
    public_spki_sha256: str
    #: The station's label in ``<a-b-c-d>.<label>.<zone>``.
    local_label: str
    updated_at: Optional[datetime]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalEndpoint:
        return cls(
            station_id=_s(d, "station_id"),
            name=_s(d, "name"),
            location_id=_s(d, "location_id"),
            connected=_b(d, "connected"),
            local_enabled=_b(d, "local_enabled"),
            port=_i(d, "port"),
            addresses=_strs(d, "addresses"),
            urls=_strs(d, "urls"),
            tls_mode=_s(d, "tls_mode"),
            cert_fingerprint_sha256=_s(d, "cert_fingerprint_sha256"),
            public_spki_sha256=_s(d, "public_spki_sha256"),
            local_label=_s(d, "local_label"),
            updated_at=parse_time(d.get("updated_at")),
            raw=dict(d),
        )

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.raw)
        out.update(
            station_id=self.station_id,
            name=self.name,
            location_id=self.location_id,
            connected=self.connected,
            local_enabled=self.local_enabled,
            port=self.port,
            addresses=list(self.addresses),
            urls=list(self.urls),
            tls_mode=self.tls_mode,
            cert_fingerprint_sha256=self.cert_fingerprint_sha256,
            public_spki_sha256=self.public_spki_sha256,
            local_label=self.local_label,
        )
        return out


def endpoints_from(v: Any) -> Optional[List[LocalEndpoint]]:
    """A ``stations`` array, or ``None`` when it is missing / null."""
    if not isinstance(v, list):
        return None
    return [LocalEndpoint.from_dict(x) for x in v if isinstance(x, dict)]


@dataclass
class LocalToken:
    """A minted local token (``czl1…``) that stations verify offline."""

    token: str
    token_id: str
    expires_at: Optional[datetime]
    #: The organization's station endpoints (``None`` when the API didn't include them).
    stations: Optional[List[LocalEndpoint]]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # never show the token itself
        return f"LocalToken(token_id={self.token_id!r}, expires_at={self.expires_at!r})"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalToken:
        return cls(
            token=_s(d, "token"),
            token_id=_s(d, "token_id"),
            expires_at=parse_time(d.get("expires_at")),
            stations=endpoints_from(d.get("stations")),
            raw=dict(d),
        )


@dataclass
class LocalJob:
    """A job as a station's local API reports it."""

    id: str
    idempotency_key: str
    #: queued, printing, printed or failed.
    status: str
    error_code: str
    detail: str
    printer_id: str
    printer_name: str
    role: str
    location_id: str
    copies: int
    title: str
    reference: str
    created_at: Optional[datetime]
    finished_at: Optional[datetime]
    printed_locally: bool
    via: str
    cloud_job_id: Optional[str]
    #: False for jobs sent with a test-mode token (``None`` when the station didn't say).
    live: Optional[bool]
    #: The station URL that answered (set by the SDK).
    station_url: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalJob:
        live = d.get("live")
        return cls(
            id=_s(d, "id"),
            idempotency_key=_s(d, "idempotency_key"),
            status=_s(d, "status"),
            error_code=_s(d, "error_code"),
            detail=_s(d, "detail"),
            printer_id=_s(d, "printer_id"),
            printer_name=_s(d, "printer_name"),
            role=_s(d, "role"),
            location_id=_s(d, "location_id"),
            copies=_i(d, "copies"),
            title=_s(d, "title"),
            reference=_s(d, "reference"),
            created_at=parse_time(d.get("created_at")),
            finished_at=parse_time(d.get("finished_at")),
            printed_locally=_b(d, "printed_locally"),
            via=_s(d, "via"),
            cloud_job_id=_os(d, "cloud_job_id"),
            live=live if isinstance(live, bool) else None,
            raw=dict(d),
        )


@dataclass
class LocalStationState:
    """The station part of :class:`LocalStatus`."""

    id: str
    name: str
    cloud_connected: bool
    last_cloud_contact: Optional[datetime]
    version: str
    local_enabled: bool

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalStationState:
        return cls(
            id=_s(d, "id"),
            name=_s(d, "name"),
            cloud_connected=_b(d, "cloud_connected"),
            last_cloud_contact=parse_time(d.get("last_cloud_contact")),
            version=_s(d, "version"),
            local_enabled=_b(d, "local_enabled"),
        )


@dataclass
class LocalPrinterStatus:
    """One printer in :class:`LocalStatus`."""

    id: str
    name: str
    role: str
    #: ready, offline, paper_out, paper_low, cover_open, error, busy or unknown.
    status: str
    #: ok, low, out or unknown.
    paper: str
    jobs_waiting: int

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalPrinterStatus:
        return cls(
            id=_s(d, "id"),
            name=_s(d, "name"),
            role=_s(d, "role"),
            status=_s(d, "status"),
            paper=_s(d, "paper"),
            jobs_waiting=_i(d, "jobs_waiting"),
        )


@dataclass
class LocalAllowance:
    """What the station may still print without the cloud."""

    expires_at: Optional[datetime]
    prints_left: int

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalAllowance:
        return cls(expires_at=parse_time(d.get("expires_at")), prints_left=_i(d, "prints_left"))


@dataclass
class LocalStatus:
    """A station's ``GET /local/v1/status`` answer."""

    station: LocalStationState
    printers: List[LocalPrinterStatus]
    jobs_waiting: int
    #: Local jobs the station still has to report to the cloud.
    unsynced: int
    allowance: LocalAllowance
    #: The station URL that answered (set by the SDK).
    station_url: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalStatus:
        printers = d.get("printers")
        return cls(
            station=LocalStationState.from_dict(_dict(d, "station")),
            printers=[LocalPrinterStatus.from_dict(p) for p in printers if isinstance(p, dict)]
            if isinstance(printers, list)
            else [],
            jobs_waiting=_i(d, "jobs_waiting"),
            unsynced=_i(d, "unsynced"),
            allowance=LocalAllowance.from_dict(_dict(d, "allowance")),
            raw=dict(d),
        )


@dataclass
class LocalInfo:
    """A station's unauthenticated ``GET /local/v1/info`` answer."""

    station_id: str
    name: str
    org_id: str
    location_id: str
    version: str
    port: int
    tls_mode: str
    cert_fingerprint_sha256: str
    hostnames: List[str]
    addresses: List[str]
    pairing_open: bool
    time: Optional[datetime]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalInfo:
        return cls(
            station_id=_s(d, "station_id"),
            name=_s(d, "name"),
            org_id=_s(d, "org_id"),
            location_id=_s(d, "location_id"),
            version=_s(d, "version"),
            port=_i(d, "port"),
            tls_mode=_s(d, "tls_mode"),
            cert_fingerprint_sha256=_s(d, "cert_fingerprint_sha256"),
            hostnames=_strs(d, "hostnames"),
            addresses=_strs(d, "addresses"),
            pairing_open=_b(d, "pairing_open"),
            time=parse_time(d.get("time")),
            raw=dict(d),
        )


@dataclass
class LocalPairing:
    """The result of ``local.pair``: a device token (``czd_…``) for that one station.

    Pass ``token`` as ``LocalConfig(token=...)``.
    """

    device_id: str
    token: str
    station_id: str
    org_id: str
    location_id: str

    def __repr__(self) -> str:  # never show the token itself
        return f"LocalPairing(device_id={self.device_id!r}, station_id={self.station_id!r})"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LocalPairing:
        return cls(
            device_id=_s(d, "device_id"),
            token=_s(d, "token"),
            station_id=_s(d, "station_id"),
            org_id=_s(d, "org_id"),
            location_id=_s(d, "location_id"),
        )


@dataclass
class LocalStation:
    """A station found by ``local.discover``."""

    station_id: str
    name: str
    org_id: str
    location_id: str
    port: int
    addresses: List[str]
    #: The URLs the SDK would use, in order.
    urls: List[str]
    tls_mode: str
    fingerprint: str
    #: In the cloud's endpoint list (authenticated).
    from_cloud: bool = False
    #: Answered over mDNS on the local network.
    from_mdns: bool = False
    #: From the cloud endpoint list.
    local_enabled: bool = False
    connected: bool = False
