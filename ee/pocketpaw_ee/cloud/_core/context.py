"""RequestContext: typed per-request envelope.

Routers obtain a `RequestContext` via `Depends(request_context)`.
Services accept it as their first positional argument and pass parts of
it to repositories (typically `workspace_id`). This replaces the ad-hoc
`current_user_id` / `current_workspace_id` dependencies in
`shared/deps.py` over the course of the strangler migration; both styles
coexist during the transition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, HTTPException, Request

from pocketpaw_ee.cloud.auth import current_optional_user
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
        scopes: ``None`` for JWT/cookie auth (full access). A concrete
            list when the caller authed with an API key — ``require_scope``
            checks against this list.
    """

    user_id: str
    workspace_id: str | None
    request_id: str
    scope: ScopeKind
    started_at: datetime
    scopes: list[str] | None = field(default=None)


def _bearer_token(request: Request) -> str | None:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return None


async def request_context(
    request: Request,
    user: Annotated[User | None, Depends(current_optional_user)] = None,
) -> RequestContext:
    """Build a `RequestContext` from the authenticated user or API key.

    Branches:
      - ``Authorization: Bearer paw_<...>`` → resolve via API-key service.
      - Otherwise → resolve via the existing fastapi-users JWT/cookie dep.
    """
    request_id = request.headers.get("x-request-id") or uuid4().hex
    bearer = _bearer_token(request)
    if bearer and bearer.startswith("paw_"):
        from pocketpaw_ee.cloud.auth import api_keys as api_keys_service

        resolved = await api_keys_service.resolve_bearer(bearer)
        if resolved is None:
            raise HTTPException(status_code=401, detail="invalid_api_key")
        user_id, workspace_id, scopes = resolved
        await _maybe_emit_api_key_use(bearer, workspace_id, user_id)
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id=request_id,
            scope=ScopeKind.NONE,
            started_at=datetime.now(UTC),
            scopes=scopes,
        )

    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return RequestContext(
        user_id=str(user.id),
        workspace_id=user.active_workspace,
        request_id=request_id,
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
        scopes=None,
    )


_API_KEY_AUDIT_INTERVAL = 60.0
_API_KEY_AUDIT_LRU_MAX = 1000
_api_key_audit_writes: dict[str, float] = {}


async def _maybe_emit_api_key_use(token: str, workspace_id: str, user_id: str) -> None:
    import time as _time

    body = token[4:]
    prefix = body[:8]
    now = _time.monotonic()
    prev = _api_key_audit_writes.get(prefix)
    if prev is not None and now - prev < _API_KEY_AUDIT_INTERVAL:
        return
    _api_key_audit_writes[prefix] = now
    if len(_api_key_audit_writes) > _API_KEY_AUDIT_LRU_MAX:
        oldest = sorted(_api_key_audit_writes.items(), key=lambda kv: kv[1])[
            : len(_api_key_audit_writes) - _API_KEY_AUDIT_LRU_MAX
        ]
        for k, _ in oldest:
            _api_key_audit_writes.pop(k, None)

    try:
        from pocketpaw_ee.cloud.audit import service as audit_service

        await audit_service.record(
            workspace_id,
            user_id,
            "api_key.use",
            target_type="api_key",
            target_id=prefix,
        )
    except Exception:  # noqa: BLE001
        pass


def require_scope(scope: str):
    """Build a FastAPI dep that enforces a scope on the resolved context.

    JWT-authed callers (``ctx.scopes is None``) always pass — they hold
    full session credentials. API-key callers must have the scope present.
    """

    async def _dep(ctx: Annotated[RequestContext, Depends(request_context)]) -> RequestContext:
        if ctx.scopes is None:
            return ctx
        if scope not in ctx.scopes:
            raise HTTPException(status_code=403, detail=f"missing_scope:{scope}")
        return ctx

    return _dep


__all__ = ["RequestContext", "ScopeKind", "request_context", "require_scope"]
