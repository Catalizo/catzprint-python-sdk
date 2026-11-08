"""Local printing for the synchronous client: routing, tokens, stations (see ``_local_core``)."""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import httpx

from . import _mdns
from ._base import USER_AGENT, api_error, build_job, decode_body, new_idempotency_key, seg
from ._errors import (
    APIConnectionError,
    AuthenticationError,
    CatzPrintError,
    NotFoundError,
    OutcomeUnknownError,
    ServerError,
    StationUnreachableError,
    ValidationError,
)
from ._local_core import (
    MODE_CLOUD,
    MODE_CLOUD_FIRST,
    MODE_LOCAL,
    MODE_LOCAL_FIRST,
    REMINT_INTERVAL,
    JobParams,
    LocalConfig,
    LocalState,
    Target,
    detect_kind,
    is_station_retryable,
    local_capable,
    local_job_to_job,
    mdns_targets,
    no_token_error,
    normalize_fingerprint,
    normalize_station_url,
    parse_ip,
    resolve_config,
    token_live,
    unsupported_locally,
    url_host,
    usable,
    wait_query,
)
from ._local_models import LocalEndpoint, LocalInfo, LocalJob, LocalPairing, LocalStation, LocalStatus, LocalToken
from ._local_transport import ResponseTooLarge, StationTransport, is_delivered, mark_delivered
from ._models import Job
from ._tokenstore import StoredToken

if TYPE_CHECKING:
    from ._client import CatzPrint
    from ._mdns import MDNSStation

__all__ = ["Local", "LocalEndpoints", "LocalManager", "LocalTokens"]

# (unreachable, error, data): unreachable means "this station can't be used now, try the next one".
SendResult = Tuple[bool, Optional[CatzPrintError], Any]


def new_local_key() -> str:
    """The idempotency key the SDK uses in the local modes when the caller sets none."""
    return "sdk-" + str(uuid.uuid4())


def mint_error_is_unreachable(err: BaseException) -> bool:
    """A token that can't be minted because the cloud can't be reached makes local printing
    impossible ("unreachable"); a 4xx from the cloud (e.g. a missing scope) is raised as is."""
    return isinstance(err, (APIConnectionError, ServerError))


def invalid(field: str, message: str) -> ValidationError:
    return ValidationError(message, code="invalid", fields={field: message})


def interpret(status: int, headers: Mapping[str, str], raw: bytes) -> SendResult:
    """Turn a station answer into a SendResult (shared by the sync and async managers)."""
    try:
        return _interpret(status, headers, raw)
    except CatzPrintError as e:  # e.g. JSON nested too deeply
        e.status = status
        return False, mark_delivered(e) if status < 300 else e, None


def _interpret(status: int, headers: Mapping[str, str], raw: bytes) -> SendResult:
    if status >= 300:
        err = api_error(status, raw, headers)
        if status < 400:
            mark_delivered(err)  # not a station: something on the network redirected a delivered request
        return is_station_retryable(status, err.code), err, None
    if not raw.strip():
        return False, None, {}
    data = decode_body(raw, headers.get("content-type", ""))
    if not isinstance(data, dict):  # a 2xx we can't read: for a job POST, maybe printed (C5)
        return False, invalid_job_answer(status), None
    return False, None, data


def too_large(e: ResponseTooLarge) -> SendResult:
    err = CatzPrintError("The station's answer is too large.", status=e.status, code="response_too_large")
    return False, mark_delivered(err), None


#: DELIVERED_NO_ANSWER: how long / how often the SDK asks the same station for the job by its key.
LOOKUP_BUDGET = 5.0
LOOKUP_TRIES = 3
LOOKUP_PAUSE = 0.25


def lookup_result(status: int, headers: Mapping[str, str], raw: bytes) -> Tuple[str, Any]:
    """Interpret ``GET /local/v1/jobs?idempotency_key=K``:

    * 200 with a job → ("found", data)
    * 404 ``not_found`` → ("missing", None): the station never recorded it
    * 409 ``duplicate`` → ("answer", error): the key belongs to a job this caller can't see
    * anything else → ("retry", error): keep probing, then OutcomeUnknown
    """
    _, err, data = interpret(status, headers, raw)
    if err is None:
        if valid_local_job(data):
            return "found", data
        return "retry", invalid_job_answer(status)
    if status == 404 and err.code == "not_found":
        return "missing", None
    if status == 409 and err.code == "duplicate":
        return "answer", err
    return "retry", err


