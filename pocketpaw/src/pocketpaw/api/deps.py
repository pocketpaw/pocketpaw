# Shared FastAPI dependencies for the API layer.
# Created: 2026-02-20

from __future__ import annotations

from fastapi import HTTPException, Request


def require_scope(*scopes: str):
    """FastAPI dependency that checks API key scopes.

    Usage::

        @router.put("/settings", dependencies=[Depends(require_scope("settings:write"))])
        async def update_settings(...): ...

    If the request was authenticated via API key, verifies the key has at least
    one of the required scopes. Master token, session token, cookie, and
    localhost auth bypass scope checks (they have full access).
    """

    async def _check(request: Request) -> None:
        # Check API key scopes
        api_key = getattr(request.state, "api_key", None)
        if api_key is not None:
            key_scopes = set(api_key.scopes)
            if "admin" in key_scopes:
                return
            required = set(scopes)
            if not key_scopes & required:
                raise HTTPException(
                    status_code=403,
                    detail=f"API key missing required scope: {' or '.join(sorted(required))}",
                )
            return

        # Check OAuth2 token scopes
        oauth_token = getattr(request.state, "oauth_token", None)
        if oauth_token is not None:
            token_scopes = set(oauth_token.scope.split()) if oauth_token.scope else set()
            if "admin" in token_scopes:
                return
            required = set(scopes)
            if not token_scopes & required:
                raise HTTPException(
                    status_code=403,
                    detail=f"OAuth token missing required scope: {' or '.join(sorted(required))}",
                )
            return

        # Not an API key or OAuth auth — master/session/cookie/localhost have full access

    return _check
