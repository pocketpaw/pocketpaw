"""FastAPI dependency for the platform authority axis.

Created: 2026-09-14 (feat/platform-authority-axis) — chunk 1 of the Paw Admin PRD.

``require_platform(action)`` is the guard every route under
``/api/v1/platform`` must carry. It is deliberately NOT built on
``_core.deps.require_action``: that path resolves the caller's workspace and
their membership in it, and a platform route has no workspace of its own. Every
platform route takes its target as an explicit path parameter instead, which is
the exact inversion of this codebase's strongest tenancy invariant ("workspace
is derived from the session, never accepted from the caller").

That inversion is why the prefix exists. A reviewer greps one prefix to find
every route that legitimately accepts a caller-supplied workspace, and any route
OUTSIDE the prefix that does so is unambiguously a bug. ``test_platform_guard``
asserts the first half of that: every route mounted under the platform prefix
carries this dependency.

Nothing here consults workspace membership, and nothing here reads
``is_superuser``. The two axes stay separate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException, Request

from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.guards.audit import log_denial
from pocketpaw_ee.guards.platform import check_platform_action, get_platform_rule
from pocketpaw_ee.guards.rbac import Forbidden

logger = logging.getLogger(__name__)

_PlatformDep = Callable[..., Coroutine[Any, Any, User]]

# The cookie the console authenticates with. Set by the fastapi-users cookie
# transport in auth/core.py.
_SESSION_COOKIE = "paw_auth"


def _require_interactive_principal(request: Request, action: str, user: User) -> None:
    """Refuse any caller that is not a human at an interactive console.

    THE THREAT THIS EXISTS FOR. The rung check asks "does this user hold
    operator?". It cannot ask "is this request the user's own intent?" — and on
    this platform those are different questions, because the user runs AI agents.

    An operator is also an ordinary PocketPaw user. They chat with agents, and
    those agents read tenant-supplied content: documents, site copy, support
    threads, connector payloads. A prompt injection in any of it can steer an
    agent into making requests. If that agent carries the operator's credential,
    every check in this module passes, because the caller genuinely IS an
    operator. The blast radius is every tenant's data and every tenant's wallet.

    So the platform axis additionally requires that the credential be one an
    automated caller cannot hold:

      - ``request.state.api_key`` / ``oauth_token`` — a long-lived bearer token.
        API keys are minted per workspace and (as of this writing) their CRUD is
        guarded by bare membership, so any workspace member can mint one. A key
        minted by an operator must never inherit that operator's platform rung.
        These principals are refused outright.
      - ``Authorization: Bearer`` — the back-compat JWT transport, used by
        native clients and scripts. It is the credential an agent is most likely
        to be handed, and it is copyable in a way a cookie is not. The platform
        axis accepts the session COOKIE only. The operator console forwards that
        cookie server-side, so this costs the console nothing; it costs a script
        the ability to act as an operator, which is the point.

    WHAT THIS DOES NOT COVER, and must be stated plainly: an agent that calls
    service functions IN-PROCESS never passes through a route, so no dependency
    can see it. Nothing here protects ``credits.service.grant`` from being
    called directly. The rule for every later chunk is that platform mutations
    live behind the platform router and are never re-exported as an agent tool
    or a service an agent surface can reach. Guarding the route is necessary and
    is not sufficient.
    """
    state = request.state

    for marker in ("api_key", "oauth_token"):
        if getattr(state, marker, None):
            log_denial(
                actor=str(user.id),
                action=action,
                code="platform.non_interactive_principal",
                detail=f"Refused a {marker} principal on the platform axis.",
                platform_role=user.platform_role or "none",
                path=request.url.path,
            )
            raise HTTPException(status_code=403, detail="platform.non_interactive_principal")

    # A bearer token, even a valid one for a real operator, is refused. Note the
    # cookie must ALSO be present: a request carrying both is ambiguous about
    # which credential authenticated it, and the safe reading is the weaker one.
    if request.headers.get("authorization"):
        log_denial(
            actor=str(user.id),
            action=action,
            code="platform.non_interactive_principal",
            detail="Refused a bearer credential on the platform axis.",
            platform_role=user.platform_role or "none",
            path=request.url.path,
        )
        raise HTTPException(status_code=403, detail="platform.non_interactive_principal")

    if not request.cookies.get(_SESSION_COOKIE):
        log_denial(
            actor=str(user.id),
            action=action,
            code="platform.non_interactive_principal",
            detail="Platform routes require an interactive session cookie.",
            platform_role=user.platform_role or "none",
            path=request.url.path,
        )
        raise HTTPException(status_code=403, detail="platform.non_interactive_principal")


def require_platform(action: str) -> _PlatformDep:
    """Dependency factory: allow only callers whose platform rung satisfies ``action``.

    Returns the ``User`` so a handler can depend on this once and still have the
    operator to hand for the audit record, rather than depending on the guard
    and the user separately and risking the two drifting apart.

    Usage::

        @router.get("/workspaces")
        async def list_workspaces(
            operator: User = Depends(require_platform("platform.workspace.read")),
        ) -> list[WorkspaceOut]: ...

    Raises 403 with the rule's stable ``code`` as ``detail``, matching how
    ``guards.deps`` reports a workspace denial, so the frontend keys off one
    shape for both axes.
    """
    # Resolve at decoration time, not per request. An action string that is not
    # in PLATFORM_ACTIONS raises here — at import, when the app is built — so a
    # typo takes the process down at startup instead of silently guarding a
    # money route with a rule that does not exist.
    rule = get_platform_rule(action)

    async def _guard(
        request: Request,
        user: User = Depends(current_active_user),
    ) -> User:
        # Principal check BEFORE the rung check, because the dangerous case here
        # is not "wrong rung" — it is "right rung, wrong caller".
        _require_interactive_principal(request, action, user)

        try:
            check_platform_action(action, user.platform_role)
        except Forbidden as exc:
            # Denials on this axis are worth recording every time. A denied
            # cross-tenant read is a materially more interesting event than a
            # denied workspace read, because the caller had to reach a platform
            # route to generate one.
            log_denial(
                actor=str(user.id),
                action=action,
                code=exc.code,
                detail=exc.detail,
                platform_role=user.platform_role or "none",
                path=request.url.path,
            )
            raise HTTPException(status_code=403, detail=exc.code) from exc

        return user

    # Surfaced for the route-coverage test, which needs to tell a platform guard
    # apart from any other dependency without calling it.
    _guard.__platform_action__ = action  # type: ignore[attr-defined]
    _guard.__platform_minimum__ = rule.minimum  # type: ignore[attr-defined]

    return _guard


__all__ = ["require_platform"]
