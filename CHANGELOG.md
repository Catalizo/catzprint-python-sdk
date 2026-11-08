# Changelog

## 2.2.1 (2026-11-07)

Security audit round 7 (no API changes; sync and async clients).

- A cloud `400`/`422` naming `sealed_content` (the server key was rotated) is no longer final: the SDK drops the cached server key, fetches it again, re-seals and re-sends **once** with the same idempotency key. After an ambiguous attempt (an answer lost, a retry in flight, a `409 duplicate`), a failed key fetch, or a re-send that is rejected again, not delivered or ambiguous, is `OutcomeUnknown` — never a validation error, never local. Without one, a repeated rejection is returned as the validation error.
- The server key is cached for at most an hour (and dropped at once on a `sealed_content` rejection).
- Stored token placeholders are recognised by the same set in every SDK: `…`, a run of 3+ `*`, `[redacted]`, `redacted`, `hidden` (any case), optionally after `czl1.` or `czd_`. (`<redacted>` and `redacted token` are no longer treated as placeholders; `***` and `…` now are.)
- Once any answer for the key said `409 in_progress` (the job request or a probe by key), a later probe `404 not_found` is `OutcomeUnknown` (no next station, no cloud). (A job request answered `in_progress` already was.)
- A server-key fetch that was already in flight when a `sealed_content` rejection dropped the key no longer re-installs the dropped key.

## 2.2.0 (2026-11-07)

Local printing: print through the CatzPrint desktop station on the same network when the internet is
down. Same behaviour as the Go and Node SDKs 2.2.0.

- `CatzPrint(local=LocalConfig(...))` / `AsyncCatzPrint(local=...)` with modes `cloud` (default,
  unchanged), `local`, `local_first` and `cloud_first`. `local_first` falls back to the cloud only when
  no station can be reached (connect/TLS/timeout, 502/504, 503 `local_disabled` or a 503 without a
  code); `cloud_first` makes one cloud attempt (`cloud_timeout`) and falls back to the stations on a
  network error, timeout or 5xx. Printer errors, `no_printer`, validation errors, 402/403/409/429 are
  always raised, never retried elsewhere.
- The local modes always send an idempotency key (`"sdk-<uuid4>"` when you set none), the same one to
  the station and the cloud.
- Local tokens are minted (`POST /v1/orgs/{org}/local-tokens`, bound to `location_id` unless
  `all_locations=True`), cached in a token store (`FileTokenStore`: mode 0600, atomic writes;
  `MemoryTokenStore`; or your own `TokenStore`) with the station endpoint list, renewed on use when
  less than 7 days are left (no background thread), and the endpoint list is refreshed hourly while
  online. A station's `401 token_expired` causes one re-mint, at most once per 10 minutes per device
  (persisted); never on `device_revoked` or `unauthenticated`.
- TLS: public hostnames come only from the cloud endpoint list, are verified normally **and** pinned to
  `public_spki_sha256`, and are dialled at their dashed IP only under the station's own label. IP and
  self-signed stations only with a pinned certificate SHA-256 (endpoint list or
  `LocalConfig.fingerprints`), checked before any request byte is sent; private addresses only. mDNS
  (`_catzprint._tcp`, stdlib only) only maps a station ID to its address for stations on the cloud
  list or with a configured fingerprint; the announced hostname is ignored.
- A paired device (`mode="local"` with a static `czd_…` token) works without an API key; its cloud
  calls raise `AuthenticationError` with code `missing_api_key`.
- `client.local`: `status`, `discover`, `info`, `pair` (needs `fingerprint=` from the station app or a
  station on the cloud list), `get_job`, `refresh`, `token`, `mode`, `store_error`;
  `client.local_tokens.create/revoke`; `client.local_endpoints.list`. `jobs.get`/`wait_for` read local
  job IDs (`lj_…`) from the station.
- `Job.via`, `printed_locally`, `local_device`, `duplicate_of`, `offline`, `idempotency_key`, `local`
  (`LocalJob`; its `live` wins, paired devices are live).
- Errors: `PrinterError`, `StationUnreachableError` (`attempts`, `urls`, `cloud_error`),
  `PinMismatchError`; helpers `is_printer_offline`, `is_out_of_paper`, `is_cover_open`,
  `is_offline_allowance_exhausted`, `is_station_unreachable`. An error body's `param` now fills
  `fields` when `fields` is absent.
- New example: `examples/local_hub.py`. No new dependencies.

### Security (audit round 4, SDK addendum 2)

- Never print twice on an ambiguous outcome: a station that got the whole request but gave no usable
  answer (timeout, reset, unreadable/too large answer, redirect) is asked for the job by its key
  (`GET /local/v1/jobs?idempotency_key=…`, up to 3 times within 5 s); found → that job, `404` → as not
  delivered, otherwise `OutcomeUnknownError` (`outcome_unknown`, `is_outcome_unknown`) with no fallback.
  Cloud creates: not delivered / 502-504 → stations (`cloud_first`) or `StationUnreachableError`
  (`local_first`); no answer / 500 → one retry with the same key, then `OutcomeUnknownError`.
  `cloud_first` no longer falls back to the stations on a 500 or a cloud timeout after sending.