def valid_local_job(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("id"), str) and bool(data["id"])


def invalid_job_answer(status: int) -> CatzPrintError:
    """C5: a 2xx to a job request with an unreadable body: DELIVERED_NO_ANSWER."""
    return mark_delivered(
        CatzPrintError(
            "The station answered with a body the SDK couldn't read.", status=status, code="invalid_response"
        )
    )


def being_created(err: CatzPrintError) -> bool:
    """A cloud 409 ``duplicate`` (e.g. "A job with this idempotency key is being created."): re-sent after
    1 s up to 3 times; still 409 after that is the cloud's answer. Any 409 ``duplicate`` counts (like the
    other SDKs), not only the "being created" wording."""
    return err.status == 409 and err.code == "duplicate"


def in_progress_answer(err: CatzPrintError) -> bool:
    """A station's 409 ``in_progress``: the POST with this key is still being processed."""
    return err.status == 409 and err.code == "in_progress"


#: R4: how often / how long apart a "being created" 409 is re-sent.
DUPLICATE_RESENDS = 3
DUPLICATE_WAIT = 1.0


def sealed_rejected(err: CatzPrintError) -> bool:
    """A 400/422 saying the API couldn't use ``sealed_content`` (usually: the server key was rotated)."""
    return isinstance(err, ValidationError) and "sealed_content" in err.fields


class CloudRetry:
    """Decides what to do after a failed cloud create (shared by the sync and async managers).

    Returns ``raise`` (an answer: raise it), ``unavailable`` (first attempt NOT_DELIVERED), ``retry``
    (ambiguous: one immediate retry with the same key), ``wait`` (409 being created: re-send the identical
    request after 1 s, up to 3 times; still 409 after that → ``raise``, the cloud's definite answer),
    ``reseal`` (a sealed job's ``sealed_content`` 400/422: drop the server key, fetch it again, seal again
    and re-send once with the same key) or ``unknown`` (a re-send not delivered or still ambiguous:
    OutcomeUnknownError).
    """

    def __init__(self, sealed: bool = False) -> None:
        self.attempts = 0
        self.ambiguous_retried = False
        self.resends = 0
        self.sealed = sealed
        self.resealed = False
        #: An attempt may have reached the cloud (set by the caller for retries inside the HTTP client).
        self.ambiguous = False

    def after(self, err: CatzPrintError, *, fallback_allowed: bool = True) -> str:
        self.attempts += 1
        first = self.attempts == 1
        if self.sealed and sealed_rejected(err):
            if self.resends:
                return "unknown"  # a job with this key exists or is being created
            if not self.resealed:
                self.resealed = True
                return "reseal"
            return "unknown" if self.ambiguous or self.ambiguous_retried else "raise"
        if self.resealed and (self.ambiguous or self.ambiguous_retried) and not being_created(err):
            return "unknown"  # after an ambiguous attempt, only the job (or a 409) settles it
        if being_created(err):
            if self.resends >= DUPLICATE_RESENDS:
                return "raise"  # still 409 after 3 re-sends: a definite answer from the cloud
            self.resends += 1
            return "wait"
        kind = cloud_outcome(err)
        if kind == "answer":
            return "raise"
        if first:
            if kind == "not_delivered":
                return "unavailable" if fallback_allowed else "raise"
            self.ambiguous_retried = True
            return "retry"
        # Not delivered or still ambiguous after an ambiguous attempt / a "being created" answer.
        return "unknown"


class CloudUnavailable(Exception):
    """Internal: the cloud attempt was NOT_DELIVERED (or 502/503/504); the job surely isn't there."""

    def __init__(self, err: CatzPrintError) -> None:
        super().__init__(str(err))
        self.err = err


NOT_DELIVERED_HTTPX = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
    httpx.ProxyError,
)


