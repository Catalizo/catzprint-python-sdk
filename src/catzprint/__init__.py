"""CatzPrint: print receipts, PDFs and images to any printer from Python.

Sync::

    from catzprint import CatzPrint
    client = CatzPrint()          # reads CATZPRINT_API_KEY
    job = client.print_text("Hello from Python", role="receipt")

Async::

    from catzprint import AsyncCatzPrint
    async with AsyncCatzPrint() as client:
        job = await client.print_text("Hello", role="receipt")
"""

from __future__ import annotations

from . import commands, webhooks
from ._async_client import AsyncCatzPrint, AsyncJobs, AsyncLocations, AsyncPrinters, AsyncTemplates
from ._base import DEFAULT_BASE_URL, USER_AGENT
from ._client import CatzPrint, Jobs, Locations, Printers, Templates
from ._errors import (
    APIConnectionError,
    AuthenticationError,
    CatzPrintError,
    ConflictError,
    NotFoundError,
    OutOfPrintsError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    SignatureVerificationError,
    ValidationError,
    WaitTimeoutError,
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
    "USER_AGENT",
    "APIConnectionError",
    "ApiKeyInfo",
    "AsyncCatzPrint",
    "AsyncJobs",
    "AsyncLocations",
    "AsyncPrinters",
    "AsyncTemplates",
    "AuthenticationError",
    "CatzPrint",
    "CatzPrintError",
    "ConflictError",
    "ConnectionError",
    "Job",
    "JobEvent",
    "JobList",
    "Jobs",
    "Location",
    "Locations",
    "NotFoundError",
    "OutOfPrintsError",
    "PermissionDeniedError",
    "PermissionError",
    "Printer",
    "Printers",
    "RateLimitError",
    "ServerError",
    "SignatureVerificationError",
    "Template",
    "Templates",
    "TimeoutError",
    "ValidationError",
    "WaitTimeoutError",
    "Webhook",
    "WebhookEvent",
    "Webhooks",
    "__version__",
    "commands",
    "construct_event",
    "seal",
    "sealing_available",
    "webhooks",
]
