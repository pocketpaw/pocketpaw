# preview.py — Web Cursor live dev-server preview URL (WC-8/P3b).
# Created 2026-07-16 (feat/code-mode).
#
# Exposes a running dev-server port inside the sandbox VM as a public,
# iframe-embeddable preview URL. Thin service-layer orchestration ABOVE
# ``websandbox/service.py`` (the registry + auth oracle) that resolves + authorizes
# the row exactly like ``provision.get_tree`` / ``edit.propose_edit`` do BEFORE any
# runtime op, then delegates the heavy lifting to
# ``DaytonaClient.get_port_preview_url`` (which returns the public preview URL with
# the access token already appended — directly embeddable in an ``<iframe>``).
#
# SECURITY: tenancy is enforced first — owner-scoped ``get_sandbox`` (NotFound for a
# row the caller doesn't own) + fail-closed ``authorize_sandbox`` on the bound
# Daytona id — BEFORE the preview URL is minted. The port is validated at entry:
# an out-of-range port and the reserved Daytona web-terminal port (22222) are
# refused with a clean ValidationError so a crafted port can neither escape the
# 1..65535 range nor surface the built-in shell as a "preview".
#
# DI seam: ``client: DaytonaClient | None = None`` (default ``get_daytona_client()``)
# so tests inject a fake and never hit real Daytona.
from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, ValidationError
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.dto import PreviewResponse

logger = logging.getLogger(__name__)

# Valid TCP port range for a previewable dev server.
_MIN_PORT = 1
_MAX_PORT = 65535

# Daytona's built-in web terminal binds this port (see
# ``DaytonaClient.get_web_terminal_url``). Exposing it as a "preview" would surface
# a second interactive shell inside an iframe, so it is never previewable.
_RESERVED_TERMINAL_PORT = 22222


def _require_client(client: DaytonaClient | None) -> DaytonaClient:
    """Resolve the Daytona client, raising a clean CloudError when unconfigured
    (mirrors ``provision._require_client`` — a None client is a 503, not a crash)."""
    resolved = client if client is not None else get_daytona_client()
    if resolved is None:
        raise CloudError(
            503,
            "websandbox.daytona_unavailable",
            "The sandbox runtime is not configured",
        )
    return resolved


def _validate_port(port: int) -> int:
    """Validate ``port`` is an in-range, non-reserved TCP port; return it.

    ``bool`` is rejected explicitly (``True``/``False`` are ``int`` subclasses that
    would otherwise slip through as 1/0). Out-of-range and the reserved terminal
    port both raise a clean 422 ValidationError before any VM op.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValidationError("websandbox.invalid_port", "A numeric 'port' is required")
    if port < _MIN_PORT or port > _MAX_PORT:
        raise ValidationError(
            "websandbox.invalid_port",
            f"Port must be between {_MIN_PORT} and {_MAX_PORT}",
        )
    if port == _RESERVED_TERMINAL_PORT:
        raise ValidationError(
            "websandbox.reserved_port",
            f"Port {_RESERVED_TERMINAL_PORT} is reserved for the sandbox terminal",
        )
    return port


async def get_preview(
    workspace_id: str,
    user_id: str,
    row_id: str,
    port: int,
    *,
    client: DaytonaClient | None = None,
) -> PreviewResponse:
    """Resolve the iframe-embeddable preview URL for a dev-server ``port`` in a
    ready sandbox.

    Flow: validate the port (out-of-range / reserved refused at entry) → resolve
    the row owner-scoped (``get_sandbox`` → NotFound for a row the caller doesn't
    own) → 409 if it never bound a Daytona id → fail-closed ``authorize_sandbox``
    BEFORE any VM touch → ``get_port_preview_url`` → return ``{url, port}``.
    """
    port = _validate_port(port)
    daytona = _require_client(client)

    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")

    # Fail-closed authorization on the Daytona id BEFORE touching the runtime.
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    url = await daytona.get_port_preview_url(row.sandbox_id, port)
    return PreviewResponse(url=url, port=port)


__all__ = ["get_preview"]