def cloud_outcome(err: CatzPrintError) -> str:
    """Classify a failed cloud create: ``not_delivered`` (connect/TLS/write errors, 502/503/504),
    ``ambiguous`` (written, then timeout/reset/unreadable/too large/redirect, or another 5xx such as
    500), or ``answer`` (the API answered: 4xx…)."""
    if getattr(err, "not_sent", False):  # failed before the job POST (org lookup, server key)
        return "not_delivered"
    if is_delivered(err):  # e.g. a 2xx with an unreadable body
        return "ambiguous"
    if isinstance(err, APIConnectionError):
        cause = err.__cause__
        return "not_delivered" if isinstance(cause, NOT_DELIVERED_HTTPX) else "ambiguous"
    if err.code in ("response_too_large", "unexpected_redirect"):
        return "ambiguous"
    if err.status in (502, 503, 504):
        return "not_delivered"
    if err.status is not None and err.status >= 500:
        return "ambiguous"
    return "answer"


def check_pair_code(code: str) -> None:
    if len(code) != 8 or any(c not in "0123456789" for c in code):
        raise invalid("code", "The pairing code is the 8 digits shown in the station app.")


def pair_target_with_fingerprint(u: str, fingerprint: str) -> Target:
    fp = normalize_fingerprint(fingerprint)
    if not fp:
        raise invalid("fingerprint", "The fingerprint is the 64 hex characters (SHA-256) shown in the station app.")
    host, _ = url_host(u)
    if parse_ip(host) is None:
        raise invalid(
            "url", "Pair with the station's IP address URL when you pass the fingerprint from the station app."
        )
    return Target(url=u, fingerprint=fp, tls_mode="self_signed", source="config")


def pair_needs_trust(t: Target) -> None:
    if (t.needs_pin() and not t.fingerprint) or (not t.needs_pin() and not t.station_id):
        raise invalid(
            "fingerprint",
            "Pairing needs the certificate fingerprint shown in the station app (fingerprint=...), "
            "or a station on the cloud endpoint list.",
        )


def prepare_params(state: LocalState, params: JobParams) -> JobParams:
    """Non-cloud modes: the same idempotency key goes to the station and the cloud, so a fallback can
    never print twice; the configured location is the default target."""
    p = dict(params)
    if not p.get("idempotency_key"):
        p["idempotency_key"] = new_local_key()
    if not p.get("printer_id") and not p.get("location_id"):
        p["location_id"] = state.cfg.location_id
    if p.get("content") is not None and not p.get("kind"):
        p["kind"] = detect_kind(bytes(p["content"]), p.get("file_name"))
    return p


def station_body(params: JobParams) -> Dict[str, Any]:
    body = build_job(**params).finish()
    body.pop("file_name", None)
    return body


def with_remint_failure(err: CatzPrintError, merr: BaseException) -> CatzPrintError:
    err.message = f"{err.message} (and a fresh token couldn't be minted: {merr})"
    err.__cause__ = merr
    return err


