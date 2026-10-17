# CatzPrint for Python

The official Python SDK for [CatzPrint](https://catzprint.com): print receipts, PDFs, images and
templates to any printer connected through a CatzPrint station, from a sync or an asyncio app.

- Sync `CatzPrint` and async `AsyncCatzPrint` clients (built on `httpx`)
- Typed models (`Job`, `Printer`, `Location`, `Template`, `ApiKeyInfo`), `py.typed` included
- Safe automatic retries with idempotency keys
- Webhook signature verification
- Optional end-to-end sealed printing (NaCl sealed box)
- Python 3.9+

## Install

```bash
pip install catzprint
pip install "catzprint[sealed]"   # adds PyNaCl for sealed printing
```

## Quickstart

```python
from catzprint import CatzPrint

client = CatzPrint("cz_test_...")          # or set CATZPRINT_API_KEY
job = client.print_text("Hello from Python!", role="receipt")
print(job.id, job.status)                  # 6f1c2a7e-… queued

done = client.jobs.wait_for(job.id)        # polls until printed / failed / cancelled
print(done.status)                         # printed
```

## Configuration

```python
client = CatzPrint(
    api_key="cz_live_...",                  # default: env CATZPRINT_API_KEY
    base_url="https://api-v1.catzprint.com",# default; or env CATZPRINT_BASE_URL
    org_id=None,                            # default: looked up once with GET /v1/key
    timeout=30.0,                           # seconds per request
    max_retries=2,                          # retries on network errors, 429, 502, 503, 504
)
```

Use it as a context manager (`with CatzPrint() as client:`) or call `client.close()` when done.
You can pass your own `httpx.Client` (or `httpx.AsyncClient`) with `http_client=` for proxies or
custom TLS settings.

The API key is never logged and is redacted in `repr()` (`cz_live_****`).

## Async

Every method has the same name and arguments on `AsyncCatzPrint`; just `await` it.

```python
import asyncio
from catzprint import AsyncCatzPrint

async def main() -> None:
    async with AsyncCatzPrint() as client:
        job = await client.print_text("Hello", role="receipt")
        job = await client.jobs.wait_for(job.id)
        async for j in client.jobs.iter(status="failed"):
            print(j.id, j.status_detail)

asyncio.run(main())
```

## Methods

### Your key and organization

```python
info = client.key()          # alias: client.whoami()
info.org_id, info.org_name, info.live, info.scopes
client.org_id()              # cached org id
```

### Printing helpers

All helpers accept the routing and job options below and return a `Job`.

```python
client.print_text("Table 4: 2 x soup", printer_id="...")
client.print_pdf("invoice.pdf", role="office")                 # path or bytes
client.print_image(open("logo.png", "rb").read(), printer_id="...")
client.print_receipt([{"type": "text", "text": "Hi"}, {"type": "cut"}], role="receipt")
client.print_template("tpl_id", {"order": "1042", "total": "9.50"}, role="kitchen")
```

### `jobs.create`

```python
from datetime import datetime, timedelta, timezone

job = client.jobs.create(
    # where (pick one, or none to let routing rules decide)
    printer_id="...", location_id="...", role="receipt",
    # what (exactly one)
    text="Hello",                        # or
    # commands=[...],                    # ESC/POS command list, see "Receipt commands"
    # template_id="...", data={...},     # saved template
    # content=pdf_bytes, file_name="a.pdf", kind="pdf",  # PDF / image / raw ESC/POS / office document
    # options
    title="Order 1042", reference="order-1042", copies=2,
    idempotency_key="order-1042",        # generated (UUIDv4) when omitted
    scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
    options={},
    sealed=False,                        # True encrypts text/content to the server key
)
```

`kind` is inferred: `text`, `commands`, `template`; for `content` it is `pdf` or `image` from the file
signature, otherwise `document` (the server converts office files). Pass `kind="escpos"` for raw ESC/POS bytes.

### Reading, listing, changing jobs

```python
job = client.jobs.get(job_id)            # includes job.events (history)
page = client.jobs.list(status=["queued", "sent"], printer_id="...", limit=50)
for j in page: ...
if page.next_cursor:
    page = client.jobs.list(cursor=page.next_cursor)

for j in client.jobs.iter(status="printed", since="2026-10-01T00:00:00Z"):  # all pages
    ...

client.jobs.cancel(job_id)               # scheduled, held, queued or sent jobs
client.jobs.retry(job_id)                # failed or held jobs
new_job = client.jobs.reprint(job_id, printer_id="...", copies=1)
done = client.jobs.wait_for(job_id, timeout=60, interval=1)  # raises WaitTimeoutError
```

Job statuses: `scheduled`, `held`, `queued`, `sent`, `printing`, `printed`, `failed`, `cancelled`.
`job.is_final` is true for the last three.

`jobs.list` filters: `status` (string or list), `printer_id`, `station_id`, `location_id`, `source`,
`live`, `q` (reference, title or id prefix), `since`, `limit` (1-200), `cursor`.

### Printers, locations, templates

```python
for p in client.printers.list():
    print(p.name, p.role, p.status, p.capabilities)
locations = client.locations.list()
shop = client.locations.get(location_id)
templates = client.templates.list()
```

### Receipt commands

`catzprint.commands` builds the JSON command list (or write the dicts yourself):

```python
from catzprint import commands as c

client.print_receipt([
    c.text("Corner Cafe", align="center", bold=True, size=2),
    c.line(),
    c.columns(("2 x Latte", 0.7), ("7.00", 0.3, "right")),
    c.barcode("1042", format="code128"),
    c.qr("https://example.com/r/1042"),
    c.image(open("logo.png", "rb").read(), align="center"),
    c.feed(2), c.beep(), c.drawer(), c.cut(),
], role="receipt")
```

## Errors

Every error is a `catzprint.CatzPrintError` with `status`, `code`, `message`, `fields` and `retry_after`.

| Exception | When |
|---|---|
| `AuthenticationError` | 401: missing, wrong, revoked or expired key |
| `PermissionDeniedError` (alias `catzprint.PermissionError`) | 403: the key lacks a scope, or a test key touched a live job |
| `NotFoundError` | 404 |
| `ValidationError` | 400 / 422; `e.fields` maps field names to messages |
| `OutOfPrintsError` | 402 / `out_of_prints` |
| `ConflictError` | 409, e.g. cancelling a printed job |
| `RateLimitError` | 429; `e.retry_after` seconds |
| `ServerError` | 5xx |
| `APIConnectionError` (alias `catzprint.ConnectionError`) | network error or timeout |
| `WaitTimeoutError` (alias `catzprint.TimeoutError`) | `jobs.wait_for` timed out; `e.job` is the last state |
| `SignatureVerificationError` | webhook signature didn't verify |

```python
from catzprint import CatzPrintError, ValidationError

try:
    client.print_text("Hi", printer_id="nope")
except ValidationError as e:
    print(e.fields)          # {'printer_id': 'Unknown printer.'}
except CatzPrintError as e:
    print(e.status, e.code, e.message)
```

## Retries and idempotency

Requests are retried up to `max_retries` times (default 2) with exponential backoff and jitter on
network errors, 429 (waiting `Retry-After` / `retry_after_seconds`, up to 60 s) and 502/503/504.

A `POST` is only retried when it carries an idempotency key. `jobs.create` (and every `print_*` helper)
always sends one, generating a UUIDv4 when you don't pass `idempotency_key`, so a retry can never print
twice. Pass your own key (an order number, say) to make your *own* retries safe too: repeating a key
returns the first job instead of printing again. `cancel`, `retry` and `reprint` are never retried
automatically.

## Webhooks

CatzPrint signs deliveries with `CatzPrint-Signature: t=<unix>,v1=<hex HMAC-SHA256>`. Verify the
**raw** body with `webhooks.verify` (alias `construct_event`); it checks the signature in constant time,
rejects timestamps more than `tolerance` seconds (default 300) away, accepts several `v1` entries and
several secrets (while rotating), and returns a `WebhookEvent`.

Every event carries `event.id`, the delivery id, which stays the same across retries. **Deduplicate on
`event.id`**: the timestamp check limits the replay window to 5 minutes, it does not stop a captured
delivery from being replayed within that window (and retries can deliver the same event twice).

### Flask

```python
import os
from flask import Flask, request
from catzprint import SignatureVerificationError, webhooks

app = Flask(__name__)
SECRET = os.environ["CATZPRINT_WEBHOOK_SECRET"]   # whsec_...

@app.post("/hooks/catzprint")
def catzprint_hook():
    try:
        event = webhooks.verify(request.get_data(), request.headers.get("CatzPrint-Signature"), SECRET)
    except SignatureVerificationError:
        return "bad signature", 400
    if event.type == "job.printed" and event.job:
        mark_order_printed(event.job.reference)   # your code
    return "", 204
```

### FastAPI

```python
from fastapi import FastAPI, Header, HTTPException, Request, Response
from catzprint import SignatureVerificationError, webhooks

app = FastAPI()

@app.post("/hooks/catzprint")
async def catzprint_hook(request: Request, catzprint_signature: str = Header(None)):
    try:
        event = webhooks.verify(await request.body(), catzprint_signature, SECRET)
    except SignatureVerificationError:
        raise HTTPException(status_code=400, detail="bad signature")
    ...
    return Response(status_code=204)
```

### Django

```python
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from catzprint import SignatureVerificationError, webhooks

@csrf_exempt
@require_POST
def catzprint_hook(request):
    try:
        event = webhooks.verify(request.body, request.headers.get("CatzPrint-Signature"), SECRET)
    except SignatureVerificationError:
        return HttpResponseBadRequest("bad signature")
    ...
    return HttpResponse(status=204)
```

Events: `job.created`, `job.printed`, `job.failed`, `job.cancelled`, `job.retried`, `station.online`,
`station.offline`, `printer.status` and `ping` (test). Answer quickly and do slow work in the background.

To test your handler, sign a payload yourself: `webhooks.sign(body, secret)` produces the same header
the server sends.

## Sealed printing

With `sealed=True` the SDK encrypts the text or file on your machine to the CatzPrint server's public key
(`GET /v1/public/server-key`, fetched once and cached) using a NaCl sealed box
(X25519 + XSalsa20-Poly1305), and sends it as `sealed_content`. The server re-seals it to the
station's key, so the document is never sent in the clear. Requires `pip install "catzprint[sealed]"`.

```python
client.print_pdf("payslip.pdf", role="office", sealed=True)
client.print_text("Door code: 4512", printer_id="...", sealed=True)
```

Receipt commands and templates are rendered by the server and can't be sealed.

## Test and live keys

- `cz_test_…` keys create test jobs: they print, but are never billed (`job.live is False`,
  `job.billed_as == "test"`) and can only change test jobs.
- `cz_live_…` keys create real, billed jobs.

`client.is_test_key` tells you which one you are using. Keys have scopes (`jobs:write`, `jobs:read`,
`printers:read`, `locations:read`); calling something outside them raises `PermissionDeniedError`.

## Examples

See [`examples/`](examples): `print_receipt.py`, `print_pdf_and_wait.py`, `verify_webhook.py`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                                    # unit tests
CATZPRINT_TEST_FIXTURE=/path/fixture.json .venv/bin/pytest          # + integration tests
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

## License

MIT, Copyright (c) Catalizo.
