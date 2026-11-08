"""Local printing for the asyncio client (the async twin of ``_local``)."""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import _mdns
from ._base import USER_AGENT, new_idempotency_key, seg
from ._errors import (
    APIConnectionError,
    AuthenticationError,
    CatzPrintError,
    NotFoundError,
    OutcomeUnknownError,
    StationUnreachableError,
)
from ._local import (
    DUPLICATE_WAIT,
    LOOKUP_BUDGET,
    LOOKUP_PAUSE,
    LOOKUP_TRIES,
    CloudRetry,
    CloudUnavailable,
    SendResult,
    check_pair_code,
    check_token_body,
    endpoints_list,
    in_progress_answer,
    interpret,
    invalid_job_answer,
    lookup_result,
    mint_error_is_unreachable,
    pair_needs_trust,
    pair_target_with_fingerprint,
    prepare_params,
    station_body,
    too_large,
    valid_local_job,
    with_remint_failure,
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
    local_capable,
    local_job_to_job,
    mdns_targets,
    no_token_error,
    normalize_station_url,
    resolve_config,
    token_live,
    unsupported_locally,
    usable,
    wait_query,
)
from ._local_models import LocalEndpoint, LocalInfo, LocalJob, LocalPairing, LocalStation, LocalStatus, LocalToken
from ._local_transport import ResponseTooLarge, StationTransport, is_delivered
from ._models import Job
from ._tokenstore import StoredToken

if TYPE_CHECKING:
    from ._async_client import AsyncCatzPrint
    from ._mdns import MDNSStation

__all__ = ["AsyncLocal", "AsyncLocalEndpoints", "AsyncLocalManager", "AsyncLocalTokens"]

Step = Callable[[Target], Awaitable[Tuple[bool, Any]]]