class LocalManager:
    """The sync client's local printing machinery (one per client)."""

    def __init__(self, client: CatzPrint, cfg: Optional[LocalConfig]) -> None:
        self._c = client
        rc = resolve_config(cfg)
        self.state = LocalState(rc, client._config.api_key, lambda: client._config.org_id)
        self.transport = StationTransport(
            connect_timeout=rc.connect_timeout,
            request_timeout=rc.request_timeout,
            user_agent=USER_AGENT,
            ca_file=rc.ca_file,
            ca_data=rc.ca_data,
        )
        self._mint_lock = threading.Lock()
        self._remint_lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @property
    def cfg(self) -> LocalConfig:
        return self.state.cfg

    # -- cloud calls (no retries, cloud timeout)
    def _cloud_mint(self) -> LocalToken:
        return self._c.local_tokens._create(self.state.mint_body(), max_retries=0, timeout=self.cfg.cloud_timeout)

    def _cloud_endpoints(self) -> List[LocalEndpoint]:
        return self._c.local_endpoints._list(self.cfg.location_id, max_retries=0, timeout=self.cfg.cloud_timeout)

    # -- tokens
    def mint(self, force: bool) -> StoredToken:
        """A fresh local token from the cloud, stored with the endpoint list that comes with it.
        Unless ``force``, a concurrent mint that already produced a good token is reused."""
        with self._mint_lock:
            if not force:
                st = self.state.snapshot()
                if st is not None and usable(st, self.state.now()):
                    return st
            lt = self._cloud_mint()
            st = self.state.snapshot() or StoredToken()
            st.token, st.token_id, st.expires_at = lt.token, lt.token_id, lt.expires_at
            if lt.stations is not None:
                st.stations, st.stations_updated_at = lt.stations, self.state.now()
            self.state.save(st)
            return st

    def token(self) -> str:
        """The token for station requests, minting one when none is usable."""
        if self.cfg.token:
            return self.cfg.token
        self.state.ensure_loaded()
        st = self.state.snapshot()
        if st is not None and usable(st, self.state.now()):
            return st.token
        return self.mint(False).token

    def refresh(self, force: bool) -> None:
        """Renew the token when it has less than 7 days left and the endpoint list when it is older
        than an hour (or always, with ``force``)."""
        self.state.ensure_loaded()
        tok, eps = self.state.needs_refresh()
        if tok:
            prev = self.state.snapshot()
            before = prev.stations_updated_at if prev is not None else None
            st = self.mint(True)
            if st.stations_updated_at != before:
                return  # the mint brought a fresh endpoint list
        if not eps and not force:
            return
        lst = self._cloud_endpoints()
        st2 = self.state.snapshot() or StoredToken()
        st2.stations, st2.stations_updated_at = lst, self.state.now()
        self.state.save(st2)

    def maybe_refresh(self) -> None:
        """On use (no background thread): renew a token with < 7 days left / an endpoint list older
        than an hour. Failures are retried 5 minutes later and never raised."""
        if not self.state.refresh_due():
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            self.refresh(False)
        except Exception:
            self.state.refresh_failed()
        finally:
            self._refresh_lock.release()

    def remint(self, rejected: CatzPrintError) -> Tuple[Optional[str], Optional[BaseException]]:
        """Answer a 401 with a fresh token: only for ``token_expired`` (never ``device_revoked``), only
        for minted tokens, at most once per 10 minutes per store key (persisted in the store)."""
        if not self.state.remint_allowed(rejected.code):
            return None, None
        with self._remint_lock:
            st = self.state.snapshot() or StoredToken()
            now = self.state.now()
            if st.last_remint_at is not None and now - st.last_remint_at < REMINT_INTERVAL:
                return None, None
            st.last_remint_at = now
            self.state.save(st)
            try:
                return self.mint(True).token, None
            except CatzPrintError as e:
                return None, e

    # -- stations
    def browse(self, fresh: bool) -> Tuple[List[MDNSStation], Optional[BaseException]]:
        """An mDNS browse (reused for a minute unless ``fresh``)."""
        cached = self.state.mdns_cached(fresh)
        if cached is not None:
            return cached, None
        err: Optional[BaseException] = None
        try:
            found = _mdns.browse(self.cfg.discover_timeout, self.state.mdns_addr)
        except OSError as e:
            found, err = [], e
        self.state.set_mdns(found)
        return found, err

    def targets(self, preferred: Optional[str] = None) -> Iterator[Target]:
        """Candidates in order: configured URLs, cloud endpoint URLs, then (with discover, browsed only
        when those all failed) stations found over mDNS. ``preferred`` goes first."""
        tried = set()
        for t in self.state.ordered_targets(preferred):
            tried.add(t.url)
            yield t
        if not self.cfg.discover:
            return
        found, _ = self.browse(False)
        for d in self.state.accepted_mdns(found, self.state.snapshot()):
            for t in mdns_targets(d):
                if t.url in tried:
                    continue
                tried.add(t.url)
                yield t

    def send(
        self,
        t: Target,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        token: str = "",
        body: Any = None,
        extra: float = 0.0,
    ) -> SendResult:
        """One station request. ``unreachable`` means connect/TLS/pin/timeout errors, 502/504, 503
        ``local_disabled`` (or without a code): this station can't be used now."""
        try:
            status, headers, raw = self.transport.request(
                t, method, path, query=query, token=token, body=body, extra=extra
            )
        except ResponseTooLarge as e:
            return too_large(e)
        except CatzPrintError as e:  # connection, TLS, timeout, pin mismatch, no pin
            return True, e, None
        return interpret(status, headers, raw)

    # -- jobs
    def create(self, params: JobParams) -> Job:
        """Route a job according to the mode."""
        mode = self.cfg.mode
        if mode == MODE_CLOUD:
            return self.cloud_mode(params)
        p = prepare_params(self.state, params)
        why = local_capable(p)
        if mode == MODE_LOCAL:
            if why:
                raise unsupported_locally(why)
            return self.create_local(p)
        if mode == MODE_LOCAL_FIRST:
            if why:  # can't print locally: the same cloud create as mode cloud (with the 409 loop)
                return self.cloud_mode(p)
            try:
                return self.create_local(p)
            except StationUnreachableError as sue:
                try:
                    return self.cloud_leg(p, None)
                except CloudUnavailable as cu:  # the cloud surely doesn't have it either
                    sue.cloud_error = cu.err
                    raise sue from cu.err
                # A cloud answer (4xx…) or OutcomeUnknownError is raised as is.
        assert mode == MODE_CLOUD_FIRST
        if why:
            return self.cloud_mode(p)
        try:
            return self.cloud_leg(p, self.cfg.cloud_timeout)
        except CloudUnavailable as cu:
            err = cu.err
        try:
            return self.create_local(p)
        except StationUnreachableError as sue:
            sue.cloud_error = err
            raise sue from err

    def cloud_leg(self, p: JobParams, timeout: Optional[float]) -> Job:
        """The cloud create of the local modes. Raises CloudUnavailable when the job surely didn't reach
        the cloud, OutcomeUnknownError when it may have, and the API's answer otherwise.

        Ambiguous (written, no answer; a 2xx without a job; 5xx other than 502/503/504): ONE retry with
        the same key. A 409 "being created": re-sent after 1 s, up to 3 times. Not delivered or still
        ambiguous after that: OutcomeUnknownError (never the stations: the cloud may have queued it)."""
        return self._cloud_loop(p, max_retries=0, timeout=timeout, fallback_allowed=True)

    def cloud_mode(self, params: JobParams) -> Job:
        """Mode cloud: the usual create (with retries); a 409 "being created" is re-sent like above."""
        p = dict(params)
        if not p.get("idempotency_key"):
            p["idempotency_key"] = new_idempotency_key()
        return self._cloud_loop(p, max_retries=None, timeout=None, fallback_allowed=False, cloud_mode=True)

    def _cloud_loop(
        self,
        p: JobParams,
        *,
        max_retries: Optional[int],
        timeout: Optional[float],
        fallback_allowed: bool,
        cloud_mode: bool = False,
    ) -> Job:
        jobs = self._c.jobs
        plan = CloudRetry(sealed=bool(p.get("sealed")))
        prepared: Dict[str, Any] = {}  # the same body (and seal) for every re-send

        def ambiguous() -> None:  # a retry inside the HTTP client after an attempt that may have arrived
            plan.ambiguous = True

        while True:
            try:
                return jobs._create_cloud(
                    p, max_retries=max_retries, timeout=timeout, prepared=prepared, on_ambiguous=ambiguous
                )
            except CatzPrintError as err:
                action = plan.after(err, fallback_allowed=fallback_allowed)
                if cloud_mode and action in ("retry", "unavailable") and plan.resends == 0:
                    action = "raise"  # mode cloud keeps its usual error handling
                if action == "raise":
                    raise
                if action == "unavailable":
                    raise CloudUnavailable(err) from err
                if action == "unknown":
                    raise OutcomeUnknownError(self._c.base_url, str(p["idempotency_key"]), err) from err
                if action == "wait":
                    self._c._sleep(DUPLICATE_WAIT)
                if action == "reseal":
                    # The API couldn't decrypt the sealed content (key rotated): fetch the key again and
                    # seal again, once; the same idempotency key goes out (S1).
                    self._c._drop_server_key()
                    prepared.clear()
                    try:
                        jobs._prepare_into(p, prepared)
                    except CatzPrintError as kerr:
                        if plan.ambiguous or plan.ambiguous_retried:
                            raise OutcomeUnknownError(self._c.base_url, str(p["idempotency_key"]), kerr) from kerr
                        raise err from None  # nothing more was sent: the API's answer stands

    def _token_or_unreachable(self) -> str:
        try:
            return self.token()
        except CatzPrintError as e:
            if not mint_error_is_unreachable(e):
                raise
            sue = StationUnreachableError()
            sue.add("", no_token_error(e))
            raise sue from e

    def create_local(self, params: JobParams) -> Job:
        """Send a job to the first station that takes it."""
        body = station_body(params)
        tok = self._token_or_unreachable()
        try:
            sue = StationUnreachableError()
            q = wait_query(self.cfg.wait)
            reminted = False
            for t in self.targets():
                unreachable, err, data = self.send(
                    t, "POST", "/local/v1/jobs", query=q, token=tok, body=body, extra=self.cfg.wait
                )
                if err is not None and not unreachable and isinstance(err, AuthenticationError) and not reminted:
                    # 401 token_expired: mint a fresh token (rate-limited) and retry this station once.
                    reminted = True
                    nt, merr = self.remint(err)
                    if nt:
                        tok = nt
                        unreachable, err, data = self.send(
                            t, "POST", "/local/v1/jobs", query=q, token=tok, body=body, extra=self.cfg.wait
                        )
                    elif merr is not None:
                        err = with_remint_failure(err, merr)
                if err is None and not valid_local_job(data):
                    err = invalid_job_answer(200)  # C5: a 2xx we can't read: maybe printed
                in_progress = err is not None and in_progress_answer(err)
                if err is not None and (in_progress or is_delivered(err)):
                    # DELIVERED_NO_ANSWER (or 409 in_progress): the station may print it. Ask it, never
                    # another one; an in-progress job must be found (a 404 means "not yet").
                    key = str(body["idempotency_key"])
                    outcome, found = self.lookup(t, tok, key, missing_ok=not in_progress)
                    if outcome == "missing":  # the station never recorded it: as if not delivered
                        sue.add(t.url, err)
                        continue
                    if outcome == "answer":  # 409 duplicate: the key belongs to a job we can't see
                        raise found
                    if outcome != "found":
                        raise OutcomeUnknownError(t.url, key, found if found is not None else err)
                    err, data = None, found
                if err is None:
                    lj = LocalJob.from_dict(data)
                    lj.station_url = t.url
                    self.state.remember_job(lj.id, t.url)
                    return local_job_to_job(lj, t, params, token_live(tok))
                if unreachable:
                    sue.add(t.url, err)
                    continue
                if isinstance(err, AuthenticationError) and self.cfg.mode != MODE_LOCAL:
                    # Still refused: in the fallback modes this means "use the cloud".
                    sue.add(t.url, err)
                    raise sue
                raise err
            raise sue
        finally:
            self.maybe_refresh()

    def lookup(self, t: Target, tok: str, key: str, missing_ok: bool = True) -> Tuple[str, Any]:
        """Ask the same station for the job by its key, up to 3 times within 5 s (fresh connections,
        same pinning): ("found", job data) / ("missing", None) / ("unknown", last error)."""
        deadline = time.monotonic() + LOOKUP_BUDGET
        last: Any = None
        for i in range(LOOKUP_TRIES):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if i:
                time.sleep(min(LOOKUP_PAUSE, left))
                left = deadline - time.monotonic()
                if left <= 0:
                    break
            try:
                status, headers, raw = self.transport.request(
                    t,
                    "GET",
                    "/local/v1/jobs",
                    query={"idempotency_key": key},
                    token=tok,
                    timeout=min(self.cfg.request_timeout, left),
                )
            except ResponseTooLarge as e:
                last = too_large(e)[1]
                continue
            except CatzPrintError as e:
                last = e
                continue
            outcome, val = lookup_result(status, headers, raw)
            if outcome == "retry" and isinstance(val, CatzPrintError) and in_progress_answer(val):
                missing_ok = False  # the station said it is processing this key: a later 404 isn't "never"
            if outcome == "missing" and not missing_ok:  # the POST is still in progress: not yet
                last = val
                continue
            if outcome != "retry":
                return outcome, val
            last = val
        return "unknown", last

    def authed(self, preferred: Optional[str], fn: Callable[[Target, str], SendResult]) -> Any:
        """Run ``fn`` against each candidate station with a token until one answers."""
        tok = self._token_or_unreachable()
        try:
            sue = StationUnreachableError()
            reminted = False
            for t in self.targets(preferred):
                unreachable, err, res = fn(t, tok)
                if err is not None and not unreachable and isinstance(err, AuthenticationError) and not reminted:
                    reminted = True
                    nt, _ = self.remint(err)
                    if nt:
                        tok = nt
                        unreachable, err, res = fn(t, tok)
                if err is None:
                    return res
                if unreachable:
                    sue.add(t.url, err)
                    continue
                raise err
            raise sue
        finally:
            self.maybe_refresh()

    def target(self, station_url: str) -> Target:
        """Trust for an explicit station URL, browsing mDNS once if an IP URL has no known fingerprint."""
        u = normalize_station_url(station_url)
        self.state.ensure_loaded()
        st = self.state.snapshot()
        with self.state.lock:
            mdns = list(self.state.mdns)
        t = self.state.target_for(u, "config", st, mdns)
        if t.needs_pin() and not t.fingerprint and self.cfg.discover:
            found, _ = self.browse(True)
            if found:
                t = self.state.target_for(u, "config", st, found)
        return t

    def pair_target(self, station_url: str, fingerprint: Optional[str]) -> Target:
        """Trust for pairing, without mDNS."""
        u = normalize_station_url(station_url)
        if fingerprint:
            return pair_target_with_fingerprint(u, fingerprint)
        self.state.ensure_loaded()
        t = self.state.target_for(u, "config", self.state.snapshot(), [])
        pair_needs_trust(t)
        return t


