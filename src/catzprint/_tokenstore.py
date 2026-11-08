"""Token stores: where the SDK keeps a device's local token and the station endpoint list."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Union, runtime_checkable

from ._errors import CatzPrintError
from ._local_models import LocalEndpoint, endpoints_from
from ._models import parse_time

__all__ = ["FileTokenStore", "MemoryTokenStore", "StoredToken", "TokenStore"]


def _fmt(t: Optional[datetime]) -> Optional[str]:
    if t is None:
        return None
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(v: Any) -> Optional[datetime]:
    t = parse_time(v)
    # Go writes its zero time ("0001-01-01T00:00:00Z") for unset fields: same as None here.
    return None if t is None or t.year <= 1 else t


_PLACEHOLDER = re.compile(r"(?:czl1\.|czd_)?(?:…|\*{3,}|\[redacted\]|redacted|hidden)", re.IGNORECASE)


def is_redaction_placeholder(token: str) -> bool:
    """True for a token that is really a redacted rendering, e.g. a store written from logged/printed
    state instead of the real value: ``…``, a run of 3+ ``*``, ``[redacted]``, ``redacted`` or ``hidden``
    (any case), optionally after ``czl1.`` or ``czd_``. Every CatzPrint SDK recognises the same set. Such a
    token is "no token"."""
    return bool(token) and _PLACEHOLDER.fullmatch(token) is not None


@dataclass
class StoredToken:
    """What the SDK keeps in a :class:`TokenStore` for one device.

    Its local token and the last station endpoint list from the cloud, so a hub that restarts while
    the internet is down can still find and trust its stations.
    """

    #: The local token (``czl1…``); empty when only endpoints are stored (static ``token``).
    token: str = ""
    token_id: str = ""
    expires_at: Optional[datetime] = None
    #: The last endpoint list from the cloud.
    stations: List[LocalEndpoint] = field(default_factory=list)
    stations_updated_at: Optional[datetime] = None
    #: When a station's ``token_expired`` last caused a re-mint (at most once per 10 minutes).
    last_remint_at: Optional[datetime] = None

    def __repr__(self) -> str:  # never show the token itself
        return (
            f"StoredToken(token_id={self.token_id!r}, expires_at={self.expires_at!r}, "
            f"stations={len(self.stations)}, stations_updated_at={self.stations_updated_at!r})"
        )

    def clone(self) -> StoredToken:
        return copy.deepcopy(self)

    def to_dict(self) -> Dict[str, Any]:
        """JSON form (the same keys as the Go and Node SDKs)."""
        d: Dict[str, Any] = {}
        if self.token:
            d["token"] = self.token
        if self.token_id:
            d["token_id"] = self.token_id
        if self.expires_at is not None:
            d["expires_at"] = _fmt(self.expires_at)
        if self.stations:
            d["stations"] = [s.to_dict() for s in self.stations]
        if self.stations_updated_at is not None:
            d["stations_updated_at"] = _fmt(self.stations_updated_at)
        if self.last_remint_at is not None:
            d["last_remint_at"] = _fmt(self.last_remint_at)
        return d

    @classmethod
    def from_dict(cls, d: Any) -> Optional[StoredToken]:
        if not isinstance(d, dict):
            return None
        tok = d.get("token")
        tid = d.get("token_id")
        return cls(
            token=tok if isinstance(tok, str) else "",
            token_id=tid if isinstance(tid, str) else "",
            expires_at=_time(d.get("expires_at")),
            stations=endpoints_from(d.get("stations")) or [],
            stations_updated_at=_time(d.get("stations_updated_at")),
            last_remint_at=_time(d.get("last_remint_at")),
        )


@runtime_checkable
class TokenStore(Protocol):
    """Persists local tokens between runs. Implementations must be thread-safe.

    ``load`` returns ``None`` when nothing is stored under ``key``. Keys are opaque strings chosen by
    the SDK (they never contain the API key itself). ``save(key, None)`` deletes the entry.
    """

    def load(self, key: str) -> Optional[StoredToken]: ...

    def save(self, key: str, token: Optional[StoredToken]) -> None: ...


class MemoryTokenStore:
    """Keeps tokens in memory (the default). Tokens are minted again after a restart."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._m: Dict[str, StoredToken] = {}

    def load(self, key: str) -> Optional[StoredToken]:
        with self._lock:
            t = self._m.get(key)
            return t.clone() if t is not None else None

    def save(self, key: str, token: Optional[StoredToken]) -> None:
        with self._lock:
            if token is None:
                self._m.pop(key, None)
            else:
                self._m[key] = token.clone()


