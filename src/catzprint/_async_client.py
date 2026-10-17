"""Asynchronous client (asyncio, built on ``httpx.AsyncClient``)."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Union

import httpx

from ._base import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    RETRY_STATUSES,
    SAFE_METHODS,
    ClientConfig,
    Content,
    ResponseTooLarge,
    api_error,
    aread_limited,
    build_job,
    decode_body,
    list_params,
    logger,
    read_file_arg,
    redact_key,
    seg,
    too_large_error,
)
from ._errors import APIConnectionError, CatzPrintError, WaitTimeoutError
from ._models import ApiKeyInfo, Job, JobList, Location, Printer, Template
from ._sealed import seal
from .webhooks import Webhooks

__all__ = ["AsyncCatzPrint"]

PathArg = Union[Content, str, "os.PathLike[str]"]


class AsyncCatzPrint:
    """Asynchronous CatzPrint client.

    >>> async with AsyncCatzPrint("cz_test_...") as client:
    ...     job = await client.print_text("Hello", printer_id="...")
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
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._config = ClientConfig(api_key, base_url, org_id, timeout, max_retries, backoff_base, max_backoff)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._lock: Optional[asyncio.Lock] = None
        self._key_info: Optional[ApiKeyInfo] = None
        self._server_key: Optional[str] = None
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self.jobs = AsyncJobs(self)
        self.printers = AsyncPrinters(self)
        self.locations = AsyncLocations(self)
        self.templates = AsyncTemplates(self)
        self.webhooks = Webhooks()

    # -- lifecycle -------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    close = aclose

    async def __aenter__(self) -> AsyncCatzPrint:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"AsyncCatzPrint(api_key={redact_key(self._config.api_key)!r}, base_url={self._config.base_url!r})"

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def is_test_key(self) -> bool:
        """True for ``cz_test_`` keys (jobs are never billed)."""
        return self._config.is_test_key

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- HTTP ------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        json: Any = None,
        idempotent: bool = False,
        auth: bool = True,
    ) -> Any:
        url = self._config.base_url + path
        retryable = method.upper() in SAFE_METHODS or idempotent
        policy = self._config.retry
        attempt = 0
        while True:
            try:
                async with self._http.stream(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=self._config.headers(auth),
                    timeout=self._config.timeout,
                ) as resp:
                    status = resp.status_code
                    resp_headers = resp.headers
                    content = await aread_limited(resp_headers, resp.aiter_bytes())
            except ResponseTooLarge:
                raise too_large_error(status) from None
            except httpx.HTTPError as e:
                if policy.should_retry(attempt, retryable):
                    delay = policy.delay(attempt)
                    logger.debug(
                        "catzprint %s %s failed (%s); retrying in %.2fs", method, path, type(e).__name__, delay
                    )
                    await self._sleep(delay)
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
            if status in RETRY_STATUSES and policy.should_retry(attempt, retryable, err.retry_after):
                delay = policy.delay(attempt, err.retry_after)
                logger.debug("catzprint %s %s -> %d; retrying in %.2fs", method, path, status, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            raise err

    async def _org_path(self, suffix: str) -> str:
        return f"/v1/orgs/{seg(await self.org_id())}{suffix}"

    async def org_id(self) -> str:
        """The organization of the API key (looked up once via ``GET /v1/key``, then cached)."""
        if self._config.org_id:
            return self._config.org_id
        info = await self.key()
        self._config.org_id = info.org_id
        return info.org_id

    async def key(self, *, refresh: bool = False) -> ApiKeyInfo:
        """Describe the API key: ``id``, ``org_id``, ``org_name``, ``live``, ``scopes``."""
        async with self._get_lock():
            if self._key_info is None or refresh:
                self._key_info = ApiKeyInfo.from_dict(await self._request("GET", "/v1/key"))
            return self._key_info

    whoami = key

    async def server_public_key(self, *, refresh: bool = False) -> str:
        """The server's X25519 public key (base64) used for sealed printing; cached."""
        async with self._get_lock():
            if self._server_key is None or refresh:
                res = await self._request("GET", "/v1/public/server-key", auth=False)
                pk = res.get("public_key") if isinstance(res, dict) else None
                if not isinstance(pk, str) or not pk:
                    raise CatzPrintError("The server didn't return a public key.")
                self._server_key = pk
            return self._server_key

    # -- convenience helpers ----------------------------------------------
    async def print_text(self, text: str, **opts: Any) -> Job:
        """Print plain text."""
        return await self.jobs.create(text=text, **opts)

    async def print_pdf(self, pdf: PathArg, **opts: Any) -> Job:
        """Print a PDF given as bytes or a file path."""
        data, name = read_file_arg(pdf, opts.pop("file_name", None))
        return await self.jobs.create(content=data, kind=opts.pop("kind", "pdf"), file_name=name, **opts)

    async def print_image(self, image: PathArg, **opts: Any) -> Job:
        """Print a PNG or JPEG given as bytes or a file path."""
        data, name = read_file_arg(image, opts.pop("file_name", None))
        return await self.jobs.create(content=data, kind=opts.pop("kind", "image"), file_name=name, **opts)

    async def print_receipt(self, commands: Sequence[Mapping[str, Any]], **opts: Any) -> Job:
        """Print an ESC/POS command list (see :mod:`catzprint.commands`)."""
        return await self.jobs.create(commands=commands, **opts)

    async def print_template(self, template_id: str, data: Optional[Mapping[str, Any]] = None, **opts: Any) -> Job:
        """Print a saved template filled with ``data``."""
        return await self.jobs.create(template_id=template_id, data=data or {}, **opts)


class AsyncJobs:
    """``client.jobs`` for :class:`AsyncCatzPrint`."""

    def __init__(self, client: AsyncCatzPrint) -> None:
        self._c = client

    async def create(
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
        """Create a print job. Give exactly one of ``text``, ``commands``, ``template_id`` or ``content``."""
        draft = build_job(
            printer_id=printer_id,
            location_id=location_id,
            role=role,
            kind=kind,
            title=title,
            reference=reference,
            copies=copies,
            idempotency_key=idempotency_key,
            scheduled_for=scheduled_for,
            options=options,
            text=text,
            commands=commands,
            template_id=template_id,
            data=data,
            content=content,
            file_name=file_name,
            sealed=sealed,
        )
        ciphertext = None
        if draft.seal_payload is not None:
            ciphertext = seal(await self._c.server_public_key(), draft.seal_payload)
        body = draft.finish(ciphertext)
        res = await self._c._request("POST", await self._c._org_path("/jobs"), json=body, idempotent=True)
        return Job.from_dict(res["job"])

    async def get(self, job_id: str) -> Job:
        """Fetch a job, with its ``events`` history."""
        res = await self._c._request("GET", await self._c._org_path(f"/jobs/{seg(job_id)}"))
        return Job.from_dict(res["job"], res.get("events"))

    async def list(
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
        return JobList.from_dict(await self._c._request("GET", await self._c._org_path("/jobs"), params=params))

    async def iter(
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
    ) -> AsyncIterator[Job]:
        """Iterate over every matching job (``async for``), fetching pages as needed."""
        cursor: Optional[str] = None
        seen = 0
        while True:
            page = await self.list(
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

    async def cancel(self, job_id: str) -> Job:
        """Cancel a scheduled, held, queued or sent job."""
        res = await self._c._request("POST", await self._c._org_path(f"/jobs/{seg(job_id)}/cancel"))
        return Job.from_dict(res["job"])

    async def retry(self, job_id: str) -> Job:
        """Queue a failed or held job again."""
        res = await self._c._request("POST", await self._c._org_path(f"/jobs/{seg(job_id)}/retry"))
        return Job.from_dict(res["job"])

    async def reprint(self, job_id: str, *, printer_id: Optional[str] = None, copies: Optional[int] = None) -> Job:
        """Print a job again (optionally on another printer); returns the new job."""
        body: Dict[str, Any] = {}
        if printer_id is not None:
            body["printer_id"] = printer_id
        if copies is not None:
            body["copies"] = int(copies)
        res = await self._c._request("POST", await self._c._org_path(f"/jobs/{seg(job_id)}/reprint"), json=body)
        return Job.from_dict(res["job"])

    async def wait_for(self, job_id: str, *, timeout: float = 60.0, interval: float = 1.0) -> Job:
        """Poll until the job is printed, failed or cancelled; raise :class:`WaitTimeoutError` on timeout."""
        if not interval or interval <= 0 or interval != interval:
            interval = 1.0  # never poll in a tight loop
        deadline = time.monotonic() + timeout
        while True:
            job = await self.get(job_id)
            if job.is_final:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WaitTimeoutError(f"Job {job_id} is still {job.status} after {timeout:g}s.", job=job)
            await self._c._sleep(min(interval, remaining))

    async def print_text(self, text: str, **opts: Any) -> Job:
        return await self._c.print_text(text, **opts)

    async def print_pdf(self, pdf: PathArg, **opts: Any) -> Job:
        return await self._c.print_pdf(pdf, **opts)

    async def print_image(self, image: PathArg, **opts: Any) -> Job:
        return await self._c.print_image(image, **opts)

    async def print_receipt(self, commands: Sequence[Mapping[str, Any]], **opts: Any) -> Job:
        return await self._c.print_receipt(commands, **opts)

    async def print_template(self, template_id: str, data: Optional[Mapping[str, Any]] = None, **opts: Any) -> Job:
        return await self._c.print_template(template_id, data, **opts)


class AsyncPrinters:
    """``client.printers`` for :class:`AsyncCatzPrint`."""

    def __init__(self, client: AsyncCatzPrint) -> None:
        self._c = client

    async def list(self, *, station_id: Optional[str] = None) -> List[Printer]:
        params = {"station_id": station_id} if station_id else None
        res = await self._c._request("GET", await self._c._org_path("/printers"), params=params)
        return [Printer.from_dict(p) for p in res.get("printers") or []]


class AsyncLocations:
    """``client.locations`` for :class:`AsyncCatzPrint`."""

    def __init__(self, client: AsyncCatzPrint) -> None:
        self._c = client

    async def list(self) -> List[Location]:
        res = await self._c._request("GET", await self._c._org_path("/locations"))
        return [Location.from_dict(x) for x in res.get("locations") or []]

    async def get(self, location_id: str) -> Location:
        res = await self._c._request("GET", await self._c._org_path(f"/locations/{seg(location_id)}"))
        return Location.from_dict(res["location"])


class AsyncTemplates:
    """``client.templates`` for :class:`AsyncCatzPrint`."""

    def __init__(self, client: AsyncCatzPrint) -> None:
        self._c = client

    async def list(self) -> List[Template]:
        res = await self._c._request("GET", await self._c._org_path("/templates"))
        return [Template.from_dict(x) for x in res.get("templates") or []]