- Idempotency keys: empty after trimming = no key; leading/trailing whitespace or control characters →
  `ValidationError`.
- Tokens are renewed when less than min(7 days, TTL/2) is left (short TTLs no longer re-mint on every job).
- `FileTokenStore` refuses (POSIX) a store file or directory not owned by the current user or writable by
  group/others (`token_store_insecure`).
- Deeply nested JSON (API, station, token store, webhook, token claims) raises a `CatzPrintError` instead
  of `RecursionError`.
- The sdist no longer ships `tests/` (including the test-only certificates and keys).

### Security (audit round 5, SDK addendum 3)

- Probe by key: `404 not_found` → never recorded (next station / fallback); `409 duplicate` → raised as
  the answer (no other station, no cloud); anything else → keep probing, then `OutcomeUnknownError`.
- A cloud `409 duplicate` "being created" is re-sent (identical body, sealed jobs not re-sealed) after 1 s
  up to 3 times, in every mode; still `409` → that `ConflictError`; a re-send not delivered or ambiguous →
  `OutcomeUnknownError`. In mode cloud the generated key is fixed before the first send.
- `local_first`: a cloud `4xx` after the stations were unreachable is raised itself, not wrapped in
  `StationUnreachableError`; any `5xx` other than 502/503/504 is ambiguous (one retry).
- A failed org-ID / server-key lookup before the job POST counts as not delivered.
- A `2xx` with an unreadable body (not JSON, or no job id) to a job request is "delivered, no answer":
  stations are probed by key, the cloud gets its one retry (mode cloud: a `CatzPrintError` with code
  `invalid_response` instead of a `KeyError`).
- `AsyncCatzPrint`: a cancellation after the job was fully written (or during the probe, or during the
  cloud POST in the local modes) raises `OutcomeUnknownError` wrapping the `CancelledError`.

### Security (audit round 6, SDK addendum 4)

- A station's `409 in_progress`, to the key probe or to the job POST itself, means "not done yet": keep
  probing within the budget (after an in-progress POST a `404` is "not yet", not "never"), then
  `OutcomeUnknownError`; never a fallback.
- Jobs that can't print locally (sealed, PDF/image/document, scheduled) in `local_first`/`cloud_first`
  use the same cloud create as mode cloud (client retries and timeout, the 409 re-sends).
- Any cloud `409 duplicate` is re-sent (after 1 s, up to 3 times), not only "being created", like the
  other SDKs.
- Tokens: `LocalToken`, `LocalPairing` and `StoredToken` keep the token when serialized
  (`dataclasses.asdict`, `StoredToken.to_dict`); only `repr`/`str` redact. A stored token that is a
  redaction placeholder (`****`, `[REDACTED]`…) is treated as no token (a store error; a new one is minted).

## 2.1.4 (2026-10-20)

- Docs only: the README explains that printer names aren't unique across stations, how to target printers by ID, and the console's copy-ID shortcut.

## 2.1.3 (2026-10-17)

### Security

- The base URL must use `https://`; `http://` is accepted only for `localhost`, `127.0.0.1`, `::1` and
  `*.localhost`, so the API key is never sent in clear text.
- Redirects are never followed (sync and async clients), so the API key only reaches the configured host; a
  3xx answer raises an `unexpected_redirect` error.
- Response bodies are capped at 64 MiB; a larger answer is an error instead of being read into memory.
- Webhook verification hardened: missing/empty secrets are rejected, the timestamp must be all digits, and a
  `NaN` tolerance no longer disables the timestamp check.
- `WebhookEvent.id`: the delivery id the server now includes in every signed event (`None` if absent).
  Deduplicate on `event.id`; the timestamp check limits the replay window to 5 minutes, it does not stop
  replays within it.

## 1.0.0 (2026-10-16)

First release.

- `CatzPrint` (sync) and `AsyncCatzPrint` (asyncio) clients on `httpx`.
- Jobs: `create`, `get`, `list`, `iter` (auto-pagination), `cancel`, `retry`, `reprint`, `wait_for`.
- Helpers: `print_text`, `print_pdf`, `print_image`, `print_receipt`, `print_template`.
- `printers.list`, `locations.list`, `locations.get`, `templates.list`, `key()` / `whoami()`.
- Typed models with unknown fields kept in `.raw`; `py.typed`.
- Error hierarchy mapped from the API error shape.
- Retries with exponential backoff and jitter on network errors, 429 and 502/503/504; POSTs only
  retried with an idempotency key (always sent by `jobs.create`).
- Webhook signature verification (`webhooks.verify`, `construct_event`, `sign`).
- Sealed printing with the optional `[sealed]` extra (PyNaCl).
- `catzprint.commands` builders for ESC/POS receipt commands.