class Local:
    """``client.local``: talk to CatzPrint stations on the local network."""

    def __init__(self, manager: LocalManager) -> None:
        self._m = manager

    @property
    def mode(self) -> str:
        """The configured local mode."""
        return self._m.cfg.mode

    @property
    def store_error(self) -> Optional[BaseException]:
        """The last token store error (``None`` if none). Store failures never stop printing; they only
        mean the token or endpoint list won't survive a restart."""
        with self._m.state.lock:
            return self._m.state.store_error

    def status(self) -> LocalStatus:
        """The status of the first station that answers (printers, paper, unsynced jobs, offline allowance)."""

        def fn(t: Target, tok: str) -> SendResult:
            unreachable, err, data = self._m.send(t, "GET", "/local/v1/status", token=tok)
            if err is not None:
                return unreachable, err, None
            st = LocalStatus.from_dict(data)
            st.station_url = t.url
            return False, None, st

        res: LocalStatus = self._m.authed(None, fn)
        return res

    def get_job(self, job_id: str) -> Job:
        """A local job (``lj_…``) from the station that has it. ``jobs.get``/``wait_for`` call this."""
        m = self._m
        pref = m.state.job_url(job_id)
        not_found: List[CatzPrintError] = []

        def fn(t: Target, tok: str) -> SendResult:
            unreachable, err, data = m.send(t, "GET", "/local/v1/jobs/" + seg(job_id), token=tok)
            if err is None:
                lj = LocalJob.from_dict(data)
                lj.station_url = t.url
                m.state.remember_job(job_id, t.url)
                return False, None, local_job_to_job(lj, t, {}, token_live(tok))
            if isinstance(err, NotFoundError) and not pref:
                not_found.append(err)  # another station may have it
                return True, err, None
            return unreachable, err, None

        try:
            res: Job = m.authed(pref, fn)
        except StationUnreachableError:
            if not_found:
                raise not_found[0] from None
            raise
        return res

    def info(self, station_url: str) -> LocalInfo:
        """A station's public information (``GET /local/v1/info``, no token needed).

        The URL must be trusted like any station URL: a public hostname on the cloud list, or an IP
        with a fingerprint from the endpoint list, mDNS for a listed station, or ``fingerprints``.
        """
        t = self._m.target(station_url)
        unreachable, err, data = self._m.send(t, "GET", "/local/v1/info")
        if err is not None:
            if unreachable:
                sue = StationUnreachableError()
                sue.add(t.url, err)
                raise sue
            raise err
        return LocalInfo.from_dict(data)

    def pair(
        self, station_url: str, code: str, device_name: Optional[str] = None, *, fingerprint: Optional[str] = None
    ) -> LocalPairing:
        """Pair this device with one station using the 8-digit code shown in the station app.

        The returned device token (``czd_…``) works only on that station and without any cloud
        account; pass it as ``LocalConfig(token=...)``. The station must be authenticated before the
        code is sent, or a device on the LAN could relay it: an IP URL needs the ``fingerprint`` shown
        in the station app (or ``LocalConfig.fingerprints``, or a cloud endpoint list containing that
        address); a hostname URL must be on the cloud endpoint list. mDNS is never enough.
        """
        check_pair_code(code)
        name = (device_name or self._m.cfg.device_name or "")[:80]
        t = self._m.pair_target(station_url, fingerprint)
        unreachable, err, data = self._m.send(t, "POST", "/local/v1/pair", body={"code": code, "device_name": name})
        if err is not None:
            if unreachable:
                sue = StationUnreachableError()
                sue.add(t.url, err)
                raise sue
            raise err
        p = LocalPairing.from_dict(data)
        if not p.token:
            raise CatzPrintError("The station answered without a token.", status=201, code="invalid_response")
        return p

    def discover(self) -> List[LocalStation]:
        """Stations from the cloud endpoint list (refreshed now when the cloud answers, else the cached
        copy) and from an mDNS browse, merged by station ID. Doesn't need ``discover=True``."""
        m = self._m
        m.state.ensure_loaded()
        cloud_err: Optional[CatzPrintError] = None
        try:
            m.refresh(True)
        except CatzPrintError as e:
            cloud_err = e
        st = m.state.snapshot()
        found, mdns_err = m.browse(True)
        out = m.state.discovered_stations(st, found)
        if not out:
            if cloud_err is not None:
                raise cloud_err
            if mdns_err is not None:
                raise APIConnectionError(f"mDNS browse failed: {mdns_err}") from mdns_err
        return out

    def refresh(self) -> None:
        """Renew the local token (when it has less than 7 days left) and re-read the endpoint list now.

        The SDK also does this by itself when it uses the token (no background thread); call it at
        start-up or after network changes.
        """
        self._m.refresh(True)

    def token(self) -> str:
        """The token the SDK uses for stations (minting one when needed), e.g. for a browser POS."""
        return self._m.token()


