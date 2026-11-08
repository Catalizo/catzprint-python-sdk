"""Local printing: configuration, trust decisions and state shared by the sync and async clients (no I/O).

The behaviour follows the Go SDK (the reference implementation): see ``local.go`` there.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import ipaddress
import json
import math
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ._errors import CatzPrintError, ValidationError
from ._local_models import VIA_LOCAL, LocalEndpoint, LocalJob, LocalStation
from ._models import Job
from ._tokenstore import MemoryTokenStore, StoredToken, TokenStore, is_redaction_placeholder

if TYPE_CHECKING:
    from ._mdns import MDNSStation

__all__ = [
    "MODE_CLOUD",
    "MODE_CLOUD_FIRST",
    "MODE_LOCAL",
    "MODE_LOCAL_FIRST",
    "LocalConfig",
]

#: Every job goes to the CatzPrint API (the default; same behaviour as before 2.2.0).
MODE_CLOUD = "cloud"
#: Jobs go only to stations on the local network.
MODE_LOCAL = "local"
#: Stations first; the cloud only when no station can be reached.
MODE_LOCAL_FIRST = "local_first"
#: The cloud first; the stations on a network error, timeout or 5xx.
MODE_CLOUD_FIRST = "cloud_first"
MODES = (MODE_CLOUD, MODE_LOCAL, MODE_LOCAL_FIRST, MODE_CLOUD_FIRST)

DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_CLOUD_TIMEOUT = 10.0
DEFAULT_DISCOVER_TIMEOUT = 1.5
TOKEN_REFRESH_BEFORE = timedelta(days=7)
ENDPOINTS_MAX_AGE = timedelta(hours=1)
MDNS_CACHE_TTL = timedelta(minutes=1)
REFRESH_RETRY_AFTER = timedelta(minutes=5)
REMINT_INTERVAL = timedelta(minutes=10)
MAX_LOCAL_WAIT = 30.0
#: Largest station answer the SDK reads.
MAX_LOCAL_RESPONSE_BYTES = 4 << 20
#: Station job kinds (§4.2); anything else goes through the cloud.
LOCAL_KINDS = frozenset({"commands", "template", "text", "escpos", "test"})


@dataclass
class LocalConfig:
    """Local printing through CatzPrint stations on the same network (``CatzPrint(local=...)``).

    Timeouts are in seconds. See the README section "Local printing (offline)".
    """

    #: ``cloud`` (default), ``local``, ``local_first`` or ``cloud_first``.
    mode: str = MODE_CLOUD
    #: The location this device prints at. It narrows minted tokens and the endpoint list, filters
    #: discovered stations, and is used as ``location_id`` when a job sets neither ``printer_id`` nor
    #: ``location_id``. Required in the local modes unless ``all_locations`` or ``token`` is set.
    location_id: Optional[str] = None
    #: Station URLs to try first, in order, e.g. ``https://192-168-1-20.ab3k9x2mqp.local.catzprint.com:7443``
    #: or ``https://192.168.1.20:7443``. IP URLs need a fingerprint (from the endpoint list or ``fingerprints``).
    station_urls: Sequence[str] = ()
    #: Pins for certificates you got out of band (the station app shows its fingerprint): keys are a
    #: station URL, ``host:port`` or host; values the hex SHA-256 of the certificate (colons allowed).
    fingerprints: Mapping[str, str] = field(default_factory=dict)
    #: Add stations from the cloud endpoint list (cached in the token store, refreshed hourly while
    #: online) and map their station IDs to current addresses with mDNS. mDNS alone is never trusted.
    discover: bool = False
    #: Mint tokens valid at every location the API key may use (default: bound to ``location_id``).
    all_locations: bool = False
    #: Keeps the local token and endpoint list between runs (default: :class:`MemoryTokenStore`).
    token_store: Optional[TokenStore] = None
    #: A fixed token instead of minting one: a device token from ``local.pair`` (``czd_…``) or a local
    #: token (``czl1.…``).
    token: Optional[str] = field(default=None, repr=False)
    #: Names this device on stations (default: the host name).
    device_name: Optional[str] = None
    #: Lifetime of minted tokens, 1..30 days (default 30).
    token_ttl_days: int = 0
    #: Connecting to a station, TLS included.
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    #: One station request.
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    #: The cloud attempt in ``cloud_first``, token minting and endpoint refreshes.
    cloud_timeout: float = DEFAULT_CLOUD_TIMEOUT
    #: How long an mDNS browse listens.
    discover_timeout: float = DEFAULT_DISCOVER_TIMEOUT
    #: Ask the station to hold its answer until the job printed or failed, 0..30 s (default 0).
    wait: float = 0.0
    #: CA certificates (PEM file / PEM text) that verify public-certificate station hostnames, instead
    #: of the system roots.
    ca_file: Optional[str] = None
    ca_data: Optional[str] = field(default=None, repr=False)


def resolve_config(cfg: Optional[LocalConfig]) -> LocalConfig:
    """Validate ``cfg`` and return a normalized copy (raises ``ValueError``)."""
    c = dataclasses.replace(cfg) if cfg is not None else LocalConfig()
    c.mode = c.mode or MODE_CLOUD
    if c.mode not in MODES:
        raise ValueError(f"Unknown local mode {c.mode!r} (use cloud, local, local_first or cloud_first).")
    urls: List[str] = []
    for raw in c.station_urls or ():
        u = normalize_station_url(raw)
        if u not in urls:
            urls.append(u)
    c.station_urls = tuple(urls)
    pins: Dict[str, str] = {}
    for k, v in (c.fingerprints or {}).items():
        fp = normalize_fingerprint(v)
        if not fp:
            raise ValueError(f"The fingerprint for {k!r} must be 64 hex characters (SHA-256).")
        pins[pin_key(k)] = fp
    c.fingerprints = pins
    c.location_id = c.location_id or None
    c.token = c.token or None
    local = c.mode != MODE_CLOUD
    if local and not c.station_urls and not c.discover:
        raise ValueError("Local printing needs station_urls or discover=True.")
    if local and not c.token and not c.location_id and not c.all_locations:
        raise ValueError(
            "Set LocalConfig.location_id (minted local tokens are bound to it), or all_locations=True for a "
            "token valid at every location."
        )
    if c.token and not c.token.startswith(("czl1.", "czd_")):
        raise ValueError("LocalConfig.token must be a local token (czl1.…) or a device token (czd_…).")
    if not isinstance(c.token_ttl_days, int) or c.token_ttl_days < 0 or c.token_ttl_days > 30:
        raise ValueError("token_ttl_days must be between 1 and 30.")
    if not (0 <= c.wait <= MAX_LOCAL_WAIT):
        raise ValueError("LocalConfig.wait must be between 0 and 30 seconds.")
    for name, default in (
        ("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        ("request_timeout", DEFAULT_REQUEST_TIMEOUT),
        ("cloud_timeout", DEFAULT_CLOUD_TIMEOUT),
        ("discover_timeout", DEFAULT_DISCOVER_TIMEOUT),
    ):
        v = getattr(c, name)
        if v is None or not v > 0:  # also rejects NaN
            setattr(c, name, default)
    if c.token_store is None:
        c.token_store = MemoryTokenStore()
    if not c.device_name:
        try:
            c.device_name = socket.gethostname() or "CatzPrint Python SDK"
        except OSError:  # pragma: no cover
            c.device_name = "CatzPrint Python SDK"
    c.device_name = c.device_name[:80]
    return c


# ---------------------------------------------------------------------------- URLs, pins, addresses


def normalize_station_url(raw: str) -> str:
    """Check a station URL (https only; no credentials, path, query or fragment); return https://host:port."""
    s = (raw or "").strip()
    try:
        u = urlsplit(s)
        host = u.hostname
        _ = u.port  # raises on a bad port
    except ValueError as e:
        raise ValueError(f"Station URL {raw!r} isn't a valid URL.") from e
    if not u.netloc or not host:
        raise ValueError(f"Station URL {raw!r} isn't a valid URL.")
    if u.scheme != "https":
        raise ValueError(f"Station URL {raw!r} must use https://.")
    if u.username is not None or u.password is not None or u.query or u.fragment or u.path not in ("", "/"):
        raise ValueError(f"Station URL {raw!r} must be just https://host:port.")
    return "https://" + u.netloc.lower()


