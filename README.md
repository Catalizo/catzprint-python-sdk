# CatzPrint for Python

The official Python SDK for [CatzPrint](https://catzprint.com): print receipts, PDFs, images and
templates to any printer connected through a CatzPrint station, from a sync or an asyncio app.

- Sync `CatzPrint` and async `AsyncCatzPrint` clients (built on `httpx`)
- Typed models (`Job`, `Printer`, `Location`, `Template`, `ApiKeyInfo`), `py.typed` included
- Safe automatic retries with idempotency keys
- Webhook signature verification
- Optional end-to-end sealed printing (NaCl sealed box)
- Local printing through the CatzPrint station on your network, also while the internet is down
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
    local=None,                             # LocalConfig(...): see "Local printing (offline)"
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

Printer names aren't unique (two stations can each have a "Kitchen"), so target printers by ID. `client.printers.list()` returns each printer's `id`, `station_id`, `station_name`, `location_id` and `role`. Optional shortcut: in the CatzPrint console, open **Locations & stations** (**My devices** in a personal account), then a station, and click the ID under a printer's name to copy it.

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

## Local printing (offline)

A restaurant must print when its internet is down. With `local=LocalConfig(...)`, the SDK sends jobs
straight to the CatzPrint Station (desktop or Android) on the same network (its local API on port 7443), and the
station reports everything it printed offline to the cloud when it's back: one job per idempotency key,
billed, with webhooks (`job.printed_locally`, `job.offline`, `job.duplicate_of`).

```python
from catzprint import CatzPrint, FileTokenStore, LocalConfig
from catzprint import commands as c

client = CatzPrint(
    "cz_live_...",                                   # or CATZPRINT_API_KEY
    local=LocalConfig(
        mode="local_first",
        location_id=location_id,
        discover=True,                               # the cloud's endpoint list + mDNS on the LAN
        token_store=FileTokenStore("/var/lib/catzserve/catzprint-local.json"),
        device_name="CatzServe hub",
    ),
)

# A kitchen ticket: an inverted ALLERGY banner, then the order.
job = client.jobs.create(
    role="kitchen",
    reference=f"order-{order.id}",
    # One key per ticket and attempt: the station and the cloud both dedupe on it,
    # so a fallback or a retry never prints twice.
    idempotency_key=f"order-{order.id}-kitchen-r1",
    commands=[
        c.text(" ALLERGY: PEANUTS ", align="center", bold=True, invert=True, size=2),
        c.text("Table 12", bold=True, size=2),
        c.line(),
        c.columns(c.column("2 x", 0.15), c.column("Pad thai", bold=True)),
        c.text("   no egg"),
        c.cut(),
    ],
)

# Or a saved template with data (templates are synced to the station):
job = client.jobs.create(
    role="kitchen",
    template_id=kitchen_template_id,
    data={"table": "12", "items": items},
    idempotency_key=f"order-{order.id}-kitchen-r1",
)
print(job.id, job.status, job.via, job.live)  # lj_… queued local True   (or … cloud)
```

`AsyncCatzPrint(local=LocalConfig(...))` works the same way (`await client.jobs.create(...)`).

### Modes

| Mode | Behaviour |
|---|---|
| `"cloud"` (default) | Unchanged: every job goes to the API. |
| `"local"` | Stations only. No station → `StationUnreachableError`. |
| `"local_first"` | Stations first; the cloud **only** when no station can be reached. |
| `"cloud_first"` | Cloud first (`cloud_timeout`); stations only when the job surely didn't reach the cloud (connect/TLS errors, `502`/`503`/`504`). A 4xx (validation, 402, 429…) is raised, not retried locally. |

(`catzprint.MODE_CLOUD`, `MODE_LOCAL`, `MODE_LOCAL_FIRST`, `MODE_CLOUD_FIRST` are the same strings.)

In `local_first` the SDK falls back to the cloud only when the stations are **unreachable**:
connection, TLS or timeout errors, `502`/`504`, and `503 local_disabled` (or a `503` without a code).
A station that answers is never second-guessed: these are raised to you, never sent to the cloud:

| Station answer | Python |
|---|---|
| `503 printer_offline` / `out_of_paper` / `cover_open` / `printer_error` (not accepted, not counted) | `PrinterError`, `is_printer_offline`, `is_out_of_paper`, `is_cover_open` |
| `422 no_printer` / `invalid` / `unsupported_locally` / `test_raw_not_allowed`, `400` | `ValidationError` |
| `402 offline_allowance_exhausted` (offline too long / offline prints used), `402 test_limit` | `OutOfPrintsError`, `is_offline_allowance_exhausted` |
| `403 device_not_approved` / `forbidden_location` | `PermissionDeniedError` |
| `409 duplicate`, `429 rate_limited` | `ConflictError`, `RateLimitError` |

**Never printed twice on an ambiguous outcome.** The SDK tells apart a job that surely didn't arrive
(DNS, connect, TLS or pin errors, a failed org-ID or server-key lookup, anything before the whole
request was written, and a station's `502`/`504`/`503 local_disabled`) from one that was sent but got
no usable answer (timeout, connection reset, a `2xx` whose body isn't a readable job, a too large
answer, a redirect):

- Not delivered: the next station, then (in `local_first`) the cloud, as above.
- Sent to a station, no answer: the SDK asks **that same station** for the job by its key
  (`GET /local/v1/jobs?idempotency_key=…`, up to 3 times within 5 s, same pinning). Found → that job is
  the result. `404 not_found` → the station never recorded it, so it counts as not delivered.
  `409 duplicate` → the key belongs to a job you can't see: that `ConflictError` is raised (no other
  station, no cloud). `409 in_progress` → not done yet, keep asking. Anything else, or still no answer →
  `OutcomeUnknownError` (code `outcome_unknown`, with `url` and `idempotency_key`): *"The station may
  have printed this job. Retry later with the same idempotency key."* Nothing else is tried.
- The cloud (`cloud_first`, and the cloud leg of `local_first`): not delivered or `502`/`503`/`504` →
  the stations (`cloud_first`) or a `StationUnreachableError` carrying the cloud error (`local_first`).
  Sent but no answer, or any other `5xx` (`500`, `501`, …) → **one** retry with the same key (the cloud
  dedupes on it); if that is ambiguous or unavailable again → `OutcomeUnknownError`, never the stations
  (the cloud may already have queued the job for another one). Any other cloud answer (a `4xx`) is
  raised as it is, also in `local_first` after the stations were unreachable.
- A station that answers the job POST itself with `409 in_progress` (the same key is being processed) is
  asked by key in the same way. Once any answer for the key (the POST or a probe) said `in_progress`, a
  `404` means "not recorded yet": no answer within the budget → `OutcomeUnknownError`, never a fallback.
- A cloud `409 duplicate` (e.g. "being created": another request with the same key is in progress), in
  any mode: the identical request (same body, same seal) is sent again after 1 s, up to 3 times. Still
  `409` → that `ConflictError` is raised. A re-send that isn't delivered or is ambiguous → `OutcomeUnknownError`.
- A cloud `400`/`422` naming `sealed_content` (the server key was rotated): the SDK drops the cached server
  key, fetches it again, re-seals and re-sends **once** with the same key. After an ambiguous attempt (an
  answer lost, a client retry, a `409`), a failed key fetch or a re-send that is rejected again, not
  delivered or ambiguous → `OutcomeUnknownError` (never a `ValidationError`, never the stations); without
  one, a repeated rejection is raised as the `ValidationError`.
- Cancelling an `AsyncCatzPrint` call (task cancel, `asyncio.wait_for` / `asyncio.timeout`) after the
  job was fully written to a station, while asking a station for it by key, or while the cloud POST was in
  flight in the local modes, raises `OutcomeUnknownError` wrapping the `CancelledError` (`e.cause`), never
  a fallback; cancelled before anything was written, you get the `CancelledError` itself. (The sync
  client has no cancellation; its timeouts apply.)

Retrying with the same idempotency key is always safe: stations and the cloud print a key once.
Check with `is_outcome_unknown(e)`.

A `401 token_expired` makes the SDK mint a fresh token once and retry (at most once per 10 minutes).
Any other `401` (`device_revoked`, `unauthenticated`), or a token that can't be renewed because the
cloud is down too: `local_first` uses the cloud and `local` raises the `AuthenticationError`.

The local modes always send an idempotency key; when you don't set one the SDK uses `"sdk-<uuid4>"`
and sends the **same** key to the station and the cloud. If a station printed a job and the cloud
later gets the same key, the cloud merges them instead of printing again. Use your own keys (like
`order-1042-kitchen-r1`) so your own retries are safe too. A key that is empty after trimming
whitespace counts as no key; a key with leading/trailing whitespace or control characters raises a
`ValidationError` (it is never silently changed).

Jobs that can't print locally (PDFs, images, Office documents, sealed or scheduled jobs) go straight
to the cloud in `local_first`/`cloud_first`, exactly as in mode `cloud` (the client's retries and
timeout, the `409` re-sends), and raise a `ValidationError` with code
`unsupported_locally` in `local` mode. When a job sets neither `printer_id` nor `location_id`,
`LocalConfig.location_id` is used.

```python
from catzprint import StationUnreachableError, is_out_of_paper, is_printer_offline

try:
    job = client.jobs.create(role="kitchen", text="...", idempotency_key=key)
except Exception as e:
    if is_printer_offline(e) or is_out_of_paper(e):
        ...  # tell the staff; retry later with the same key
    elif isinstance(e, StationUnreachableError):
        print("tried", e.urls, "cloud:", e.cloud_error)  # e.attempts: (url, error) pairs
    else:
        raise
```

### Finding and trusting stations

Stations are tried in this order: `station_urls`, then (with `discover=True`) the URLs from the
cloud's endpoint list (public-certificate hostnames before raw IPs), then current addresses found over
mDNS (`_catzprint._tcp`), browsed only when the others failed.

- **Public hostnames** (`https://192-168-1-20.<label>.local.catzprint.com:7443`) come **only** from
  the cloud endpoint list. They carry a Let's Encrypt certificate, verified like any website, **and**
  the certificate's public key must match the `public_spki_sha256` the cloud reports. The SDK
  connects to the IP in the name directly (so they work while public DNS is down) only for names
  under that station's own label.
- **IP URLs** are trusted **only** with a pinned SHA-256 fingerprint of the station's self-signed
  certificate: from the cloud endpoint list or `LocalConfig.fingerprints` (the station app shows it).
  Only private addresses (RFC 1918, IPv6 ULA, link-local) are used. Nothing is trusted on first use.
  The fingerprint is checked right after the TLS handshake, **before** any request byte (or token)
  is sent.
- **mDNS is never trusted on its own.** It only maps a station ID to its current address, for a
  station on the cloud endpoint list (pinned with the list's fingerprint) or one whose announced
  fingerprint is in `fingerprints`. The announced hostname is ignored. So a device on the LAN can't
  pose as your station, even with its own valid CatzPrint certificate.

The local token (`czl1…`) is minted with your API key (`POST /v1/orgs/{org}/local-tokens`, scope
`jobs:write`) and **bound to `location_id`** (required unless you set `all_locations=True`). It is
cached in the `token_store` with the endpoint list. There is no background thread: when the SDK uses
the token and it has less than min(7 days, half its lifetime) left (or the endpoint list is older than an hour), it renews
them right after that call, while online (a failed renewal is retried 5 minutes later; the old token
keeps working meanwhile). A station's `401 token_expired` causes one fresh mint and a retry, at most
once per 10 minutes per device (remembered in the store); `401 device_revoked` (the device was
revoked in the station app) is never answered with a new token. `FileTokenStore(path)` writes the
file with mode 0600 through an atomic rename, and refuses (POSIX) an existing file or directory that
isn't owned by the current user or is group/world-writable (error code `token_store_insecure`, see
`client.local.store_error`; printing goes on with the token in memory); the default store is in memory (`MemoryTokenStore`).
Your own store only needs `load(key) -> StoredToken | None` and `save(key, StoredToken | None)`.

Timeouts (seconds): `connect_timeout` (3, TLS included), `request_timeout` (15), `cloud_timeout`
(10: the cloud attempt in `cloud_first`, minting, refreshes), `discover_timeout` (1.5). `wait` (up to
30) asks the station to answer once the job printed or failed. Cancelling an async call never falls
back (see above). `ca_file` / `ca_data` replace the system roots for public station hostnames.

### Local helpers

```python
st = client.local.status()               # printers, paper, unsynced jobs, offline allowance
stations = client.local.discover()       # endpoint list + mDNS, merged
info = client.local.info(url)            # GET /local/v1/info
job = client.jobs.get("lj_…")            # local job IDs are read from the station (wait_for works too)
client.local.refresh()                   # renew token / endpoint list now
tok = client.local.token()               # the czl1 token, e.g. for a browser POS

# Pair a device with one station using the 8-digit code and the certificate fingerprint shown in the
# station app (or a station on the cloud endpoint list); the czd_ token works without any cloud
# account or API key.
p = client.local.pair("https://192.168.1.20:7443", "12345678", "Till 3", fingerprint="3f1a…")

# A paired till needs no API key: local mode with the device token.
till = CatzPrint(local=LocalConfig(
    mode="local",
    station_urls=["https://192.168.1.20:7443"],
    fingerprints={"192.168.1.20": "3f1a…"},
    token=p.token,
))
# (its cloud calls raise an AuthenticationError with code missing_api_key)

lt = client.local_tokens.create(device_name="Till 3", location_ids=[loc], ttl_days=30)
client.local_tokens.revoke(lt.token_id)
eps = client.local_endpoints.list(loc)
```

See [`examples/local_hub.py`](examples/local_hub.py) for a complete hub.

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
| `PrinterError` | a station's printer is offline, out of paper, open or in error (503) |
| `StationUnreachableError` | no station could be reached (`e.attempts`, `e.urls`, `e.cloud_error`) |
| `OutcomeUnknownError` | `outcome_unknown`: the job was sent but its fate is unknown; retry later with the same key (`e.url`, `e.idempotency_key`) |
| `PinMismatchError` | (in `e.attempts`) a station's certificate didn't match its pinned fingerprint |

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
(`GET /v1/public/server-key`, cached for an hour; fetched again and re-sealed once if the API can't
decrypt the job because the key was rotated) using a NaCl sealed box
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

See [`examples/`](examples): `print_receipt.py`, `print_pdf_and_wait.py`, `verify_webhook.py`, `local_hub.py`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                                    # unit tests
CATZPRINT_TEST_FIXTURE=/path/fixture.json .venv/bin/pytest          # + integration tests
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

## License

MIT, Copyright (c) Catalizo.
