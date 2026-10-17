"""Client-side sealing (NaCl sealed box, X25519 + XSalsa20-Poly1305).

Needs the optional dependency: ``pip install "catzprint[sealed]"``.
"""

from __future__ import annotations

import base64
import binascii

__all__ = ["SEALED_ALGORITHM", "seal", "sealing_available"]

SEALED_ALGORITHM = "x25519-xsalsa20-poly1305-sealed-box"


def sealing_available() -> bool:
    """True when PyNaCl is installed."""
    try:
        import nacl.public  # noqa: F401
    except ImportError:
        return False
    return True


def seal(public_key_b64: str, data: bytes) -> bytes:
    """Encrypt ``data`` to a base64 X25519 public key with a NaCl sealed box."""
    try:
        from nacl.public import PublicKey, SealedBox
    except ImportError as e:  # pragma: no cover - depends on environment
        raise ImportError('Sealed printing needs PyNaCl. Install it with: pip install "catzprint[sealed]"') from e
    try:
        key = base64.b64decode(public_key_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("The server public key isn't valid base64.") from e
    if len(key) != 32 or key == bytes(32):
        raise ValueError("The server public key must be 32 bytes.")
    sealed: bytes = SealedBox(PublicKey(key)).encrypt(bytes(data))
    return sealed
