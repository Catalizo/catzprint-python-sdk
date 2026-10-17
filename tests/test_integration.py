"""Integration tests against a live CatzPrint API.

Run with ``CATZPRINT_TEST_FIXTURE=/path/to/fixture.json pytest -m integration``.
The fixture holds base_url, API keys, printer ids and a station device token.
Only jobs are created; nothing else in the org is changed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator

import httpx
import pytest

from catzprint import (
    AsyncCatzPrint,
    AuthenticationError,
    CatzPrint,
    Job,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from conftest import fixture_path

pytestmark = pytest.mark.integration

_path = fixture_path()
if not _path:
    pytest.skip("CATZPRINT_TEST_FIXTURE is not set", allow_module_level=True)

with open(_path) as _f:
    FX: Dict[str, Any] = json.load(_f)

BASE = FX["base_url"].rstrip("/")

# A minimal valid one-page PDF.
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture
def test_client() -> Iterator[CatzPrint]:
    with CatzPrint(FX["api_key_test"], base_url=BASE) as c:
        yield c


@pytest.fixture
def live_client() -> Iterator[CatzPrint]:
    with CatzPrint(FX["api_key_live"], base_url=BASE) as c:
        yield c


def play_station(*job_ids: str) -> None:
    """Act as the paired station: fetch pending jobs (marks them sent), then report ours printed."""
    headers = {"Authorization": f"Bearer {FX['device_token']}"}
    with httpx.Client(base_url=BASE, headers=headers, timeout=30) as h:
        r = h.get("/v1/device/jobs")
        assert r.status_code == 200, r.text
        for jid in job_ids:
            r = h.post(f"/v1/device/jobs/{jid}/status", json={"status": "printed"})
            assert r.status_code in (200, 204), r.text


def test_key_returns_org(test_client: CatzPrint) -> None:
    info = test_client.key()
    assert info.org_id == FX["org_id"]
    assert info.live is False and "jobs:write" in info.scopes
    assert test_client.whoami().org_name == info.org_name


def test_text_print_with_test_key_then_station_prints_it(test_client: CatzPrint) -> None:
    job = test_client.print_text(
        "CatzPrint Python SDK\nintegration test", printer_id=FX["receipt_printer_id"], reference="py-sdk-text"
    )
    assert job.live is False
    assert job.billed_as == "test"
    assert job.source == "api"
    assert job.status in ("queued", "sent")
    play_station(job.id)
    done = test_client.jobs.wait_for(job.id, timeout=30, interval=0.5)
    assert done.status == "printed" and done.printed_at is not None
    full = test_client.jobs.get(job.id)
    assert [e.status for e in full.events][-1] == "printed"


def test_live_sealed_pdf_to_office_printer(live_client: CatzPrint) -> None:
    job = live_client.print_pdf(PDF, printer_id=FX["office_printer_id"], sealed=True, title="Sealed PDF (py sdk)")
    assert job.live is True and job.kind == "pdf"
    assert job.billed_as != "test"
    play_station(job.id)
    done = live_client.jobs.wait_for(job.id, timeout=30, interval=0.5)
    assert done.status == "printed"

    # The job shows up in the list.
    found = [j.id for j in live_client.jobs.iter(printer_id=FX["office_printer_id"], max_items=200)]
    assert job.id in found
    page = live_client.jobs.list(q=job.id, limit=5)
    assert [j.id for j in page] == [job.id]


def test_list_includes_job_and_cancel_scheduled(test_client: CatzPrint) -> None:
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    job = test_client.print_text("Scheduled (py sdk)", printer_id=FX["receipt_printer_id"], scheduled_for=when)
    assert job.status == "scheduled"
    assert job.scheduled_for is not None and abs((job.scheduled_for - when).total_seconds()) < 1
    listed = test_client.jobs.list(status="scheduled", printer_id=FX["receipt_printer_id"], limit=200)
    assert job.id in [j.id for j in listed]
    cancelled = test_client.jobs.cancel(job.id)
    assert cancelled.status == "cancelled"
    assert test_client.jobs.get(job.id).status == "cancelled"


def test_read_only_key_cannot_create() -> None:
    with CatzPrint(FX["api_key_printers_read_only"], base_url=BASE) as c:
        assert c.printers.list()  # it may read printers
        with pytest.raises(PermissionDeniedError) as ei:
            c.print_text("nope", printer_id=FX["receipt_printer_id"])
        assert ei.value.status == 403


def test_bad_key_is_authentication_error() -> None:
    with CatzPrint("cz_live_thisKeyDoesNotExist000000000000000000000", base_url=BASE) as c:
        with pytest.raises(AuthenticationError) as ei:
            c.key()
        assert ei.value.status == 401


def test_unknown_printer(test_client: CatzPrint) -> None:
    with pytest.raises((NotFoundError, ValidationError)) as ei:
        test_client.print_text("x", printer_id=str(uuid.uuid4()))
    assert ei.value.status in (400, 404, 422)
    if isinstance(ei.value, ValidationError):
        assert "printer_id" in ei.value.fields
    with pytest.raises(NotFoundError):
        test_client.jobs.get(str(uuid.uuid4()))


def test_idempotency_returns_same_job(test_client: CatzPrint) -> None:
    key = f"py-sdk-{uuid.uuid4()}"
    a = test_client.print_text("idempotent (py sdk)", printer_id=FX["receipt_printer_id"], idempotency_key=key)
    b = test_client.print_text("idempotent (py sdk)", printer_id=FX["receipt_printer_id"], idempotency_key=key)
    assert a.id == b.id
    play_station(a.id)


def test_reads(live_client: CatzPrint) -> None:
    printers = live_client.printers.list()
    ids = {p.id for p in printers}
    assert FX["receipt_printer_id"] in ids and FX["office_printer_id"] in ids
    locs = live_client.locations.list()
    assert FX["location_id"] in {loc.id for loc in locs}
    loc = live_client.locations.get(FX["location_id"])
    assert loc.id == FX["location_id"] and loc.name
    templates = live_client.templates.list()
    assert isinstance(templates, list)


async def test_async_client_round_trip() -> None:
    async with AsyncCatzPrint(FX["api_key_test"], base_url=BASE) as c:
        assert (await c.key()).org_id == FX["org_id"]
        job: Job = await c.print_receipt(
            [{"type": "text", "text": "Async receipt", "bold": True, "align": "center"}, {"type": "cut"}],
            printer_id=FX["receipt_printer_id"],
        )
        assert job.live is False and job.billed_as == "test"
        play_station(job.id)
        done = await c.jobs.wait_for(job.id, timeout=30, interval=0.5)
        assert done.status == "printed"
