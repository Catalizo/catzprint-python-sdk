"""mDNS (DNS-SD) browse for ``_catzprint._tcp`` stations, stdlib only.

mDNS is unauthenticated: what is parsed here is only ever used to map a station ID the SDK already
trusts (from the cloud endpoint list, or a user-pinned fingerprint) to its current address. See
``LocalState.accepted_mdns``.
"""

from __future__ import annotations

import contextlib
import selectors
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ._local_core import normalize_fingerprint

__all__ = ["MDNS_GROUP", "MDNS_SERVICE", "MDNSCollector", "MDNSStation", "browse", "build_query"]

#: The DNS-SD service type stations advertise.
MDNS_SERVICE = "_catzprint._tcp.local."
MDNS_GROUP: Tuple[str, int] = ("224.0.0.251", 5353)

_TYPE_A = 1
_TYPE_PTR = 12
_TYPE_TXT = 16
_TYPE_SRV = 33
_CLASS_IN_QU = 1 | 0x8000


@dataclass
class MDNSStation:
    """One station as announced over mDNS."""

    instance: str
    station_id: str
    org_id: str
    location_id: str
    port: int
    fingerprint: str
    tls_mode: str
    #: TXT ``host``: parsed, but never used to build a target (only the cloud list gives hostnames).
    hostname: str
    addresses: List[str] = field(default_factory=list)


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        b = label.encode("utf-8")
        if not b or len(b) > 63:
            raise ValueError(f"bad DNS label in {name!r}")
        out.append(len(b))
        out += b
    out.append(0)
    return bytes(out)


def build_query() -> bytes:
    """A PTR query for the CatzPrint service with the unicast-response (QU) bit set."""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(MDNS_SERVICE) + struct.pack("!HH", _TYPE_PTR, _CLASS_IN_QU)


class _Malformed(Exception):
    pass


def _read_name(msg: bytes, off: int) -> Tuple[str, int]:
    """Read a (possibly compressed) name at ``off``; return it (with a trailing dot) and the next offset."""
    labels: List[str] = []
    end = -1
    jumps = 0
    total = 0
    while True:
        if off >= len(msg):
            raise _Malformed()
        n = msg[off]
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(msg):
                raise _Malformed()
            ptr = ((n & 0x3F) << 8) | msg[off + 1]
            if end < 0:
                end = off + 2
            jumps += 1
            if jumps > 32 or ptr >= len(msg):
                raise _Malformed()
            off = ptr
            continue
        if n & 0xC0:
            raise _Malformed()
        off += 1
        if n == 0:
            break
        if off + n > len(msg):
            raise _Malformed()
        labels.append(msg[off : off + n].decode("utf-8", errors="replace"))
        total += n + 1
        if total > 255:
            raise _Malformed()
        off += n
    if end < 0:
        end = off
    return ".".join(labels) + ".", end


def _is_ipv4(s: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, s)
        return True
    except (OSError, ValueError):
        return False


def _is_service_instance(name: str) -> bool:
    return name.endswith("." + MDNS_SERVICE) and len(name) > len(MDNS_SERVICE) + 1


