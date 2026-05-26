"""RequestContext: typed per-request envelope.

Routers obtain a `RequestContext` via `Depends(request_context)`.
Services accept it as their first positional argument and pass parts of
it to repositories (typically `workspace_id`). This replaces the ad-hoc
`current_user_id` / `current_workspace_id` dependencies in
`shared/deps.py` over the course of the strangler migration; both styles
coexist during the transition.

Updates:
  - 2026-05-26: Added ``loopback_or_request_context`` — a JWT-or-loopback
    dependency for endpoints called by the local chat agent on the
    workspace's own dashboard. Validates that the caller is on a
    loopback address AND presents the trio of ``X-PocketPaw-Internal``,
    ``X-PocketPaw-Workspace-Id``, ``X-PocketPaw-User-Id`` headers; falls
    back to the standard JWT auth path when any check fails. Used by the
    foresight router; the dev-grade bypass tightens to a short-lived
    signed JWT in a follow-up PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Request

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.auth import current_active_user, current_optional_user
from pocketpaw_ee.cloud.models.user import User


class ScopeKind(StrEnum):
    """The kind of scope a request operates within.

    `StrEnum` lets the value be used directly in URL templating and JSON
    serialization without explicit conversion.
    """

    WORKSPACE = "workspace"
    SESSION = "session"
    POCKET = "pocket"
    GROUP = "group"
    DM = "dm"
    NONE = "none"


@dataclass(frozen=True)
class RequestContext:
    """Typed per-request envelope passed router → service → repository.

    Fields:
        user_id: ID of the authenticated user. Never empty in authed routes.
        workspace_id: Active workspace ID. None when the user has no
            active workspace OR the route is workspace-agnostic. Routes that
            REQUIRE a workspace must check explicitly and raise
            `WorkspaceNotFound` (or similar) if missing.
        request_id: For log correlation. Sourced from the `x-request-id`
            header if present, otherwise a fresh hex UUID.
        scope: The scope kind this endpoint operates within. Default
            `NONE`; per-route overrides set this when relevant.
        started_at: Timezone-aware UTC timestamp recorded at dependency
            resolution time. Used by the timing middleware and downstream
            log correlation.
    """

    user_id: str
    workspace_id: str | None
    request_id: str
    scope: ScopeKind
    started_at: datetime


async def request_context(
    request: Request,
    user: Annotated[User, Depends(current_active_user)],
) -> RequestContext:
    """Build a `RequestContext` from the authenticated user.

    Per-route scope overrides are out of scope for Phase 0; consumers
    needing a non-`NONE` scope should construct their own context derived
    from this one until a per-scope dep ships in a later phase.
    """
    request_id = request.headers.get("x-request-id") or uuid4().hex
    return RequestContext(
        user_id=str(user.id),
        workspace_id=user.active_workspace,
        request_id=request_id,
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Loopback-internal bypass (RFC 08 v1.0 wave 4)
# ---------------------------------------------------------------------------

INTERNAL_HEADER: str = "X-PocketPaw-Internal"
WORKSPACE_HEADER: str = "X-PocketPaw-Workspace-Id"
USER_HEADER: str = "X-PocketPaw-User-Id"

# Hostnames / IPs the bypass trusts. The dashboard always binds to one of
# these; outbound traffic from the chat agent inherits the same host. A
# real attacker either has to forge ``X-Forwarded-For`` upstream of the
# ASGI server (uvicorn doesn't honor the header by default) or land code
# execution on the host, in which case the bypass isn't the weakest link.
_LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        # IPv4-mapped IPv6 loopback. ASGI servers normalise to ``::ffff:127.0.0.1``
        # on some platforms (Linux dual-stack); accept both forms.
        "::ffff:127.0.0.1",
    }
)


def _is_loopback_client(request: Request) -> bool:
    """Return True if the request originated from a loopback address.

    Starlette exposes the connecting peer as ``request.client``; a tuple
    of ``(host, port)`` or ``None`` for tests that didn't supply a
    transport. ``None`` is treated as untrusted — failing closed is the
    right default for an auth bypass.
    """
    if request.client is None:
        return False
    return request.client.host in _LOOPBACK_HOSTS


def _try_loopback_context(request: Request) -> RequestContext | None:
    """Build a context from loopback-internal headers, or return None.

    Returns ``None`` (rather than raising) when any check fails so the
    caller can fall back to the standard JWT path. All three headers
    must be present AND the request must originate from a loopback
    address; missing any one drops back to JWT auth.

    The bypass is **dev-grade** per RFC 08 v1.0 wave 4 — the next pass
    swaps the static header trio for a short-lived signed JWT that the
    dashboard mints and the chat agent presents. The signature gate
    closes the loophole where a chained process on the same host could
    forge the headers; until then, the loopback host check is the only
    boundary between the local chat agent and full workspace access.
    """
    if request.headers.get(INTERNAL_HEADER, "").strip().lower() != "true":
        return None

    if not _is_loopback_client(request):
        return None

    workspace_id = request.headers.get(WORKSPACE_HEADER, "").strip()
    user_id = request.headers.get(USER_HEADER, "").strip()

    # Both header values must be present — a missing user id would let a
    # caller act as the workspace without a user, and a missing
    # workspace id collapses tenancy. Either case is rejected.
    if not workspace_id or not user_id:
        return None

    request_id = request.headers.get("x-request-id") or uuid4().hex
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id=request_id,
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def loopback_or_request_context(
    request: Request,
    user: Annotated[User | None, Depends(current_optional_user)] = None,
) -> RequestContext:
    """``request_context`` with a loopback-internal-header fallback.

    The dependency resolves to a :class:`RequestContext` via one of two
    paths:

    1. **Loopback bypass.** Request originates from a loopback address
       AND carries the trio of ``X-PocketPaw-Internal: true``,
       ``X-PocketPaw-Workspace-Id``, ``X-PocketPaw-User-Id``. The
       context is built from the headers; no JWT is consulted. This
       path is what the local chat agent uses when it drives Foresight
       from the dashboard.

    2. **JWT auth.** Falls back to the standard fastapi-users flow via
       :func:`current_optional_user`. A missing or invalid token
       collapses to a 403 ``auth.required`` error.

    The bypass is opt-in per endpoint: only endpoints that swap their
    ``Depends(request_context)`` for ``Depends(loopback_or_request_context)``
    get the loopback path. All other routes keep the strict JWT-only
    behavior — no global trust expansion.

    Returns:
        A :class:`RequestContext` populated from either the loopback
        headers or the resolved user.

    Raises:
        Forbidden: when the loopback path fails AND no valid JWT is
            present. The error carries ``code='auth.required'`` to
            disambiguate from the 401 fastapi-users would emit for a
            malformed token.
    """
    loopback_ctx = _try_loopback_context(request)
    if loopback_ctx is not None:
        return loopback_ctx

    if user is None:
        raise Forbidden("auth.required", "Authentication required")

    request_id = request.headers.get("x-request-id") or uuid4().hex
    return RequestContext(
        user_id=str(user.id),
        workspace_id=user.active_workspace,
        request_id=request_id,
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


__all__ = [
    "INTERNAL_HEADER",
    "RequestContext",
    "ScopeKind",
    "USER_HEADER",
    "WORKSPACE_HEADER",
    "loopback_or_request_context",
    "request_context",
]