def pin_key(k: str) -> str:
    """Normalize a ``fingerprints`` key: a URL becomes host:port, anything else is lower-cased."""
    k = k.strip().lower()
    if "://" in k:
        try:
            return urlsplit(k).netloc
        except ValueError:
            pass
    return k.rstrip("/")


def normalize_fingerprint(fp: Optional[str]) -> str:
    """Hex with optional colons/spaces/dashes -> lower-case hex, or "" when it isn't 32 bytes of hex."""
    if not isinstance(fp, str):
        return ""
    s = fp.strip().replace(":", "").replace(" ", "").replace("-", "").lower()
    if len(s) != 64:
        return ""
    try:
        bytes.fromhex(s)
    except ValueError:
        return ""
    return s


def url_host(u: str) -> Tuple[str, str]:
    """(host lower-cased without brackets, port as written) of a normalized station URL."""
    try:
        p = urlsplit(u)
        port = p.port
    except ValueError:
        return "", ""
    return (p.hostname or "").lower(), str(port) if port is not None else ""


def parse_ip(h: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        return None


_PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",  # RFC 1918
        "127.0.0.0/8",  # loopback (tests, a station on this machine)
        "169.254.0.0/16",  # link-local
        "fc00::/7",  # IPv6 ULA
        "fe80::/10",  # IPv6 link-local
        "::1/128",
    )
]


