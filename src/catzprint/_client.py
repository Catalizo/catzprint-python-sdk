"""Synchronous client."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import httpx

from ._base import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    SAFE_METHODS,
    SERVER_KEY_TTL,
    ClientConfig,
    Content,
    ResponseTooLarge,
    api_error,
    build_job,
    check_idempotency_key,
    created_job,
    decode_body,
    list_params,
    logger,
    mark_not_sent,
    read_file_arg,
    read_limited,
    redact_key,
    seg,
    too_large_error,
)
from ._base import RETRY_STATUSES as _RETRY_STATUSES
from ._errors import APIConnectionError, CatzPrintError, StationUnreachableError, WaitTimeoutError
from ._local import NOT_DELIVERED_HTTPX, Local, LocalEndpoints, LocalManager, LocalTokens
from ._local_core import JobParams, LocalConfig, static_token_mode
from ._local_models import VIA_CLOUD
from ._models import ApiKeyInfo, Job, JobList, Location, Printer, Template
from ._sealed import seal
from .webhooks import Webhooks

__all__ = ["CatzPrint"]

PathArg = Union[Content, str, "os.PathLike[str]"]


class CatzPrint:
    """Synchronous CatzPrint client.

    >>> client = CatzPrint("cz_test_...")
    >>> job = client.print_text("Hello", printer_id="...")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        org_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = 0.5,
        max_backoff: float = 8.0,
        http_client: Optional[httpx.Client] = None,
        local: Optional[LocalConfig] = None,
    ) -> None:
        self._config = ClientConfig(
            api_key,
            base_url,
            org_id,
            timeout,
            max_retries,
            backoff_base,
            max_backoff,
            allow_no_key=static_token_mode(local),
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._lock = threading.Lock()
        self._key_info: Optional[ApiKeyInfo] = None
        self._server_key_gen = 0
        self._server_key: Optional[str] = None
        self._server_key_at = 0.0
        self._sleep: Callable[[float], None] = time.sleep
        self.jobs = Jobs(self)
        self.printers = Printers(self)
        self.locations = Locations(self)
        self.templates = Templates(self)
        self.webhooks = Webhooks()
        self.local_tokens = LocalTokens(self)
        self.local_endpoints = LocalEndpoints(self)
        self._local = LocalManager(self, local)
        #: Stations on the local network (status, discovery, pairing, local jobs). See ``LocalConfig``.
        self.local = Local(self._local)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> CatzPrint:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"CatzPrint(api_key={redact_key(self._config.api_key)!r}, base_url={self._config.base_url!r})"

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def is_test_key(self) -> bool:
        """True for ``cz_test_`` keys (jobs are never billed)."""
        return self._config.is_test_key

    # -- HTTP ------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        json: Any = None,
        idempotent: bool = False,
        auth: bool = True,
        max_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        on_ambiguous: Optional[Callable[[], None]] = None,
    ) -> Any:
        self._config.require_key()
        url = self._config.base_url + path
        retryable = method.upper() in SAFE_METHODS or idempotent
        policy = self._config.policy(max_retries)
        attempt = 0
        while True:
            try:
                with self._http.stream(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=self._config.headers(auth),
                    timeout=self._config.timeout if timeout is None else timeout,
                ) as resp:
                    status = resp.status_code
                    resp_headers = resp.headers
                    content = read_limited(resp_headers, resp.iter_bytes())
            except ResponseTooLarge:
                raise too_large_error(status) from None
            except httpx.HTTPError as e:
                if policy.should_retry(attempt, retryable):
                    if on_ambiguous is not None and not isinstance(e, NOT_DELIVERED_HTTPX):
                        on_ambiguous()  # the failed attempt may have reached the API
                    delay = policy.delay(attempt)
                    logger.debug(
                        "catzprint %s %s failed (%s); retrying in %.2fs", method, path, type(e).__name__, delay
                    )
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise APIConnectionError(f"Couldn't reach CatzPrint: {type(e).__name__}: {e}") from e
            logger.debug("catzprint %s %s -> %d", method, path, status)
            if 300 <= status < 400:
                # Redirects are never followed, so the API key only goes to the configured host.
                raise CatzPrintError(
                    "The API answered with a redirect, which the SDK doesn't follow. Check the base URL.",
                    status=status,
                    code="unexpected_redirect",
                )
            if status < 400:
                return decode_body(content, resp_headers.get("content-type", ""))
            err = api_error(status, content, resp_headers)
            if status in _RETRY_STATUSES and policy.should_retry(attempt, retryable, err.retry_after):
                delay = policy.delay(attempt, err.retry_after)
                logger.debug("catzprint %s %s -> %d; retrying in %.2fs", method, path, status, delay)
                self._sleep(delay)
                attempt += 1
                continue
            raise err

    def _org_path(self, suffix: str) -> str:
        return f"/v1/orgs/{seg(self.org_id())}{suffix}"

    def org_id(self) -> str:
        """The organization of the API key (looked up once via ``GET /v1/key``, then cached)."""
        if self._config.org_id:
            return self._config.org_id
        info = self.key()
        self._config.org_id = info.org_id
        return info.org_id

    def key(self, *, refresh: bool = False) -> ApiKeyInfo:
        """Describe the API key: ``id``, ``org_id``, ``org_name``, ``live``, ``scopes``."""
        with self._lock:
            if self._key_info is None or refresh:
                self._key_info = ApiKeyInfo.from_dict(self._request("GET", "/v1/key"))
            return self._key_info

    whoami = key

    def server_public_key(self, *, refresh: bool = False) -> str:
        """The server's X25519 public key (base64) used for sealed printing. Cached for an hour (the
        server may rotate it), and dropped at once when the API can't decrypt a sealed job."""
        with self._lock:
            stale = time.monotonic() - self._server_key_at >= SERVER_KEY_TTL
            if self._server_key is None or refresh or stale:
                gen = self._server_key_gen
                res = self._request("GET", "/v1/public/server-key", auth=False)
                pk = res.get("public_key") if isinstance(res, dict) else None
                if not isinstance(pk, str) or not pk:
                    raise CatzPrintError("The server didn't return a public key.")
                if gen == self._server_key_gen:
                    # Only a fetch that started after the last drop installs its key: an older
                    # in-flight fetch must not bring a dropped (rotated) key back.
                    self._server_key = pk
                    self._server_key_at = time.monotonic()
                return pk
            return self._server_key

    def _drop_server_key(self) -> None:
        """Forget the cached server key (the API couldn't decrypt a sealed job)."""
        self._server_key = None
        self._server_key_at = 0.0
        self._server_key_gen += 1

    # -- convenience helpers ----------------------------------------------
    def print_text(self, text: str, **opts: Any) -> Job:
        """Print plain text (rendered for receipt printers, or as a PDF for office printers)."""
        return self.jobs.create(text=text, **opts)

    def print_pdf(self, pdf: PathArg, **opts: Any) -> Job:
        """Print a PDF given as bytes or a file path."""
        data, name = read_file_arg(pdf, opts.pop("file_name", None))
        return self.jobs.create(content=data, kind=opts.pop("kind", "pdf"), file_name=name, **opts)

    def print_image(self, image: PathArg, **opts: Any) -> Job:
        """Print a PNG or JPEG given as bytes or a file path."""
        data, name = read_file_arg(image, opts.pop("file_name", None))
        return self.jobs.create(content=data, kind=opts.pop("kind", "image"), file_name=name, **opts)

    def print_receipt(self, commands: Sequence[Mapping[str, Any]], **opts: Any) -> Job:
        """Print an ESC/POS command list (see :mod:`catzprint.commands`)."""
        return self.jobs.create(commands=commands, **opts)

    def print_template(self, template_id: str, data: Optional[Mapping[str, Any]] = None, **opts: Any) -> Job:
        """Print a saved template filled with ``data``."""
        return self.jobs.create(template_id=template_id, data=data or {}, **opts)


