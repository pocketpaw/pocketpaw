# runtimes.py — dispatch a brokered browser-side credential by RUNTIME ID (RR-2).
# Created 2026-07-20 (feat/code-runtime-requirements).
#
# WHY THIS EXISTS: the credential broker was written for BrowserPod and named for
# it, but the shape was never BrowserPod-specific. Any runtime that boots INSIDE
# THE USER'S TAB needs a vendor key in that tab, and therefore needs exactly the
# same treatment: the key lives only in server config, is handed out per-request
# to an authenticated workspace-scoped caller, and can be rotated, gated and
# audited centrally instead of being frozen into a frontend build artifact.
# WebContainers is the next such runtime. Keying the broker by runtime id means
# adding one is a registry entry here, not a new endpoint.
#
# WHY AN UNKNOWN RUNTIME IS NOT A 404: the caller of this endpoint is a router
# deciding where to run a project, and it treats "no credential" as "use a
# different runtime". An unconfigured runtime and an unknown one lead to the same
# action, so they get the same answer — ``available: false``. Returning 404 for
# one and 200 for the other would force every client to write error-handling that
# converges on the identical fallback, and would make a typo'd runtime id look
# like an outage.
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pocketpaw_ee.cloud.websandbox import browserpod
from pocketpaw_ee.cloud.websandbox.dto import RuntimeCredentialsResponse

logger = logging.getLogger(__name__)

# runtime id -> the broker that resolves its browser-side credential. Registering
# a runtime here is the whole cost of adding one to the credential surface.
_CREDENTIAL_BROKERS: dict[str, Callable[[str, str], Awaitable[RuntimeCredentialsResponse]]] = {
    "browserpod": browserpod.get_credentials,
}


async def get_runtime_credentials(
    runtime_id: str,
    workspace_id: str,
    user_id: str,
) -> RuntimeCredentialsResponse:
    """Return the browser-side boot credential for ``runtime_id``.

    An unknown runtime id answers ``available: false`` rather than raising — see
    the module header for why that is the honest answer and not a swallowed
    error. It is logged, because a client asking for a runtime that does not
    exist is a real (if benign) symptom of a version skew between the frontend's
    runtime registry and this one.
    """
    broker = _CREDENTIAL_BROKERS.get(runtime_id)
    if broker is None:
        logger.info(
            "websandbox: no credential broker for runtime %r — reporting unavailable "
            "(workspace=%s)",
            runtime_id,
            workspace_id,
        )
        return RuntimeCredentialsResponse(available=False, apiKey=None)
    return await broker(workspace_id, user_id)


__all__ = ["get_runtime_credentials"]