class FileTokenStore:
    """Keeps tokens in a JSON file readable only by the current user.

    The file has mode 0600 (its directory is created with 0700). Every save writes a temporary file
    next to it and renames it into place, so the file is never half-written. One file can hold
    several keys. A corrupt file raises on ``load`` and is replaced on the next ``save``.
    """

    def __init__(self, path: Union[str, os.PathLike[str]]) -> None:
        self._path = os.fspath(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    def __repr__(self) -> str:
        return f"FileTokenStore({self._path!r})"

    def _check_secure(self) -> None:
        """POSIX: refuse an existing store file or directory that isn't owned by the current user or is
        group/world-writable (someone else could plant or read tokens). Skipped on Windows."""
        if os.name == "nt" or not hasattr(os, "geteuid"):
            return
        uid = os.geteuid()
        for p, what in ((os.path.dirname(os.path.abspath(self._path)), "directory"), (self._path, "file")):
            try:
                st = os.stat(p)
            except FileNotFoundError:
                continue
            except OSError as e:
                raise CatzPrintError(f"Couldn't check the token store {what} {p}: {e}", code="token_store") from e
            if st.st_uid != uid:
                raise CatzPrintError(
                    f"The token store {what} {p} isn't owned by the current user; refusing to use it.",
                    code="token_store_insecure",
                )
            if st.st_mode & 0o022:
                raise CatzPrintError(
                    f"The token store {what} {p} is writable by other users; refusing to use it "
                    f"(chmod go-w, or use a private directory).",
                    code="token_store_insecure",
                )

    def _read_all(self) -> Dict[str, Any]:
        self._check_secure()
        try:
            with open(self._path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise CatzPrintError(f"Couldn't read the token store {self._path}: {e}", code="token_store") from e
        if not data.strip():
            return {}
        try:
            m = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError) as e:
            raise CatzPrintError(f"The token store {self._path} is corrupt: {e}", code="token_store") from e
        if not isinstance(m, dict):
            raise CatzPrintError(f"The token store {self._path} is corrupt.", code="token_store")
        return m

    def load(self, key: str) -> Optional[StoredToken]:
        with self._lock:
            st = StoredToken.from_dict(self._read_all().get(key))
        if st is not None and is_redaction_placeholder(st.token):
            raise CatzPrintError(
                f"The token store {self._path} holds a redacted placeholder instead of a token.", code="token_store"
            )
        return st

    def save(self, key: str, token: Optional[StoredToken]) -> None:
        with self._lock:
            self._check_secure()  # an insecure file is never used, not even overwritten
            try:
                m = self._read_all()
            except CatzPrintError:
                m = {}  # a corrupt file is replaced rather than blocking printing
            if token is None:
                m.pop(key, None)
            else:
                m[key] = token.to_dict()
            data = json.dumps(m, indent=2, sort_keys=True).encode("utf-8")
            _write_atomic(self._path, data)


def _write_atomic(path: str, data: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    except OSError as e:
        raise CatzPrintError(f"Couldn't write the token store {path}: {e}", code="token_store") from e
    ok = False
    try:
        # mkstemp creates the file with mode 0600 already; chmod again in case of an odd umask/platform.
        with contextlib.suppress(OSError):  # e.g. some Windows file systems
            os.chmod(tmp, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        ok = True
    except OSError as e:
        raise CatzPrintError(f"Couldn't write the token store {path}: {e}", code="token_store") from e
    finally:
        if not ok:
            with contextlib.suppress(OSError):
                os.remove(tmp)
