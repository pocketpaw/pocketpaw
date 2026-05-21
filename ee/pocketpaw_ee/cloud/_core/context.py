"""RequestContext: typed per-request envelope.

Routers obtain a `RequestContext` via `Depends(request_context)`.
Services accept it as their first positional argument and pass parts of
it to repositories (typically `workspace_id`). This replaces the ad-hoc
`current_user_id` / `current_workspace_id` dependencies in
`shared/deps.py` over the course of the strangler migration; both styles
coexist during the transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Request

from pocketpaw_ee.cloud.auth import current_active_user
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


__all__ = ["RequestContext", "ScopeKind", "request_context"]