class AsyncLocalManager:
    """The async client's local printing machinery (one per client)."""

    def __init__(self, client: AsyncCatzPrint, cfg: Optional[LocalConfig]) -> None:
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
        # Created on first use, inside the running event loop.
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        lk = self._locks.get(name)
        if lk is None:
            lk = self._locks[name] = asyncio.Lock()
        return lk

    @property
    def cfg(self) -> LocalConfig:
        return self.state.cfg

    # -- cloud calls (no retries, cloud timeout)
    async def _cloud_mint(self) -> LocalToken:
        return await self._c.local_tokens._create(self.state.mint_body(), max_retries=0, timeout=self.cfg.cloud_timeout)

    async def _cloud_endpoints(self) -> List[LocalEndpoint]:
        return await self._c.local_endpoints._list(self.cfg.location_id, max_retries=0, timeout=self.cfg.cloud_timeout)

    # -- tokens
    async def mint(self, force: bool) -> StoredToken:
        async with self._lock("mint"):
            if not force:
                st = self.state.snapshot()
                if st is not None and usable(st, self.state.now()):
                    return st
            lt = await self._cloud_mint()
            st = self.state.snapshot() or StoredToken()
            st.token, st.token_id, st.expires_at = lt.token, lt.token_id, lt.expires_at
            if lt.stations is not None:
                st.stations, st.stations_updated_at = lt.stations, self.state.now()
            self.state.save(st)
            return st

    async def token(self) -> str:
        if self.cfg.token:
            return self.cfg.token
        self.state.ensure_loaded()
        st = self.state.snapshot()
        if st is not None and usable(st, self.state.now()):
            return st.token
        return (await self.mint(False)).token

    async def refresh(self, force: bool) -> None:
        self.state.ensure_loaded()
        tok, eps = self.state.needs_refresh()
        if tok:
            prev = self.state.snapshot()
            before = prev.stations_updated_at if prev is not None else None
            st = await self.mint(True)
            if st.stations_updated_at != before:
                return  # the mint brought a fresh endpoint list
        if not eps and not force:
            return
        lst = await self._cloud_endpoints()
        st2 = self.state.snapshot() or StoredToken()
        st2.stations, st2.stations_updated_at = lst, self.state.now()
        self.state.save(st2)

    async def maybe_refresh(self) -> None:
        if not self.state.refresh_due():
            return
        lk = self._lock("refresh")
        if lk.locked():
            return
        async with lk:
            try:
                await self.refresh(False)
            except Exception:
                self.state.refresh_failed()

    async def remint(self, rejected: CatzPrintError) -> Tuple[Optional[str], Optional[BaseException]]:
        if not self.state.remint_allowed(rejected.code):
            return None, None
        async with self._lock("remint"):
            st = self.state.snapshot() or StoredToken()
            now = self.state.now()
            if st.last_remint_at is not None and now - st.last_remint_at < REMINT_INTERVAL:
                return None, None
            st.last_remint_at = now
            self.state.save(st)
            try:
                return (await self.mint(True)).token, None
            except CatzPrintError as e:
                return None, e

    # -- stations
    async def browse(self, fresh: bool) -> Tuple[List[MDNSStation], Optional[BaseException]]:
        cached = self.state.mdns_cached(fresh)
        if cached is not None:
            return cached, None
        cancel = threading.Event()
        loop = asyncio.get_running_loop()
        err: Optional[BaseException] = None
        fut = loop.run_in_executor(
            None, functools.partial(_mdns.browse, self.cfg.discover_timeout, self.state.mdns_addr, cancel)
        )
        try:
            found = await fut
        except asyncio.CancelledError:
            cancel.set()
            raise
        except OSError as e:
            found, err = [], e
        self.state.set_mdns(found)
        return found, err

    async def each_target(self, preferred: Optional[str], step: Step) -> Tuple[bool, Any]:
        """Call ``step`` for every candidate in order (configured URLs, cloud endpoint URLs, then mDNS
        with discover) until it returns ``(True, value)``."""
        tried: Set[str] = set()
        for t in self.state.ordered_targets(preferred):
            tried.add(t.url)
            done, val = await step(t)
            if done:
                return True, val
        if not self.cfg.discover:
            return False, None
        found, _ = await self.browse(False)
        for d in self.state.accepted_mdns(found, self.state.snapshot()):
            for t in mdns_targets(d):
                if t.url in tried:
                    continue
                tried.add(t.url)
                done, val = await step(t)
                if done:
                    return True, val
        return False, None

    async def send(
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
        try:
            status, headers, raw = await self.transport.arequest(
                t, method, path, query=query, token=token, body=body, extra=extra
            )
        except ResponseTooLarge as e:
            return too_large(e)
        except CatzPrintError as e:  # connection, TLS, timeout, pin mismatch, no pin
            return True, e, None
        return interpret(status, headers, raw)

    # -- jobs
    async def create(self, params: JobParams) -> Job:
        mode = self.cfg.mode
        if mode == MODE_CLOUD:
            return await self.cloud_mode(params)
        p = prepare_params(self.state, params)
        why = local_capable(p)
        if mode == MODE_LOCAL:
            if why:
                raise unsupported_locally(why)
            return await self.create_local(p)
        if mode == MODE_LOCAL_FIRST:
            if why:  # can't print locally: the same cloud create as mode cloud (with the 409 loop)
                return await self.cloud_mode(p)
            try:
                return await self.create_local(p)
            except StationUnreachableError as sue:
                try:
                    return await self.cloud_leg(p, None)
                except CloudUnavailable as cu:  # the cloud surely doesn't have it either
                    sue.cloud_error = cu.err
                    raise sue from cu.err
                # A cloud answer (4xx…) or OutcomeUnknownError is raised as is.
        assert mode == MODE_CLOUD_FIRST
        if why:
            return await self.cloud_mode(p)
        try:
            return await self.cloud_leg(p, self.cfg.cloud_timeout)
        except CloudUnavailable as cu:
            err = cu.err
        try:
            return await self.create_local(p)
        except StationUnreachableError as sue:
            sue.cloud_error = err
            raise sue from err

    async def cloud_leg(self, p: JobParams, timeout: Optional[float]) -> Job:
        """See ``LocalManager.cloud_leg``. Cancelled while the job POST was in flight (maybe received):
        OutcomeUnknownError wrapping the cancellation."""
        return await self._cloud_loop(p, max_retries=0, timeout=timeout, fallback_allowed=True)

    async def cloud_mode(self, params: JobParams) -> Job:
        p = dict(params)
        if not p.get("idempotency_key"):
            p["idempotency_key"] = new_idempotency_key()
        return await self._cloud_loop(p, max_retries=None, timeout=None, fallback_allowed=False, cloud_mode=True)

    async def _cloud_loop(
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

        key = str(p["idempotency_key"])
        while True:
            try:
                return await jobs._create_cloud(
                    p, max_retries=max_retries, timeout=timeout, prepared=prepared, on_ambiguous=ambiguous
                )
            except asyncio.CancelledError as ce:
                if not cloud_mode and is_delivered(ce):
                    raise OutcomeUnknownError(self._c.base_url, key, ce) from ce
                raise
            except CatzPrintError as err:
                action = plan.after(err, fallback_allowed=fallback_allowed)
                if cloud_mode and action in ("retry", "unavailable") and plan.resends == 0:
                    action = "raise"
                if action == "raise":
                    raise
                if action == "unavailable":
                    raise CloudUnavailable(err) from err
                if action == "unknown":
                    raise OutcomeUnknownError(self._c.base_url, key, err) from err
                if action == "wait":
                    await self._c._sleep(DUPLICATE_WAIT)
                if action == "reseal":
                    # The API couldn't decrypt the sealed content (key rotated): fetch the key again and
                    # seal again, once; the same idempotency key goes out (S1).
                    self._c._drop_server_key()
                    prepared.clear()
                    try:
                        await jobs._prepare_into(p, prepared)
                    except CatzPrintError as kerr:
                        if plan.ambiguous or plan.ambiguous_retried:
                            raise OutcomeUnknownError(self._c.base_url, key, kerr) from kerr
                        raise err from None  # nothing more was sent: the API's answer stands

    async def lookup(self, t: Target, tok: str, key: str, missing_ok: bool = True) -> Tuple[str, Any]:
        """See ``LocalManager.lookup``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOOKUP_BUDGET
        last: Any = None
        for i in range(LOOKUP_TRIES):
            left = deadline - loop.time()
            if left <= 0:
                break
            if i:
                await asyncio.sleep(min(LOOKUP_PAUSE, left))
                left = deadline - loop.time()
                if left <= 0:
                    break
            try:
                status, headers, raw = await self.transport.arequest(
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

    async def _token_or_unreachable(self) -> str:
        try:
            return await self.token()
        except CatzPrintError as e:
            if not mint_error_is_unreachable(e):
                raise
            sue = StationUnreachableError()
            sue.add("", no_token_error(e))
            raise sue from e

    async def create_local(self, params: JobParams) -> Job:
        body = station_body(params)
        tok = await self._token_or_unreachable()
        sue = StationUnreachableError()
        q = wait_query(self.cfg.wait)
        reminted = False

        key = str(body["idempotency_key"])

        async def post(t: Target) -> SendResult:
            try:
                return await self.send(t, "POST", "/local/v1/jobs", query=q, token=tok, body=body, extra=self.cfg.wait)
            except asyncio.CancelledError as ce:
                if is_delivered(ce):  # C6: cancelled after the full write: the station may print it
                    raise OutcomeUnknownError(t.url, key, ce) from ce
                raise

        async def probe(t: Target, missing_ok: bool = True) -> Tuple[str, Any]:
            try:
                return await self.lookup(t, tok, key, missing_ok)
            except asyncio.CancelledError as ce:
                raise OutcomeUnknownError(t.url, key, ce) from ce

        async def step(t: Target) -> Tuple[bool, Any]:
            nonlocal tok, reminted
            unreachable, err, data = await post(t)
            if err is not None and not unreachable and isinstance(err, AuthenticationError) and not reminted:
                # 401 token_expired: mint a fresh token (rate-limited) and retry this station once.
                reminted = True
                nt, merr = await self.remint(err)
                if nt:
                    tok = nt
                    unreachable, err, data = await post(t)
                elif merr is not None:
                    err = with_remint_failure(err, merr)
            if err is None and not valid_local_job(data):
                err = invalid_job_answer(200)  # C5: a 2xx we can't read: maybe printed
            in_progress = err is not None and in_progress_answer(err)
            if err is not None and (in_progress or is_delivered(err)):
                # DELIVERED_NO_ANSWER: the station may have printed it. Ask it, never another one.
                outcome, found = await probe(t, missing_ok=not in_progress)
                if outcome == "missing":  # the station never recorded it: as if not delivered
                    sue.add(t.url, err)
                    return False, None
                if outcome == "answer":  # 409 duplicate: the key belongs to a job we can't see
                    raise found
                if outcome != "found":
                    raise OutcomeUnknownError(t.url, key, found if found is not None else err)
                err, data = None, found
            if err is None:
                lj = LocalJob.from_dict(data)
                lj.station_url = t.url
                self.state.remember_job(lj.id, t.url)
                return True, local_job_to_job(lj, t, params, token_live(tok))
            if unreachable:
                sue.add(t.url, err)
                return False, None
            if isinstance(err, AuthenticationError) and self.cfg.mode != MODE_LOCAL:
                sue.add(t.url, err)  # still refused: in the fallback modes this means "use the cloud"
                raise sue
            raise err

        try:
            done, job = await self.each_target(None, step)
            if done:
                res: Job = job
                return res
            raise sue
        finally:
            await self.maybe_refresh()

    async def authed(self, preferred: Optional[str], fn: Callable[[Target, str], Awaitable[SendResult]]) -> Any:
        tok = await self._token_or_unreachable()
        sue = StationUnreachableError()
        reminted = False

        async def step(t: Target) -> Tuple[bool, Any]:
            nonlocal tok, reminted
            unreachable, err, res = await fn(t, tok)
            if err is not None and not unreachable and isinstance(err, AuthenticationError) and not reminted:
                reminted = True
                nt, _ = await self.remint(err)
                if nt:
                    tok = nt
                    unreachable, err, res = await fn(t, tok)
            if err is None:
                return True, res
            if unreachable:
                sue.add(t.url, err)
                return False, None
            raise err

        try:
            done, res = await self.each_target(preferred, step)
            if done:
                return res
            raise sue
        finally:
            await self.maybe_refresh()

    async def target(self, station_url: str) -> Target:
        u = normalize_station_url(station_url)
        self.state.ensure_loaded()
        st = self.state.snapshot()
        with self.state.lock:
            mdns = list(self.state.mdns)
        t = self.state.target_for(u, "config", st, mdns)
        if t.needs_pin() and not t.fingerprint and self.cfg.discover:
            found, _ = await self.browse(True)
            if found:
                t = self.state.target_for(u, "config", st, found)
        return t

    def pair_target(self, station_url: str, fingerprint: Optional[str]) -> Target:
        u = normalize_station_url(station_url)
        if fingerprint:
            return pair_target_with_fingerprint(u, fingerprint)
        self.state.ensure_loaded()
        t = self.state.target_for(u, "config", self.state.snapshot(), [])
        pair_needs_trust(t)
        return t


class AsyncLocal:
    """``client.local`` for :class:`AsyncCatzPrint`."""

    def __init__(self, manager: AsyncLocalManager) -> None:
        self._m = manager

    @property
    def mode(self) -> str:
        return self._m.cfg.mode

    @property
    def store_error(self) -> Optional[BaseException]:
        with self._m.state.lock:
            return self._m.state.store_error

    async def status(self) -> LocalStatus:
        async def fn(t: Target, tok: str) -> SendResult:
            unreachable, err, data = await self._m.send(t, "GET", "/local/v1/status", token=tok)
            if err is not None:
                return unreachable, err, None
            st = LocalStatus.from_dict(data)
            st.station_url = t.url
            return False, None, st

        res: LocalStatus = await self._m.authed(None, fn)
        return res

    async def get_job(self, job_id: str) -> Job:
        m = self._m
        pref = m.state.job_url(job_id)
        not_found: List[CatzPrintError] = []

        async def fn(t: Target, tok: str) -> SendResult:
            unreachable, err, data = await m.send(t, "GET", "/local/v1/jobs/" + seg(job_id), token=tok)
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
            res: Job = await m.authed(pref, fn)
        except StationUnreachableError:
            if not_found:
                raise not_found[0] from None
            raise
        return res

    async def info(self, station_url: str) -> LocalInfo:
        t = await self._m.target(station_url)
        unreachable, err, data = await self._m.send(t, "GET", "/local/v1/info")
        if err is not None:
            if unreachable:
                sue = StationUnreachableError()
                sue.add(t.url, err)
                raise sue
            raise err
        return LocalInfo.from_dict(data)

    async def pair(
        self, station_url: str, code: str, device_name: Optional[str] = None, *, fingerprint: Optional[str] = None
    ) -> LocalPairing:
        check_pair_code(code)
        name = (device_name or self._m.cfg.device_name or "")[:80]
        t = self._m.pair_target(station_url, fingerprint)
        unreachable, err, data = await self._m.send(
            t, "POST", "/local/v1/pair", body={"code": code, "device_name": name}
        )
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

    async def discover(self) -> List[LocalStation]:
        m = self._m
        m.state.ensure_loaded()
        cloud_err: Optional[CatzPrintError] = None
        try:
            await m.refresh(True)
        except CatzPrintError as e:
            cloud_err = e
        st = m.state.snapshot()
        found, mdns_err = await m.browse(True)
        out = m.state.discovered_stations(st, found)
        if not out:
            if cloud_err is not None:
                raise cloud_err
            if mdns_err is not None:
                raise APIConnectionError(f"mDNS browse failed: {mdns_err}") from mdns_err
        return out

    async def refresh(self) -> None:
        await self._m.refresh(True)

    async def token(self) -> str:
        return await self._m.token()


class AsyncLocalTokens:
    """``client.local_tokens`` for :class:`AsyncCatzPrint`."""

    def __init__(self, client: AsyncCatzPrint) -> None:
        self._c = client

    async def create(
        self, *, device_name: str, location_ids: Optional[Sequence[str]] = None, ttl_days: Optional[int] = None
    ) -> LocalToken:
        body: Dict[str, Any] = {"device_name": device_name}
        if location_ids:
            body["location_ids"] = list(location_ids)
        if ttl_days:
            body["ttl_days"] = ttl_days
        return await self._create(body)

    async def _create(
        self, body: Dict[str, Any], *, max_retries: Optional[int] = None, timeout: Optional[float] = None
    ) -> LocalToken:
        check_token_body(body)
        res = await self._c._request(
            "POST", await self._c._org_path("/local-tokens"), json=body, max_retries=max_retries, timeout=timeout
        )
        lt = LocalToken.from_dict(res if isinstance(res, dict) else {})
        if not lt.token:
            raise CatzPrintError("The API answered without a token.", status=201, code="invalid_response")
        return lt

    async def revoke(self, token_id: str) -> None:
        await self._c._request("DELETE", await self._c._org_path(f"/local-tokens/{seg(token_id)}"))


class AsyncLocalEndpoints:
    """``client.local_endpoints`` for :class:`AsyncCatzPrint`."""

    def __init__(self, client: AsyncCatzPrint) -> None:
        self._c = client

    async def list(self, location_id: Optional[str] = None) -> List[LocalEndpoint]:
        return await self._list(location_id)

    async def _list(
        self, location_id: Optional[str], *, max_retries: Optional[int] = None, timeout: Optional[float] = None
    ) -> List[LocalEndpoint]:
        params = {"location_id": location_id} if location_id else None
        res = await self._c._request(
            "GET",
            await self._c._org_path("/local-endpoints"),
            params=params,
            max_retries=max_retries,
            timeout=timeout,
        )
        return endpoints_list(res)