class LocalTokens:
    """``client.local_tokens``: mint and revoke local tokens for stations."""

    def __init__(self, client: CatzPrint) -> None:
        self._c = client

    def create(
        self, *, device_name: str, location_ids: Optional[Sequence[str]] = None, ttl_days: Optional[int] = None
    ) -> LocalToken:
        """Mint a local token (``POST /v1/orgs/{org}/local-tokens``; needs ``jobs:write``)."""
        body: Dict[str, Any] = {"device_name": device_name}
        if location_ids:
            body["location_ids"] = list(location_ids)
        if ttl_days:
            body["ttl_days"] = ttl_days
        return self._create(body)

    def _create(
        self, body: Dict[str, Any], *, max_retries: Optional[int] = None, timeout: Optional[float] = None
    ) -> LocalToken:
        check_token_body(body)
        res = self._c._request(
            "POST", self._c._org_path("/local-tokens"), json=body, max_retries=max_retries, timeout=timeout
        )
        lt = LocalToken.from_dict(res if isinstance(res, dict) else {})
        if not lt.token:
            raise CatzPrintError("The API answered without a token.", status=201, code="invalid_response")
        return lt

    def revoke(self, token_id: str) -> None:
        """Revoke a local token by its token ID (the jti). Stations refuse it once they synced."""
        self._c._request("DELETE", self._c._org_path(f"/local-tokens/{seg(token_id)}"))


