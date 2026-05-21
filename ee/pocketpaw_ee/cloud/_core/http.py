"""HTTP-layer glue for ee/cloud — exception handler, registration helpers.

In Phase 0 the only function exposed is the `CloudError` → JSON-envelope
mapping that already lives inline at `ee/cloud/__init__.py:58`. We
extract it so it can be imported, unit-tested without booting the full
cloud app, and consistently registered.

Subsequent phases extend this module with: a request-id middleware that
propagates `RequestContext.request_id` from the response back to the
client (so frontends can echo it in bug reports), and any HTTP-shape
helpers that emerge during chat refactor.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pocketpaw_ee.cloud._core.errors import CloudError


async def cloud_error_handler(request: Request, exc: CloudError) -> JSONResponse:
    """Map a `CloudError` to its JSON envelope.

    Behaviorally identical to the inline handler currently registered in
    `ee/cloud/__init__.py:mount_cloud`. Phase 0 extracts this so the wire
    behavior is unit-testable without booting the whole cloud app.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


def add_error_handler(app: FastAPI) -> None:
    """Register `cloud_error_handler` on `app`. Safe to call more than once;
    the latter call wins (FastAPI overwrites by exception class)."""
    # Starlette types the handler as Callable[[Request, Exception], ...] but
    # narrowing to CloudError is the whole point — suppress the variance.
    app.add_exception_handler(CloudError, cloud_error_handler)  # type: ignore[arg-type]


__all__ = ["add_error_handler", "cloud_error_handler"]