def private_ip(h: str) -> bool:
    """True for an IP a station may use on the LAN: RFC 1918, IPv6 ULA, link-local or loopback.

    Anything else from the cloud list or mDNS is ignored (100.64/10 and public addresses included).
    """
    ip = parse_ip(h)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in _PRIVATE_NETS)


def dashed_ip(host: str, label: str) -> Optional[str]:
    """Decode ``<a>-<b>-<c>-<d>.<label>.<zone>`` to the private (or loopback) IPv4 it names.

    Only names under the given station label (from the cloud endpoint list) are mapped, so the SDK
    can connect while public DNS is unreachable; the certificate is still verified for the hostname
    and its public key pinned.
    """
    labels = host.split(".")
    if len(labels) < 3 or not label or labels[1] != label:
        return None
    parts = labels[0].split("-")
    if len(parts) != 4:
        return None
    try:
        ip = ipaddress.ip_address(".".join(parts))
    except ValueError:
        return None
    if not isinstance(ip, ipaddress.IPv4Address) or not any(
        ip in n for n in _PRIVATE_NETS[:4]
    ):  # RFC 1918 or loopback only
        return None
    return str(ip)


@dataclass
class Target:
    """One station URL to try, with how to trust it."""

    url: str  # https://host:port
    fingerprint: str = ""  # lower-case hex SHA-256 of the certificate DER, or ""
    tls_mode: str = ""  # public, self_signed or "" (unknown)
    station_id: str = ""
    source: str = ""  # config, cloud, mdns
    #: Hex SHA-256 of the public certificate's SubjectPublicKeyInfo, pinned on top of normal
    #: verification for public hostnames (from the cloud endpoint list).
    spki: str = ""
    #: The station's local label: a dashed-IP hostname is dialled directly only under it.
    label: str = ""

    def needs_pin(self) -> bool:
        """Raw IPs, mDNS ``.local`` names and self-signed stations are only trusted by fingerprint."""
        h, _ = url_host(self.url)
        return parse_ip(h) is not None or h.endswith(".local") or self.tls_mode == "self_signed"