class MDNSCollector:
    """Accumulates records from any number of mDNS responses."""

    def __init__(self) -> None:
        self.ptr: Dict[str, bool] = {}  # instance names (lower case)
        self.srv: Dict[str, Tuple[int, str]] = {}  # instance -> (port, target lower case)
        self.txt: Dict[str, Dict[str, str]] = {}  # instance -> TXT key/values
        self.addr: Dict[str, List[str]] = {}  # host (lower case) -> IPv4s
        self.src: Dict[str, str] = {}  # instance -> source IP of the packet
        self.name: Dict[str, str] = {}  # instance lower -> instance as sent

    def add(self, msg: bytes, source: str = "") -> None:
        """Parse one response packet. Malformed packets and unrelated records are ignored."""
        if len(msg) < 12:
            return
        _id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", msg[:12])
        if not flags & 0x8000:  # not a response
            return
        src = source if _is_ipv4(source) else ""
        off = 12
        try:
            for _ in range(qd):
                _, off = _read_name(msg, off)
                off += 4
            sections = [an, ns, ar]
            for i, count in enumerate(sections):
                for _ in range(count):
                    off = self._record(msg, off, src, i != 1)
        except (_Malformed, struct.error, IndexError):
            return

    def _record(self, msg: bytes, off: int, src: str, use: bool) -> int:
        raw_name, off = _read_name(msg, off)
        if off + 10 > len(msg):
            raise _Malformed()
        rtype, _cls, _ttl, rdlen = (int(x) for x in struct.unpack("!HHIH", msg[off : off + 10]))
        off += 10
        end = off + rdlen
        if end > len(msg):
            raise _Malformed()
        if not use:
            return end
        name = raw_name.lower()
        if rtype == _TYPE_PTR:
            target, _ = _read_name(msg, off)
            if name == MDNS_SERVICE and _is_service_instance(target.lower()):
                self.ptr[target.lower()] = True
                self.name[target.lower()] = target
        elif rtype == _TYPE_SRV:
            if rdlen < 7:
                raise _Malformed()
            _prio, _weight, port = struct.unpack("!HHH", msg[off : off + 6])
            target, _ = _read_name(msg, off + 6)
            if _is_service_instance(name):
                self.srv[name] = (port, target.lower())
                self.name[name] = raw_name
                if src:
                    self.src[name] = src
        elif rtype == _TYPE_TXT:
            kv: Dict[str, str] = {}
            p = off
            while p < end:
                n = msg[p]
                p += 1
                if p + n > end:
                    raise _Malformed()
                s = msg[p : p + n].decode("utf-8", errors="replace")
                p += n
                k, _, v = s.partition("=")
                k = k.strip().lower()
                if k and k not in kv:  # RFC 6763: the first one wins
                    kv[k] = v
            if _is_service_instance(name):
                self.txt[name] = kv
                self.name[name] = raw_name
                if src:
                    self.src[name] = src
        elif rtype == _TYPE_A:
            if rdlen != 4:
                raise _Malformed()
            ip = socket.inet_ntoa(msg[off:end])
            lst = self.addr.setdefault(name, [])
            if ip not in lst:
                lst.append(ip)
        return end

    def stations(self) -> List[MDNSStation]:
        """Every complete, valid station seen so far, sorted by instance name."""
        out: List[MDNSStation] = []
        for n in sorted(k for k in self.txt if k in self.srv):
            kv = self.txt[n]
            srv_port, target = self.srv[n]
            if kv.get("v") != "1" or not kv.get("id"):
                continue
            port = srv_port
            try:
                p = int(kv.get("port", ""))
                if 0 < p < 65536:
                    port = p
            except ValueError:
                pass
            if port <= 0:
                continue
            fp = normalize_fingerprint(kv.get("fp", ""))
            if kv.get("fp") and not fp:
                continue  # malformed fingerprint: ignore the whole announcement
            inst = self.name.get(n, n)
            suffix = "." + MDNS_SERVICE
            if inst.lower().endswith(suffix):
                inst = inst[: -len(suffix)]
            st = MDNSStation(
                instance=inst,
                station_id=kv["id"],
                org_id=kv.get("org", ""),
                location_id=kv.get("loc", ""),
                port=port,
                fingerprint=fp,
                tls_mode=kv.get("tls", ""),
                hostname=kv.get("host", "").rstrip(".").lower(),
            )
            for ip in self.addr.get(target, []):
                if ip not in st.addresses:
                    st.addresses.append(ip)
            if not st.addresses and self.src.get(n):
                st.addresses = [self.src[n]]
            out.append(st)
        return out


def _local_ipv4s() -> List[str]:
    """Best-effort list of this host's IPv4 addresses (stdlib has no interface listing)."""
    out: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out


def browse(
    timeout: float, dst: Tuple[str, int] = MDNS_GROUP, cancel: Optional[threading.Event] = None
) -> List[MDNSStation]:
    """Send a query to ``dst`` (the mDNS group, or a unicast address in tests) and collect answers.

    Listens until ``timeout`` (seconds) passes or ``cancel`` is set; all sockets are closed when it
    returns. Raises ``OSError`` when no socket can be opened.
    """
    query = build_query()
    socks: List[socket.socket] = []
    multicast = dst[0].startswith("224.")
    sel = selectors.DefaultSelector()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        socks.append(s)
        s.bind(("0.0.0.0", 0))
        s.setblocking(False)
        if multicast:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            # Responders that ignore the QU bit answer to the group: listen there too when the port
            # can be shared (it can't always be).
            try:
                mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                socks.append(mc)
                mc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    mc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                mc.bind(("", MDNS_GROUP[1]))
                mreq = socket.inet_aton(MDNS_GROUP[0]) + socket.inet_aton("0.0.0.0")
                mc.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                mc.setblocking(False)
            except OSError:
                socks.pop().close()

        def send() -> None:
            sent = False
            if multicast:
                for ip in _local_ipv4s():  # every interface we can name
                    try:
                        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                        s.sendto(query, dst)
                        sent = True
                    except OSError:
                        continue
                with contextlib.suppress(OSError):
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("0.0.0.0"))
            if not sent:
                with contextlib.suppress(OSError):
                    s.sendto(query, dst)

        for sk in socks:
            sel.register(sk, selectors.EVENT_READ)
        col = MDNSCollector()
        start = time.monotonic()
        deadline = start + max(0.0, timeout)
        resend_at: Optional[float] = start + min(0.3, timeout / 3)
        send()
        while True:
            now = time.monotonic()
            if now >= deadline or (cancel is not None and cancel.is_set()):
                break
            if resend_at is not None and now >= resend_at:
                send()  # one resend shortly after the first query: UDP may drop packets
                resend_at = None
            wake = deadline if resend_at is None else min(deadline, resend_at)
            for key, _ in sel.select(min(0.05, max(0.0, wake - now))):
                rs = key.fileobj
                if not isinstance(rs, socket.socket):  # pragma: no cover
                    continue
                while True:
                    try:
                        data, addr = rs.recvfrom(9000)
                    except OSError:  # BlockingIOError: nothing more to read now
                        break
                    col.add(data, str(addr[0]))
        return col.stations()
    finally:
        sel.close()
        for sk in socks:
            sk.close()
