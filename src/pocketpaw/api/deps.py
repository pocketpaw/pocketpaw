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
        api_key = getattr(request.state, "api_key", None)
        if api_key is None:
            # Not an API key auth — master/session/cookie/localhost have full access
            return

        key_scopes = set(api_key.scopes)
        # "admin" scope grants access to everything
        if "admin" in key_scopes:
            return

        required = set(scopes)
        if not key_scopes & required:
            raise HTTPException(
                status_code=403,
                detail=f"API key missing required scope: {' or '.join(sorted(required))}",
            )

    return _check
