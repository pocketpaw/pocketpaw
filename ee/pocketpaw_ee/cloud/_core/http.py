"""HTTP-layer glue for ee/cloud — exception handler, registration helpers.

In Phase 0 the only function exposed is the `CloudError` → JSON-envelope
mapping that already lives inline at `ee/cloud/__init__.py:58`. We
extract it so it can be imported, unit-tested without booting the full
cloud app, and consistently registered.

Subsequent phases extend this module with: a request-id middleware that
propagates `RequestContext.request_id` from the response back to the
client (so frontends can echo it in bug reports), and any HTTP-shape
helpers that emerge during chat refactor.

Updated 2026-08-24 (fix/cors-headers-on-unhandled-500): also maps
`SmokeGateFailed` — the sites generator's build/install/smoke failure, a plain
`RuntimeError` subclass, NOT a `CloudError`. The service layer deliberately lets
it propagate raw on the preview/arm path so `edit_svelte_component` can roll the
component source back and re-raise; nothing above that re-raise mapped it, so
`POST /sites/by-pocket/{id}/editable` answered a failed build with an opaque 500
(and, being unhandled, a 500 with no CORS headers — the browser then reported a
CORS failure and the real reason never reached anyone). Mapping it HERE, at the
HTTP boundary, leaves every service-layer rollback contract untouched: the
exception still propagates exactly as before, only its final wire shape changes
— to the same `sites.generator_failed` envelope the LIVE publish path already
returns.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from pocketpaw_ee.cloud._core.errors import CloudError, Internal

logger = logging.getLogger(__name__)


async def cloud_error_handler(request: Request, exc: CloudError) -> JSONResponse:
    """Map a `CloudError` to its JSON envelope.

    Behaviorally identical to the inline handler currently registered in
    `ee/cloud/__init__.py:mount_cloud`. Phase 0 extracts this so the wire
    behavior is unit-testable without booting the whole cloud app.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


async def smoke_gate_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map a raw `SmokeGateFailed` to the `sites.generator_failed` envelope.

    Same code/message the live publish path produces via
    `_build_or_cloud_error`, so a failed build reads identically whether it was
    a live publish or an arm-for-editing preview. The cause is logged, never
    leaked: a build log can carry paths and env details.
    """
    logger.error(
        "sites: generator build failed on %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            **Internal(
                "sites.generator_failed",
                "Site generation failed — the publishing toolchain is unavailable "
                "or the build did not complete. See server logs for details.",
            ).to_dict(),
            # friendlyErrorMessage (paw-enterprise) reads `detail`, not `error`.
            "detail": "Site generation failed — the build did not complete. "
            "See server logs for details.",
        },
    )


def add_error_handler(app: FastAPI) -> None:
    """Register `cloud_error_handler` on `app`. Safe to call more than once;
    the latter call wins (FastAPI overwrites by exception class)."""
    # Starlette types the handler as Callable[[Request, Exception], ...] but
    # narrowing to CloudError is the whole point — suppress the variance.
    app.add_exception_handler(CloudError, cloud_error_handler)  # type: ignore[arg-type]

    # Imported lazily: `_core` must not pull the sites package (and its
    # generator toolchain imports) in at module import time. An install without
    # the sites module simply has nothing to map.
    try:
        from pocketpaw_ee.sites.generator_client import SmokeGateFailed
    except Exception:  # noqa: BLE001 - sites layer absent/unimportable
        logger.debug("sites.generator_client unavailable — SmokeGateFailed unmapped")
        return
    app.add_exception_handler(SmokeGateFailed, smoke_gate_handler)


__all__ = ["add_error_handler", "cloud_error_handler", "smoke_gate_handler"]
