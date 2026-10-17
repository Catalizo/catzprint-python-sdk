# Changelog

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