class Jobs:
    """``client.jobs``: create, read, list, cancel, retry, reprint and wait for print jobs."""

    def __init__(self, client: CatzPrint) -> None:
        self._c = client

    def create(
        self,
        *,
        printer_id: Optional[str] = None,
        location_id: Optional[str] = None,
        role: Optional[str] = None,
        kind: Optional[str] = None,
        title: Optional[str] = None,
        reference: Optional[str] = None,
        copies: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        scheduled_for: Union[datetime, str, None] = None,
        options: Optional[Mapping[str, Any]] = None,
        text: Optional[str] = None,
        commands: Optional[Sequence[Mapping[str, Any]]] = None,
        template_id: Optional[str] = None,
        data: Optional[Mapping[str, Any]] = None,
        content: Optional[Content] = None,
        file_name: Optional[str] = None,
        sealed: bool = False,
    ) -> Job:
        """Create a print job. Give exactly one of ``text``, ``commands``, ``template_id`` or ``content``.

        An idempotency key is generated when you don't pass one, so the
        request is retried safely on network errors and 429/502/503/504.

        With ``CatzPrint(local=LocalConfig(...))`` in a mode other than ``cloud``, the job goes to a
        station on the local network and/or the cloud as the mode says; ``job.via`` tells which one took
        it. In the local modes the same idempotency key (``"sdk-<uuid4>"`` when you set none) goes to
        the station and the cloud, so a fallback never prints twice.
        """
        params: JobParams = {
            "printer_id": printer_id,
            "location_id": location_id,
            "role": role,
            "kind": kind,
            "title": title,
            "reference": reference,
            "copies": copies,
            "idempotency_key": check_idempotency_key(idempotency_key),
            "scheduled_for": scheduled_for,
            "options": options,
            "text": text,
            "commands": commands,
            "template_id": template_id,
            "data": data,
            "content": content,
            "file_name": file_name,
            "sealed": sealed,
        }
        return self._c._local.create(params)

    def _prepare_cloud(self, params: JobParams) -> Tuple[Dict[str, Any], str]:
        draft = build_job(**params)
        try:  # anything failing here happens before the job POST: the job is NOT_DELIVERED
            ciphertext = None
            if draft.seal_payload is not None:
                ciphertext = seal(self._c.server_public_key(), draft.seal_payload)
            path = self._c._org_path("/jobs")
        except CatzPrintError as e:
            raise mark_not_sent(e) from None
        return draft.finish(ciphertext), path

    def _create_cloud(
        self,
        params: JobParams,
        *,
        max_retries: Optional[int] = None,
        timeout: Optional[float] = None,
        prepared: Optional[Dict[str, Any]] = None,
        on_ambiguous: Optional[Callable[[], None]] = None,
    ) -> Job:
        """Create the job in the cloud. ``prepared`` (a dict the caller keeps) holds the body and path
        after the first call, so re-sends are byte-identical (a sealed job isn't sealed again)."""
        if prepared is not None and "body" in prepared:
            body, path = prepared["body"], prepared["path"]
        else:
            body, path = self._prepare_cloud(params)
            if prepared is not None:
                prepared["body"], prepared["path"] = body, path
        res = self._c._request(
            "POST",
            path,
            json=body,
            idempotent=True,
            max_retries=max_retries,
            timeout=timeout,
            on_ambiguous=on_ambiguous,
        )
        job = Job.from_dict(created_job(res))
        job.via = VIA_CLOUD
        return job

    def _prepare_into(self, params: JobParams, prepared: Dict[str, Any]) -> None:
        """Build (and seal) the create into ``prepared`` for the re-sends that follow."""
        prepared["body"], prepared["path"] = self._prepare_cloud(params)

    def get(self, job_id: str) -> Job:
        """Fetch a job, with its ``events`` history. Local job IDs (``lj_…``) are read from the station."""
        if job_id.startswith("lj_"):
            return self._c.local.get_job(job_id)
        res = self._c._request("GET", self._c._org_path(f"/jobs/{seg(job_id)}"))
        job = Job.from_dict(res["job"], res.get("events"))
        job.via = VIA_CLOUD
        return job

    def list(
        self,
        *,
        status: Union[str, Sequence[str], None] = None,
        printer_id: Optional[str] = None,
        station_id: Optional[str] = None,
        location_id: Optional[str] = None,
        source: Optional[str] = None,
        live: Optional[bool] = None,
        q: Optional[str] = None,
        since: Union[datetime, str, None] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> JobList:
        """One page of jobs, newest first. Use :meth:`iter` to walk every page."""
        params = list_params(
            status=status,
            printer_id=printer_id,
            station_id=station_id,
            location_id=location_id,
            source=source,
            live=live,
            q=q,
            since=since,
            limit=limit,
            cursor=cursor,
        )
        return JobList.from_dict(self._c._request("GET", self._c._org_path("/jobs"), params=params))

    def iter(
        self,
        *,
        status: Union[str, Sequence[str], None] = None,
        printer_id: Optional[str] = None,
        station_id: Optional[str] = None,
        location_id: Optional[str] = None,
        source: Optional[str] = None,
        live: Optional[bool] = None,
        q: Optional[str] = None,
        since: Union[datetime, str, None] = None,
        page_size: int = 100,
        max_items: Optional[int] = None,
    ) -> Iterator[Job]:
        """Iterate over every matching job, fetching pages as needed."""
        cursor: Optional[str] = None
        seen = 0
        while True:
            page = self.list(
                status=status,
                printer_id=printer_id,
                station_id=station_id,
                location_id=location_id,
                source=source,
                live=live,
                q=q,
                since=since,
                limit=page_size,
                cursor=cursor,
            )
            for job in page.items:
                if max_items is not None and seen >= max_items:
                    return
                seen += 1
                yield job
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def cancel(self, job_id: str) -> Job:
        """Cancel a scheduled, held, queued or sent job."""
        res = self._c._request("POST", self._c._org_path(f"/jobs/{seg(job_id)}/cancel"))
        return Job.from_dict(res["job"])

    def retry(self, job_id: str) -> Job:
        """Queue a failed or held job again."""
        res = self._c._request("POST", self._c._org_path(f"/jobs/{seg(job_id)}/retry"))
        return Job.from_dict(res["job"])

    def reprint(self, job_id: str, *, printer_id: Optional[str] = None, copies: Optional[int] = None) -> Job:
        """Print a job again (optionally on another printer); returns the new job."""
        body: Dict[str, Any] = {}
        if printer_id is not None:
            body["printer_id"] = printer_id
        if copies is not None:
            body["copies"] = int(copies)
        res = self._c._request("POST", self._c._org_path(f"/jobs/{seg(job_id)}/reprint"), json=body)
        return Job.from_dict(res["job"])

    def wait_for(self, job_id: str, *, timeout: float = 60.0, interval: float = 1.0) -> Job:
        """Poll until the job is printed, failed or cancelled; raise :class:`WaitTimeoutError` on timeout."""
        if not interval or interval <= 0 or interval != interval:
            interval = 1.0  # never poll in a tight loop
        deadline = time.monotonic() + timeout
        last: Optional[Job] = None
        while True:
            try:
                job: Optional[Job] = self.get(job_id)
            except StationUnreachableError:
                if not job_id.startswith("lj_"):
                    raise
                job = None  # a local job's station is briefly unreachable: keep polling until the deadline
            if job is not None:
                if job.is_final:
                    return job
                last = job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = last.status if last is not None else "unknown"
                raise WaitTimeoutError(f"Job {job_id} is still {status} after {timeout:g}s.", job=last)
            self._c._sleep(min(interval, remaining))

    # Convenience helpers (same as on the client).
    def print_text(self, text: str, **opts: Any) -> Job:
        return self._c.print_text(text, **opts)

    def print_pdf(self, pdf: PathArg, **opts: Any) -> Job:
        return self._c.print_pdf(pdf, **opts)

    def print_image(self, image: PathArg, **opts: Any) -> Job:
        return self._c.print_image(image, **opts)

    def print_receipt(self, commands: Sequence[Mapping[str, Any]], **opts: Any) -> Job:
        return self._c.print_receipt(commands, **opts)

    def print_template(self, template_id: str, data: Optional[Mapping[str, Any]] = None, **opts: Any) -> Job:
        return self._c.print_template(template_id, data, **opts)


class Printers:
    """``client.printers``."""

    def __init__(self, client: CatzPrint) -> None:
        self._c = client

    def list(self, *, station_id: Optional[str] = None) -> List[Printer]:
        params = {"station_id": station_id} if station_id else None
        res = self._c._request("GET", self._c._org_path("/printers"), params=params)
        return [Printer.from_dict(p) for p in res.get("printers") or []]


class Locations:
    """``client.locations``."""

    def __init__(self, client: CatzPrint) -> None:
        self._c = client

    def list(self) -> List[Location]:
        res = self._c._request("GET", self._c._org_path("/locations"))
        return [Location.from_dict(x) for x in res.get("locations") or []]

    def get(self, location_id: str) -> Location:
        res = self._c._request("GET", self._c._org_path(f"/locations/{seg(location_id)}"))
        return Location.from_dict(res["location"])


class Templates:
    """``client.templates``."""

    def __init__(self, client: CatzPrint) -> None:
        self._c = client

    def list(self) -> List[Template]:
        res = self._c._request("GET", self._c._org_path("/templates"))
        return [Template.from_dict(x) for x in res.get("templates") or []]