def endpoint_targets(ep: LocalEndpoint, source: str) -> List[Target]:
    """Turn one cloud endpoint into targets.

    Hostnames come only from here (the authenticated cloud list): verified with the system roots and
    pinned to the public key the cloud reports, and reached directly at the IP in the name only under
    the station's own label. IP URLs must be private and are pinned to the station's self-signed
    certificate.
    """
    fp = normalize_fingerprint(ep.cert_fingerprint_sha256)
    spki = normalize_fingerprint(ep.public_spki_sha256)
    out: List[Target] = []
    for raw in ep.urls:
        try:
            u = normalize_station_url(raw)
        except ValueError:
            continue
        host, _ = url_host(u)
        t = Target(url=u, tls_mode=ep.tls_mode, station_id=ep.station_id, source=source)
        if parse_ip(host) is not None:
            if not private_ip(host):
                continue
            t.fingerprint = fp
        else:
            t.spki = spki
            label = ep.local_label.lower()
            parts = host.split(".")
            if not label and len(parts) >= 3:
                label = parts[1]  # the hostname itself came from the cloud
            t.label = label
        out.append(t)
    return out


def mdns_targets(d: MDNSStation) -> List[Target]:
    """The pinned IP URLs of an accepted announcement."""
    out = []
    for a in d.addresses:
        host = f"[{a}]" if ":" in a else a
        out.append(
            Target(
                url=f"https://{host}:{d.port}",
                fingerprint=d.fingerprint,
                tls_mode="self_signed",
                station_id=d.station_id,
                source="mdns",
            )
        )
    return out


# ---------------------------------------------------------------------------- tokens


def token_claims(tok: Optional[str]) -> Dict[str, Any]:
    """Read (without verifying) a czl1 token's payload."""
    parts = (tok or "").split(".")
    if len(parts) != 3 or parts[0] != "czl1":
        return {}
    try:
        p = parts[1]
        claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode("utf-8"))
    except (ValueError, binascii.Error, RecursionError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_live(tok: str) -> bool:
    """The live flag a token implies: paired devices (czd_) are live; local tokens carry it in their claims."""
    if tok.startswith("czd_"):
        return True
    return token_claims(tok).get("live") is True


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def usable(st: Optional[StoredToken], now: datetime) -> bool:
    return (
        st is not None and bool(st.token) and st.expires_at is not None and now + timedelta(minutes=1) < st.expires_at
    )


# ---------------------------------------------------------------------------- jobs

JobParams = Dict[str, Any]


def detect_kind(content: bytes, file_name: Optional[str]) -> str:
    """Guess the kind of raw content from its first bytes (raw ESC/POS included, like the Go SDK)."""
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"\x89PNG") or content.startswith(b"\xff\xd8\xff"):
        return "image"
    if content[:1] in (b"\x1b", b"\x1d") and not file_name:
        return "escpos"
    return "document"


def local_capable(p: JobParams) -> Optional[str]:
    """Why a job can't go to a station (``None`` when it can)."""
    if p.get("sealed"):
        return "sealed jobs go through the cloud"
    if p.get("scheduled_for") is not None:
        return "scheduled jobs go through the cloud"
    kind = p.get("kind")
    content = p.get("content")
    if content is not None:
        k = kind or detect_kind(bytes(content), p.get("file_name"))
        if k not in ("escpos", "text"):
            return "PDFs, images and documents go through the cloud"
    if kind and kind not in LOCAL_KINDS:
        return "PDFs, images and documents go through the cloud"
    return None


def unsupported_locally(why: str) -> ValidationError:
    msg = f"This job can't print locally: {why}. Send PDFs, images and documents through the cloud."
    return ValidationError(msg, status=422, code="unsupported_locally")


def infer_kind(p: JobParams) -> str:
    if p.get("kind"):
        return str(p["kind"])
    if p.get("text") is not None:
        return "text"
    if p.get("commands") is not None:
        return "commands"
    if p.get("template_id") is not None:
        return "template"
    if p.get("content") is not None:
        return detect_kind(bytes(p["content"]), p.get("file_name"))
    return ""


