"""Typed models for API objects.

Every model keeps the full decoded JSON in ``raw``, so fields added to the API
later are still reachable (``job.raw["new_field"]``) and unknown fields never
cause errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Mapping, Optional

__all__ = [
    "FINAL_JOB_STATUSES",
    "JOB_STATUSES",
    "ApiKeyInfo",
    "Job",
    "JobEvent",
    "JobList",
    "Location",
    "Printer",
    "Template",
    "WebhookEvent",
    "parse_time",
]

JOB_STATUSES = ("scheduled", "held", "queued", "sent", "printing", "printed", "failed", "cancelled")
FINAL_JOB_STATUSES = frozenset({"printed", "failed", "cancelled"})

_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_time(value: Any) -> Optional[datetime]:
    """Parse an RFC 3339 timestamp (as Go writes it, up to nanoseconds) to an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    tz_s = m.group(8)
    tz: timezone = timezone.utc
    if tz_s and tz_s not in ("Z", "z"):
        sign = 1 if tz_s[0] == "+" else -1
        digits = tz_s[1:].replace(":", "")
        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
    except ValueError:
        return None


def _s(d: Mapping[str, Any], k: str) -> str:
    v = d.get(k)
    return v if isinstance(v, str) else ""


def _os(d: Mapping[str, Any], k: str) -> Optional[str]:
    v = d.get(k)
    return v if isinstance(v, str) else None


def _b(d: Mapping[str, Any], k: str) -> bool:
    return bool(d.get(k))


def _i(d: Mapping[str, Any], k: str) -> int:
    v = d.get(k)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return 0


def _strs(d: Mapping[str, Any], k: str) -> List[str]:
    v = d.get(k)
    return [str(x) for x in v] if isinstance(v, list) else []


@dataclass
class ApiKeyInfo:
    """What ``GET /v1/key`` says about the API key in use."""

    id: str
    org_id: str
    org_name: str
    live: bool
    scopes: List[str]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ApiKeyInfo:
        return cls(
            id=_s(d, "id"),
            org_id=_s(d, "org_id"),
            org_name=_s(d, "org_name"),
            live=_b(d, "live"),
            scopes=_strs(d, "scopes"),
            raw=dict(d),
        )


@dataclass
class JobEvent:
    """One step in a job's history (returned by ``jobs.get``)."""

    at: Optional[datetime]
    status: str
    detail: str
    actor: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> JobEvent:
        return cls(
            at=parse_time(d.get("at")),
            status=_s(d, "status"),
            detail=_s(d, "detail"),
            actor=_s(d, "actor"),
            raw=dict(d),
        )


@dataclass
class Job:
    """A print job."""

    id: str
    live: bool
    source: str
    kind: str
    status: str
    status_detail: str
    title: str
    reference: str
    copies: int
    printer_id: Optional[str]
    printer_name: Optional[str]
    station_id: Optional[str]
    station_name: Optional[str]
    location_id: Optional[str]
    location_name: Optional[str]
    size: int
    attempts: int
    billed_as: str
    created_by: Optional[str]
    reprint_of: Optional[str]
    scheduled_for: Optional[datetime]
    created_at: Optional[datetime]
    sent_at: Optional[datetime]
    printed_at: Optional[datetime]
    finished_at: Optional[datetime]
    events: List[JobEvent] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_final(self) -> bool:
        """True once the job is printed, failed or cancelled."""
        return self.status in FINAL_JOB_STATUSES

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], events: Any = None) -> Job:
        return cls(
            id=_s(d, "id"),
            live=_b(d, "live"),
            source=_s(d, "source"),
            kind=_s(d, "kind"),
            status=_s(d, "status"),
            status_detail=_s(d, "status_detail"),
            title=_s(d, "title"),
            reference=_s(d, "reference"),
            copies=_i(d, "copies"),
            printer_id=_os(d, "printer_id"),
            printer_name=_os(d, "printer_name"),
            station_id=_os(d, "station_id"),
            station_name=_os(d, "station_name"),
            location_id=_os(d, "location_id"),
            location_name=_os(d, "location_name"),
            size=_i(d, "size"),
            attempts=_i(d, "attempts"),
            billed_as=_s(d, "billed_as"),
            created_by=_os(d, "created_by"),
            reprint_of=_os(d, "reprint_of"),
            scheduled_for=parse_time(d.get("scheduled_for")),
            created_at=parse_time(d.get("created_at")),
            sent_at=parse_time(d.get("sent_at")),
            printed_at=parse_time(d.get("printed_at")),
            finished_at=parse_time(d.get("finished_at")),
            events=[JobEvent.from_dict(e) for e in events if isinstance(e, dict)] if isinstance(events, list) else [],
            raw=dict(d),
        )


