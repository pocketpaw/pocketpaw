# Shared FastAPI dependencies for the API layer.
# Created: 2026-02-20
# 2026-04-16: require_scope now fails closed. Master/session/cookie/localhost
# auth must set request.state.full_access = True explicitly — no implicit
# bypass. Closes #888.

from __future__ import annotations

from fastapi import HTTPException, Request

# Testing escape hatch — set to True by tests/v1/conftest.py so that v1
# router-only tests (which mount routers without the dashboard middleware
# that normally sets full_access) can exercise route logic without having
# to install middleware in every fixture. Always False in production.
_TESTING_FULL_ACCESS: bool = False


def require_scope(*scopes: str):
    """FastAPI dependency that checks scopes against the authenticated caller.

    Usage::

        @router.put("/settings", dependencies=[Depends(require_scope("settings:write"))])
        async def update_settings(...): ...

    The check is fail-closed. A request is accepted only when one of the
    following is true:

    * ``request.state.full_access`` is truthy (set by the master token,
      session token, cookie, and localhost auth paths in
      ``dashboard_auth.py`` — explicit "this caller is fully trusted").
    * ``request.state.api_key`` is a key whose scopes include one of the
      required scopes or ``admin``.
    * ``request.state.oauth_token`` carries one of the required scopes or
      ``admin``.

    If none of those hold, the request is rejected with ``403``.
    """

    async def _check(request: Request) -> None:
        # Testing escape hatch (flag is always False in production).
        if _TESTING_FULL_ACCESS:
            return

        # Explicit full-access marker — set by dashboard_auth when master,
        # session, cookie, or localhost auth succeeds.
        if getattr(request.state, "full_access", False):
            return

        required = set(scopes)

        # Check API key scopes
        api_key = getattr(request.state, "api_key", None)
        if api_key is not None:
            key_scopes = set(api_key.scopes)
            if "admin" in key_scopes or key_scopes & required:
                return
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scope: {' or '.join(sorted(required))}",
            )

        # Check OAuth2 token scopes
        oauth_token = getattr(request.state, "oauth_token", None)
        if oauth_token is not None:
            token_scopes = set(oauth_token.scope.split()) if oauth_token.scope else set()
            if "admin" in token_scopes or token_scopes & required:
                return
            raise HTTPException(
                status_code=403,
                detail=f"OAuth token missing required scope: {' or '.join(sorted(required))}",
            )

        # No full-access marker, no API key, no OAuth token — fail closed.
        raise HTTPException(
            status_code=403,
            detail=f"Missing required scope: {' or '.join(sorted(required))}",
        )

    return _check
