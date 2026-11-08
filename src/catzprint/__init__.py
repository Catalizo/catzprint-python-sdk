"""CatzPrint: print receipts, PDFs and images to any printer from Python.

Sync::

    from catzprint import CatzPrint
    client = CatzPrint()          # reads CATZPRINT_API_KEY
    job = client.print_text("Hello from Python", role="receipt")

Async::

    from catzprint import AsyncCatzPrint
    async with AsyncCatzPrint() as client:
        job = await client.print_text("Hello", role="receipt")

Local printing (stations on the same network, also while the internet is down)::

    from catzprint import CatzPrint, LocalConfig
    client = CatzPrint(local=LocalConfig(mode="local_first", location_id="...", discover=True))
"""

from __future__ import annotations

from . import commands, webhooks
from ._async_client import AsyncCatzPrint, AsyncJobs, AsyncLocations, AsyncPrinters, AsyncTemplates
from ._async_local import AsyncLocal, AsyncLocalEndpoints, AsyncLocalTokens
from ._base import DEFAULT_BASE_URL, USER_AGENT
from ._client import CatzPrint, Jobs, Locations, Printers, Templates
from ._errors import (
    APIConnectionError,
    AuthenticationError,
    CatzPrintError,
    ConflictError,
    NotFoundError,
    OutcomeUnknownError,
    OutOfPrintsError,
    PermissionDeniedError,
    PinMismatchError,
    PrinterError,
    RateLimitError,
    ServerError,
    SignatureVerificationError,
    StationAttempt,
    StationUnreachableError,
    ValidationError,
    WaitTimeoutError,
    is_cover_open,
    is_offline_allowance_exhausted,
    is_out_of_paper,
    is_outcome_unknown,
    is_printer_offline,
    is_station_unreachable,
)
from ._local import Local, LocalEndpoints, LocalTokens
from ._local_core import MODE_CLOUD, MODE_CLOUD_FIRST, MODE_LOCAL, MODE_LOCAL_FIRST, LocalConfig
from ._local_models import (
    VIA_CLOUD,
    VIA_LOCAL,
    LocalAllowance,
    LocalEndpoint,
    LocalInfo,
    LocalJob,
    LocalPairing,
    LocalPrinterStatus,
    LocalStation,
    LocalStationState,
    LocalStatus,
    LocalToken,
)
from ._models import (
    FINAL_JOB_STATUSES,
    JOB_STATUSES,
    ApiKeyInfo,
    Job,
    JobEvent,
    JobList,
    Location,
    Printer,
    Template,
    WebhookEvent,
)
from ._sealed import seal, sealing_available
from ._tokenstore import FileTokenStore, MemoryTokenStore, StoredToken, TokenStore
from ._version import __version__
from .webhooks import Webhooks, construct_event

# Names from the cross-language spec. They are aliases (not the builtins).
PermissionError = PermissionDeniedError  # noqa: A001
ConnectionError = APIConnectionError  # noqa: A001
TimeoutError = WaitTimeoutError  # noqa: A001
Webhook = Webhooks

__all__ = [
    "DEFAULT_BASE_URL",
    "FINAL_JOB_STATUSES",
    "JOB_STATUSES",
    "MODE_CLOUD",
    "MODE_CLOUD_FIRST",
    "MODE_LOCAL",
    "MODE_LOCAL_FIRST",
    "USER_AGENT",
    "VIA_CLOUD",
    "VIA_LOCAL",
    "APIConnectionError",
    "ApiKeyInfo",
    "AsyncCatzPrint",
    "AsyncJobs",
    "AsyncLocal",
    "AsyncLocalEndpoints",
    "AsyncLocalTokens",
    "AsyncLocations",
    "AsyncPrinters",
    "AsyncTemplates",
    "AuthenticationError",
    "CatzPrint",
    "CatzPrintError",
    "ConflictError",
    "ConnectionError",
    "FileTokenStore",
    "Job",
    "JobEvent",
    "JobList",
    "Jobs",
    "Local",
    "LocalAllowance",
    "LocalConfig",
    "LocalEndpoint",
    "LocalEndpoints",
    "LocalInfo",
    "LocalJob",
    "LocalPairing",
    "LocalPrinterStatus",
    "LocalStation",
    "LocalStationState",
    "LocalStatus",
    "LocalToken",
    "LocalTokens",
    "Location",
    "Locations",
    "MemoryTokenStore",
    "NotFoundError",
    "OutOfPrintsError",
    "OutcomeUnknownError",
    "PermissionDeniedError",
    "PermissionError",
    "PinMismatchError",
    "Printer",
    "PrinterError",
    "Printers",
    "RateLimitError",
    "ServerError",
    "SignatureVerificationError",
    "StationAttempt",
    "StationUnreachableError",
    "StoredToken",
    "Template",
    "Templates",
    "TimeoutError",
    "TokenStore",
    "ValidationError",
    "WaitTimeoutError",
    "Webhook",
    "WebhookEvent",
    "Webhooks",
    "__version__",
    "commands",
    "construct_event",
    "is_cover_open",
    "is_offline_allowance_exhausted",
    "is_out_of_paper",
    "is_outcome_unknown",
    "is_printer_offline",
    "is_station_unreachable",
    "seal",
    "sealing_available",
    "webhooks",
]