@dataclass
class JobList:
    """One page of jobs. Iterate it for the jobs on this page; ``next_cursor`` fetches the next."""

    items: List[Job]
    next_cursor: Optional[str]
    counts: Dict[str, int] = field(default_factory=dict)

    def __iter__(self) -> Iterator[Job]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def has_more(self) -> bool:
        return bool(self.next_cursor)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> JobList:
        items = d.get("items")
        counts = d.get("counts")
        return cls(
            items=[Job.from_dict(j) for j in items if isinstance(j, dict)] if isinstance(items, list) else [],
            next_cursor=_os(d, "next_cursor") or None,
            counts={str(k): _i(counts, k) for k in counts} if isinstance(counts, dict) else {},
        )


@dataclass
class Printer:
    """A printer attached to a station."""

    id: str
    station_id: str
    station_name: str
    location_id: Optional[str]
    device_key: str
    name: str
    role: str
    connection: str
    address: str
    paper: str
    capabilities: List[str]
    enabled: bool
    status: str
    status_detail: str
    shared_with_individuals: bool
    station_enabled: bool
    station_connected: bool
    updated_at: Optional[datetime]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Printer:
        return cls(
            id=_s(d, "id"),
            station_id=_s(d, "station_id"),
            station_name=_s(d, "station_name"),
            location_id=_os(d, "location_id"),
            device_key=_s(d, "device_key"),
            name=_s(d, "name"),
            role=_s(d, "role"),
            connection=_s(d, "connection"),
            address=_s(d, "address"),
            paper=_s(d, "paper"),
            capabilities=_strs(d, "capabilities"),
            enabled=_b(d, "enabled"),
            status=_s(d, "status"),
            status_detail=_s(d, "status_detail"),
            shared_with_individuals=_b(d, "shared_with_individuals"),
            station_enabled=_b(d, "station_enabled"),
            station_connected=_b(d, "station_connected"),
            updated_at=parse_time(d.get("updated_at")),
            raw=dict(d),
        )


@dataclass
class Location:
    """A place with stations (a shop, a kitchen, an office)."""

    id: str
    name: str
    address: str
    time_zone: str
    enabled: bool
    disabled_at: Optional[datetime]
    disabled_job_policy: str
    fallback_location_id: Optional[str]
    stations: int
    printers: int
    created_at: Optional[datetime]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Location:
        return cls(
            id=_s(d, "id"),
            name=_s(d, "name"),
            address=_s(d, "address"),
            time_zone=_s(d, "time_zone"),
            enabled=_b(d, "enabled"),
            disabled_at=parse_time(d.get("disabled_at")),
            disabled_job_policy=_s(d, "disabled_job_policy"),
            fallback_location_id=_os(d, "fallback_location_id"),
            stations=_i(d, "stations"),
            printers=_i(d, "printers"),
            created_at=parse_time(d.get("created_at")),
            raw=dict(d),
        )


@dataclass
class Template:
    """A saved receipt template (ESC/POS commands with ``{{placeholders}}``)."""

    id: str
    name: str
    paper: str
    body: Any
    version: int
    updated_at: Optional[datetime]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Template:
        return cls(
            id=_s(d, "id"),
            name=_s(d, "name"),
            paper=_s(d, "paper"),
            body=d.get("body"),
            version=_i(d, "version"),
            updated_at=parse_time(d.get("updated_at")),
            raw=dict(d),
        )


@dataclass
class WebhookEvent:
    """A verified webhook event (``job.printed``, ``station.offline``, ``ping``…)."""

    type: str
    created_at: Optional[datetime]
    org_id: str
    data: Dict[str, Any]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    #: Delivery id, identical across retries of one delivery. Deduplicate on it:
    #: the timestamp check only limits the replay window to the tolerance (5 min
    #: by default). ``None`` when the server did not send one.
    id: Optional[str] = None

    @property
    def job(self) -> Optional[Job]:
        """The job for ``job.*`` events, when the payload carries one."""
        j = self.data.get("job")
        return Job.from_dict(j) if isinstance(j, dict) else None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> WebhookEvent:
        data = d.get("data")
        return cls(
            type=_s(d, "type"),
            created_at=parse_time(d.get("created_at")),
            org_id=_s(d, "org_id"),
            data=dict(data) if isinstance(data, dict) else {},
            raw=dict(d),
            id=_os(d, "id"),
        )
