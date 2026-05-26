"""Shared login helpers — extracted so SSO callback can mint the same
cookie + session row the password login mints. The OIDC callback runs
outside the OAuth2PasswordRequestForm path but lands the user in the
identical session-tracked, cookie-backed state."""

from __future__ import annotations

import jwt as pyjwt
from fastapi import Request, Response

from pocketpaw_ee.cloud.auth import sessions as sessions_service
from pocketpaw_ee.cloud.auth.core import SECRET
from pocketpaw_ee.cloud.models.user import User

_JWT_AUDIENCE = ["fastapi-users:auth"]


def jti_from_token(token: str) -> str | None:
    try:
        payload = pyjwt.decode(
            token,
            SECRET,
            audience=_JWT_AUDIENCE,
            algorithms=["HS256"],
        )
    except pyjwt.PyJWTError:
        return None
    jti = payload.get("jti")
    return jti if isinstance(jti, str) else None


async def mint_and_record(backend, user: User, request: Request) -> Response:
    strategy = backend.get_strategy()
    token = await strategy.write_token(user)
    jti = jti_from_token(token)
    if jti:
        await sessions_service.record_session(str(user.id), jti, request)
    response = await backend.transport.get_login_response(token)

    # Why: pair paw_csrf with paw_auth so the double-submit pair exists
    # the moment the browser is authenticated. Bearer transport never
    # sets paw_auth, so its response carries no paw_csrf either.
    if backend.name == "cookie":
        from pocketpaw_ee.cloud._core.csrf import mint_csrf_token, set_csrf_cookie

        set_csrf_cookie(response, mint_csrf_token())
    return response
