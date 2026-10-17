"""Sealed printing: encrypt in the SDK, decrypt with libsodium (PyNaCl) to prove compatibility."""

from __future__ import annotations

import base64

import pytest

from catzprint import AsyncCatzPrint, CatzPrint, seal, sealing_available
from conftest import KEY, ORG, FakeServer, job_json

nacl_public = pytest.importorskip("nacl.public")

JOBS = f"/v1/orgs/{ORG}/jobs"


def keypair() -> tuple:  # type: ignore[type-arg]
    sk = nacl_public.PrivateKey.generate()
    return sk, base64.b64encode(bytes(sk.public_key)).decode()


def test_seal_round_trip() -> None:
    assert sealing_available()
    sk, pk = keypair()
    msg = b"%PDF-1.4 confidential payroll"
    ct = seal(pk, msg)
    assert len(ct) == len(msg) + 48  # 32-byte ephemeral key + 16-byte MAC
    assert nacl_public.SealedBox(sk).decrypt(ct) == msg
    assert seal(pk, msg) != ct  # fresh ephemeral key every time


def test_seal_rejects_bad_key() -> None:
    with pytest.raises(ValueError):
        seal(base64.b64encode(b"short").decode(), b"x")


def test_sealed_job_body_decrypts(server: FakeServer) -> None:
    sk, pk = keypair()
    server.on(
        "GET",
        "/v1/public/server-key",
        (200, {}, {"public_key": pk, "algorithm": "x25519-xsalsa20-poly1305-sealed-box"}),
    )
    server.on("POST", JOBS, (201, {}, {"job": job_json(kind="pdf")}))
    pdf = b"%PDF-1.7 secret contract"
    with CatzPrint(KEY, base_url=server.url, org_id=ORG) as c:
        c.print_pdf(pdf, printer_id="p1", sealed=True)
        c.print_text("secret note", printer_id="p1", sealed=True)
    calls = server.calls("POST", JOBS)
    first, second = calls[0].json(), calls[1].json()
    assert "content" not in first and first["kind"] == "pdf"
    assert nacl_public.SealedBox(sk).decrypt(base64.b64decode(first["sealed_content"])) == pdf
    assert second["kind"] == "text"
    assert nacl_public.SealedBox(sk).decrypt(base64.b64decode(second["sealed_content"])) == b"secret note"
    # server key fetched once, without the API key
    key_calls = server.calls("GET", "/v1/public/server-key")
    assert len(key_calls) == 1 and "authorization" not in key_calls[0].headers


async def test_async_sealed_job(server: FakeServer) -> None:
    sk, pk = keypair()
    server.on("GET", "/v1/public/server-key", (200, {}, {"public_key": pk}))
    server.on("POST", JOBS, (201, {}, {"job": job_json(kind="image")}))
    png = b"\x89PNG\r\n\x1a\n fake image"
    async with AsyncCatzPrint(KEY, base_url=server.url, org_id=ORG) as c:
        await c.jobs.create(content=png, sealed=True, printer_id="p1")
    body = server.calls("POST", JOBS)[0].json()
    assert body["kind"] == "image"
    assert nacl_public.SealedBox(sk).decrypt(base64.b64decode(body["sealed_content"])) == png