def local_job_to_job(lj: LocalJob, t: Target, p: JobParams, live: bool) -> Job:
    """Map a station's LocalJob to a :class:`Job` (``via="local"``)."""
    if lj.live is not None:
        live = lj.live  # the station's answer wins
    detail = lj.detail
    if lj.status == "failed" and lj.error_code and not detail:
        detail = lj.error_code
    return Job(
        id=lj.id,
        live=live,
        source="local",
        kind=infer_kind(p),
        status=lj.status,
        status_detail=detail,
        title=lj.title,
        reference=lj.reference,
        copies=lj.copies,
        printer_id=lj.printer_id or None,
        printer_name=lj.printer_name or None,
        station_id=t.station_id or None,
        station_name=None,
        location_id=lj.location_id or None,
        location_name=None,
        size=0,
        attempts=0,
        billed_as="",
        created_by=None,
        reprint_of=None,
        scheduled_for=None,
        created_at=lj.created_at,
        sent_at=None,
        printed_at=lj.finished_at if lj.status == "printed" else None,
        finished_at=lj.finished_at,
        raw=dict(lj.raw),
        printed_locally=True,
        idempotency_key=lj.idempotency_key or None,
        via=VIA_LOCAL,
        local=lj,
    )


def wait_query(wait: float) -> Optional[Dict[str, str]]:
    return {"wait": str(math.ceil(wait))} if wait > 0 else None


def is_station_retryable(status: int, code: Optional[str]) -> bool:
    """Whether a station answer means "this station can't be used now" (try the next / fall back).

    502/504, 503 ``local_disabled`` or a 503 without a code, and redirects (not a station).
    Printer errors, validation errors, 401/402/403/409/429 are answers for the caller.
    """
    if status in (502, 504):
        return True
    if status == 503 and (code is None or code in ("local_disabled", "http_503")):
        return True
    return 300 <= status < 400


# ---------------------------------------------------------------------------- state


