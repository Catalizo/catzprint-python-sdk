"""A tiny webhook receiver (standard library only) that verifies CatzPrint signatures.

    CATZPRINT_WEBHOOK_SECRET=whsec_... python examples/verify_webhook.py [port]

Then point a webhook endpoint at http://<host>:<port>/hooks/catzprint. To try it
locally without CatzPrint, run with --self-test: it signs a sample event exactly
like the server does and posts it to itself.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from catzprint import SignatureVerificationError, webhooks

SECRET = os.environ.get("CATZPRINT_WEBHOOK_SECRET", "whsec_example_secret")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/hooks/catzprint":
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))  # the raw bytes, not re-encoded JSON
        try:
            event = webhooks.verify(raw, self.headers.get("CatzPrint-Signature"), SECRET)
        except SignatureVerificationError as e:
            self.send_error(400, f"bad signature: {e.message}")
            return
        if event.type == "job.printed" and event.job:
            print(f"Job {event.job.id} printed (reference {event.job.reference!r})")
        else:
            print(f"Got {event.type}: {json.dumps(event.data)}")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def self_test(port: int) -> None:
    body = json.dumps(
        {
            "type": "job.printed",
            "org_id": "org",
            "created_at": "2026-10-12T09:14:03Z",
            "data": {"job": {"id": "6f1c2a7e", "status": "printed", "reference": "order-1042"}},
        },
        separators=(",", ":"),
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hooks/catzprint",
        data=body,
        headers={"Content-Type": "application/json", "CatzPrint-Signature": webhooks.sign(body, SECRET)},
    )
    with urllib.request.urlopen(req) as resp:
        print(f"Self-test answered {resp.status}")


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    port = int(args[0]) if args else 8787
    server = HTTPServer(("127.0.0.1", port), Handler)
    if "--self-test" in argv:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self_test(port)
        server.shutdown()
        return 0
    print(f"Listening on http://127.0.0.1:{port}/hooks/catzprint")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