class LocalEndpoints:
    """``client.local_endpoints``: the local-network addresses of the organization's stations."""

    def __init__(self, client: CatzPrint) -> None:
        self._c = client

    def list(self, location_id: Optional[str] = None) -> List[LocalEndpoint]:
        """The stations' local endpoints, optionally only those at one location."""
        return self._list(location_id)

    def _list(
        self, location_id: Optional[str], *, max_retries: Optional[int] = None, timeout: Optional[float] = None
    ) -> List[LocalEndpoint]:
        params = {"location_id": location_id} if location_id else None
        res = self._c._request(
            "GET", self._c._org_path("/local-endpoints"), params=params, max_retries=max_retries, timeout=timeout
        )
        return endpoints_list(res)


def check_token_body(body: Mapping[str, Any]) -> None:
    if not body.get("device_name"):
        raise invalid("device_name", "Set device_name.")
    ttl = body.get("ttl_days")
    if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1 or ttl > 30):
        raise invalid("ttl_days", "ttl_days must be between 1 and 30.")


def endpoints_list(res: Any) -> List[LocalEndpoint]:
    st = res.get("stations") if isinstance(res, dict) else None
    return [LocalEndpoint.from_dict(x) for x in st if isinstance(x, dict)] if isinstance(st, list) else []