class LocalState:
    """Configuration and cached state of one client's local printing (thread-safe, no network I/O)."""

    def __init__(self, cfg: LocalConfig, api_key: str, org_hint: Callable[[], Optional[str]]) -> None:
        self.cfg = cfg
        self.has_api_key = bool(api_key)
        kh = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        self.store_key = f"v1|{kh}|{cfg.location_id or ''}|{cfg.device_name}"
        self.store: TokenStore = cfg.token_store or MemoryTokenStore()
        self._org_hint = org_hint
        self.lock = threading.Lock()
        self.loaded = False
        self.state: Optional[StoredToken] = None
        self.store_error: Optional[BaseException] = None
        self.mdns: List[MDNSStation] = []
        self.mdns_at: Optional[datetime] = None
        self.mdns_addr: Tuple[str, int] = ("224.0.0.251", 5353)
        self.job_urls: Dict[str, str] = {}
        self.next_try: Optional[datetime] = None
        self.now: Callable[[], datetime] = utcnow

    @property
    def mode(self) -> str:
        return self.cfg.mode

    # -- store
    def ensure_loaded(self) -> None:
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
        try:
            st = self.store.load(self.store_key)
            if st is not None and is_redaction_placeholder(st.token):
                raise CatzPrintError(
                    "The token store holds a redacted placeholder instead of a token.", code="token_store"
                )
        except Exception as e:  # a broken store never stops printing
            with self.lock:
                self.store_error = e
            return
        with self.lock:
            self.state = st.clone() if st is not None else None

    def snapshot(self) -> Optional[StoredToken]:
        with self.lock:
            return self.state.clone() if self.state is not None else None

    def save(self, st: StoredToken) -> None:
        with self.lock:
            self.state = st.clone()
        try:
            self.store.save(self.store_key, st)
        except Exception as e:
            with self.lock:
                self.store_error = e

    def needs_refresh(self) -> Tuple[bool, bool]:
        """What is stale: the token (< 7 days left) and/or the endpoint list (older than an hour)."""
        st = self.snapshot()
        now = self.now()
        tok = False
        if not self.cfg.token:
            tok = (
                st is None
                or not st.token
                or st.expires_at is None
                or st.expires_at - now < self.refresh_window(st.token)
            )
        eps = st is None or st.stations_updated_at is None or now - st.stations_updated_at > ENDPOINTS_MAX_AGE
        return tok, eps

    def refresh_window(self, token: str) -> timedelta:
        """Renew a token when less than min(7 days, TTL/2) is left (so short TTLs aren't minted on
        every job). TTL: ``token_ttl_days``, else the token's own ``exp - iat``, else 30 days."""
        ttl: Optional[timedelta] = None
        if self.cfg.token_ttl_days:
            ttl = timedelta(days=self.cfg.token_ttl_days)
        else:
            c = token_claims(token)
            iat, exp = c.get("iat"), c.get("exp")
            if isinstance(iat, int) and isinstance(exp, int) and exp - iat >= 3600:
                ttl = timedelta(seconds=exp - iat)
        if ttl is None:
            ttl = timedelta(days=30)
        return min(TOKEN_REFRESH_BEFORE, ttl / 2)

    def refresh_due(self) -> bool:
        """Whether an on-use refresh should run now (not for a paired device without an API key)."""
        if not self.has_api_key:
            return False
        tok, eps = self.needs_refresh()
        if not tok and not eps:
            return False
        with self.lock:
            return self.next_try is None or self.now() >= self.next_try

    def refresh_failed(self) -> None:
        with self.lock:
            self.next_try = self.now() + REFRESH_RETRY_AFTER

    def org_hint(self) -> str:
        org = self._org_hint()
        if org:
            return org
        tok = self.cfg.token
        st = self.snapshot()
        if not tok and st is not None:
            tok = st.token
        v = token_claims(tok).get("org")
        return v if isinstance(v, str) else ""

    # -- candidates
    def user_pinned(self, fp: str) -> bool:
        return bool(fp) and fp in self.cfg.fingerprints.values()

    def target_for(self, u: str, source: str, st: Optional[StoredToken], mdns: Sequence[MDNSStation]) -> Target:
        """How to trust a configured URL: a user pin, else the cloud list (by URL, or by address and
        port for IPs), else an mDNS answer for a station on that list."""
        t = Target(url=u, source=source)
        host, port = url_host(u)
        for k in (f"{host}:{port}" if ":" not in host else f"[{host}]:{port}", host):
            fp = self.cfg.fingerprints.get(k)
            if fp:
                t.fingerprint = fp
                return t
        if st is not None:
            for ep in st.stations:
                for et in endpoint_targets(ep, source):
                    if et.url == u:
                        return et
                if parse_ip(host) is not None and private_ip(host) and host in ep.addresses and str(ep.port) == port:
                    t.fingerprint = normalize_fingerprint(ep.cert_fingerprint_sha256)
                    t.tls_mode, t.station_id = ep.tls_mode, ep.station_id
                    return t
        for d in self.accepted_mdns(mdns, st):
            if host in d.addresses and str(d.port) == port:
                t.fingerprint, t.tls_mode, t.station_id = d.fingerprint, d.tls_mode, d.station_id
                return t
        return t

    def primary_targets(self) -> List[Target]:
        """The configured URLs, then (with discover) the cloud list's URLs, public hostnames first."""
        st = self.snapshot()
        with self.lock:
            mdns = list(self.mdns)
        out: List[Target] = []
        seen = set()
        for u in self.cfg.station_urls:
            seen.add(u)
            out.append(self.target_for(u, "config", st, mdns))
        if self.cfg.discover and st is not None:
            loc = self.cfg.location_id
            for ep in st.stations:
                if not ep.local_enabled or (loc and ep.location_id and ep.location_id != loc):
                    continue
                for t in endpoint_targets(ep, "cloud"):
                    if t.url not in seen:
                        seen.add(t.url)
                        out.append(t)
        return out

    def ordered_targets(self, preferred: Optional[str]) -> List[Target]:
        targets = self.primary_targets()
        if preferred:
            for i, t in enumerate(targets):
                if t.url == preferred:
                    return [t, *targets[:i], *targets[i + 1 :]]
        return targets

    def accepted_mdns(self, found: Sequence[MDNSStation], st: Optional[StoredToken]) -> List[MDNSStation]:
        """Filter mDNS announcements.

        mDNS is unauthenticated, so it only maps a station ID to its current addresses: an
        announcement is used for a station on the cloud endpoint list (pinned with the list's
        fingerprint, port from the list) or one whose announced fingerprint the user configured. The
        announced hostname is never used, and only private addresses are kept.
        """
        org = self.org_hint()
        loc = self.cfg.location_id
        out: List[MDNSStation] = []
        for d0 in found:
            if org and d0.org_id and d0.org_id != org:
                continue
            if loc and d0.location_id and d0.location_id != loc:
                continue
            d = dataclasses.replace(d0, addresses=[a for a in d0.addresses if private_ip(a)], hostname="")
            ep: Optional[LocalEndpoint] = None
            if st is not None:
                for e in st.stations:
                    if e.station_id == d.station_id:
                        ep = e
            if ep is not None:
                if not ep.local_enabled:
                    continue
                d.fingerprint, d.tls_mode = normalize_fingerprint(ep.cert_fingerprint_sha256), "self_signed"
                if ep.port > 0:
                    d.port = ep.port
            elif not self.user_pinned(d.fingerprint):
                continue
            if not d.fingerprint or not d.addresses:
                continue
            out.append(d)
        return out

    def mdns_cached(self, fresh: bool) -> Optional[List[MDNSStation]]:
        with self.lock:
            if not fresh and self.mdns_at is not None and self.now() - self.mdns_at < MDNS_CACHE_TTL:
                return list(self.mdns)
        return None

    def set_mdns(self, found: List[MDNSStation]) -> None:
        with self.lock:
            self.mdns, self.mdns_at = list(found), self.now()

    def remember_job(self, job_id: str, url: str) -> None:
        if not job_id:
            return
        with self.lock:
            if len(self.job_urls) >= 1000:
                self.job_urls = {}
            self.job_urls[job_id] = url

    def job_url(self, job_id: str) -> Optional[str]:
        with self.lock:
            return self.job_urls.get(job_id)

    def mint_body(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {"device_name": self.cfg.device_name}
        if self.cfg.location_id:
            body["location_ids"] = [self.cfg.location_id]
        if self.cfg.token_ttl_days:
            body["ttl_days"] = self.cfg.token_ttl_days
        return body

    def remint_allowed(self, code: Optional[str]) -> bool:
        """Re-mint only for ``token_expired`` (never ``device_revoked``), only for minted tokens."""
        return not self.cfg.token and self.has_api_key and code == "token_expired"

    def discovered_stations(self, st: Optional[StoredToken], found: Sequence[MDNSStation]) -> List[LocalStation]:
        out: List[LocalStation] = []
        idx: Dict[str, int] = {}
        loc = self.cfg.location_id
        if st is not None:
            for ep in st.stations:
                if loc and ep.location_id and ep.location_id != loc:
                    continue
                idx[ep.station_id] = len(out)
                out.append(
                    LocalStation(
                        station_id=ep.station_id,
                        name=ep.name,
                        org_id=self.org_hint(),
                        location_id=ep.location_id,
                        port=ep.port,
                        addresses=list(ep.addresses),
                        urls=[t.url for t in endpoint_targets(ep, "cloud")],
                        tls_mode=ep.tls_mode,
                        fingerprint=normalize_fingerprint(ep.cert_fingerprint_sha256),
                        from_cloud=True,
                        local_enabled=ep.local_enabled,
                        connected=ep.connected,
                    )
                )
        for d in self.accepted_mdns(found, st):
            urls = [t.url for t in mdns_targets(d)]
            i = idx.get(d.station_id)
            if i is not None:
                ls = out[i]
                ls.from_mdns = True
                ls.addresses += [a for a in d.addresses if a not in ls.addresses]
                ls.urls += [u for u in urls if u not in ls.urls]
                continue
            idx[d.station_id] = len(out)
            out.append(
                LocalStation(
                    station_id=d.station_id,
                    name=d.instance,
                    org_id=d.org_id,
                    location_id=d.location_id,
                    port=d.port,
                    addresses=list(d.addresses),
                    urls=urls,
                    tls_mode=d.tls_mode,
                    fingerprint=d.fingerprint,
                    from_mdns=True,
                    local_enabled=True,
                )
            )
        return out


def no_token_error(err: BaseException) -> CatzPrintError:
    return CatzPrintError(f"No local token (the cloud couldn't mint one): {err}", code="no_local_token")


def static_token_mode(cfg: Optional[LocalConfig]) -> bool:
    """A paired device: local mode with a static token works without an API key."""
    return cfg is not None and cfg.mode == MODE_LOCAL and bool(cfg.token)
