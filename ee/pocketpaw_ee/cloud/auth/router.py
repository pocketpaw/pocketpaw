"""Auth domain — FastAPI router.

Profile endpoints use ``Depends(request_context)`` and call into
``ee.cloud.auth.service`` module functions directly. The fastapi-users
sub-routers (login/logout/register) and the avatar file-serving / upload
endpoints stay here unchanged in behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt as pyjwt
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users.router.common import ErrorCode
from pydantic import BaseModel

from pocketpaw.security.rate_limiter import mfa_challenge_limiter
from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.auth import mfa as mfa_service
from pocketpaw_ee.cloud.auth import service as auth_service
from pocketpaw_ee.cloud.auth import sessions as sessions_service
from pocketpaw_ee.cloud.auth._login_helpers import mint_and_record as _mint_and_record
from pocketpaw_ee.cloud.auth.api_keys_router import router as api_keys_router
from pocketpaw_ee.cloud.auth.core import (
    UserCreate,
    UserManager,
    UserRead,
    bearer_backend,
    cookie_backend,
    current_active_user,
    fastapi_users,
    get_user_manager,
)
from pocketpaw_ee.cloud.auth.dto import (
    ProfileOut,
    ProfileUpdateRequest,
    SetWorkspaceRequest,
    auth_user_to_profile_out,
)
from pocketpaw_ee.cloud.auth.mfa_tokens import mint_mfa_pending, verify_mfa_pending
from pocketpaw_ee.cloud.auth.sessions_dto import RevokeOthersResponse, SessionOut
from pocketpaw_ee.cloud.auth.ws_tickets import mint_ws_ticket

router = APIRouter(tags=["Auth"])

# Avatar storage — local filesystem for now (could swap for S3/R2 later)
_AVATAR_DIR = Path.home() / ".pocketpaw" / "avatars"
_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
_ALLOWED_AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# MFA-gated login (Wave 3 Task 4)
#
# These two routes MUST be registered before the fastapi-users auth
# sub-routers below — FastAPI matches the first route registered for a
# path, so this is how we override the stock fastapi-users /auth/login
# and /auth/bearer/login without forking the upstream router.
#
# Flow: authenticate via the user manager; if mfa_enabled, return a
# short-lived `mfa_pending` JWT instead of minting the session cookie /
# bearer token. The client then calls /auth/mfa/challenge with the
# pending token + TOTP/backup code.
#
# Both transports stay live during the cookie+CSRF rollout (security
# #1117 P1). Cookie is the long-term path for browser clients; Bearer
# is retained for back-compat with the Tauri desktop client and any
# automation / MCP tools that hold a token directly.
# ---------------------------------------------------------------------------


def _bad_credentials() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=ErrorCode.LOGIN_BAD_CREDENTIALS,
    )


async def _authenticate_or_400(
    credentials: OAuth2PasswordRequestForm,
    manager: UserManager,
) -> Any:
    user = await manager.authenticate(credentials)
    if user is None or not user.is_active:
        raise _bad_credentials()
    return user


def _current_jti(request: Request) -> str | None:
    token = request.cookies.get("paw_auth")
    if not token:
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            token = authz[7:].strip()
    if not token:
        return None
    try:
        payload = pyjwt.decode(token, options={"verify_signature": False})
    except pyjwt.PyJWTError:
        return None
    jti = payload.get("jti")
    return jti if isinstance(jti, str) else None


@router.post("/auth/login", name="auth:cookie.login.mfa-gated")
async def login_cookie(
    request: Request,
    credentials: OAuth2PasswordRequestForm = Depends(),
    manager: UserManager = Depends(get_user_manager),
):
    user = await _authenticate_or_400(credentials, manager)
    if user.mfa_enabled:
        token, _ = mint_mfa_pending(str(user.id))
        return JSONResponse({"mfa_required": True, "mfa_token": token})

    response = await _mint_and_record(cookie_backend, user, request)
    await manager.on_after_login(user, request, response)
    return response


@router.post("/auth/bearer/login", name="auth:bearer.login.mfa-gated")
async def login_bearer(
    request: Request,
    credentials: OAuth2PasswordRequestForm = Depends(),
    manager: UserManager = Depends(get_user_manager),
):
    user = await _authenticate_or_400(credentials, manager)
    if user.mfa_enabled:
        token, _ = mint_mfa_pending(str(user.id))
        return JSONResponse({"mfa_required": True, "mfa_token": token})

    response = await _mint_and_record(bearer_backend, user, request)
    await manager.on_after_login(user, request, response)
    return response


class _MfaChallengeRequest(BaseModel):
    mfa_token: str
    code: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/auth/mfa/challenge")
async def mfa_challenge(
    body: _MfaChallengeRequest,
    request: Request,
    manager: UserManager = Depends(get_user_manager),
):
    decoded = verify_mfa_pending(body.mfa_token)
    if decoded is None:
        raise HTTPException(status_code=401, detail="invalid_mfa_token")
    user_id, jti = decoded

    limiter_key = f"{_client_ip(request)}|{jti}"
    if not mfa_challenge_limiter.allow(limiter_key):
        raise HTTPException(status_code=429, detail="mfa_too_many_attempts")

    try:
        user = await manager.get(PydanticObjectId(user_id))
    except Exception as exc:  # noqa: BLE001 — UserNotExists/InvalidID both surface as 401
        raise HTTPException(status_code=401, detail="invalid_mfa_token") from exc
    if not user.is_active or not user.mfa_enabled:
        raise HTTPException(status_code=401, detail="invalid_mfa_token")

    ok = False
    if user.mfa_totp_secret and mfa_service.verify_totp(user.mfa_totp_secret, body.code):
        ok = True
    elif mfa_service.consume_backup_code(user, body.code):
        await user.save()
        ok = True

    if not ok:
        raise HTTPException(status_code=401, detail="mfa_invalid_code")

    response = await _mint_and_record(cookie_backend, user, request)
    await manager.on_after_login(user, request, response)
    return response


# ---------------------------------------------------------------------------
# WebSocket ticket — short-lived single-use JWT the SPA uses to authenticate
# the /ws/cloud upgrade. The browser can't ride the HttpOnly paw_auth
# cookie onto a cross-origin WS handshake (SameSite=Lax excludes script-
# initiated cross-origin WS upgrades), so the SPA pays one REST round-trip
# to swap its cookie session for a 30-second ticket consumed at upgrade.
# ---------------------------------------------------------------------------


@router.post("/auth/ws/ticket")
async def issue_ws_ticket(user: Any = Depends(current_active_user)) -> dict:
    ticket = await mint_ws_ticket(str(user.id))
    return {"ticket": ticket}


# ---------------------------------------------------------------------------
# Logout — flips session.revoked + adds jti to Redis set, then clears the
# cookie / bearer 204 response. The fastapi-users sub-router's /auth/logout
# is shadowed by these because they're registered first.
# ---------------------------------------------------------------------------


async def _revoke_current(request: Request, user: Any) -> None:
    jti = _current_jti(request)
    if not jti:
        return
    try:
        await sessions_service.revoke_session(str(user.id), jti, by_user_id=str(user.id))
    except Exception:  # noqa: BLE001 — already-revoked / missing row is non-fatal
        pass


@router.post("/auth/logout", name="auth:cookie.logout.revoke")
async def logout_cookie(
    request: Request,
    user: Any = Depends(current_active_user),
):
    from pocketpaw_ee.cloud._core.csrf import clear_csrf_cookie

    await _revoke_current(request, user)
    response = await cookie_backend.transport.get_logout_response()
    clear_csrf_cookie(response)
    return response


@router.post("/auth/bearer/logout", name="auth:bearer.logout.revoke")
async def logout_bearer(
    request: Request,
    user: Any = Depends(current_active_user),
):
    from pocketpaw_ee.cloud._core.csrf import clear_csrf_cookie

    await _revoke_current(request, user)
    response = Response(status_code=204)
    clear_csrf_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Session list + revoke endpoints (Wave 3 Task 6)
# ---------------------------------------------------------------------------


def _session_to_out(doc, *, current_jti: str | None) -> SessionOut:
    return SessionOut(
        id=str(doc.id),
        jti=doc.jti,
        ip=doc.ip,
        device_label=doc.device_label,
        issued_at=doc.issued_at.isoformat() if doc.issued_at else None,
        last_seen_at=doc.last_seen_at.isoformat() if doc.last_seen_at else None,
        is_current=(doc.jti == current_jti),
    )


@router.get("/auth/sessions", response_model=list[SessionOut])
async def list_my_sessions(
    request: Request,
    user: Any = Depends(current_active_user),
) -> list[SessionOut]:
    rows = await sessions_service.list_sessions(str(user.id))
    cur = _current_jti(request)
    return [_session_to_out(r, current_jti=cur) for r in rows]


@router.delete("/auth/sessions/{jti}")
async def revoke_my_session(
    jti: str,
    user: Any = Depends(current_active_user),
) -> dict:
    await sessions_service.revoke_session(str(user.id), jti, by_user_id=str(user.id))
    return {"ok": True}


@router.post("/auth/sessions/revoke-others", response_model=RevokeOthersResponse)
async def revoke_other_sessions(
    request: Request,
    user: Any = Depends(current_active_user),
) -> RevokeOthersResponse:
    cur = _current_jti(request) or ""
    n = await sessions_service.revoke_all_others(str(user.id), cur)
    return RevokeOthersResponse(revoked=n)


# ---------------------------------------------------------------------------
# fastapi-users sub-routers (logout/register). The login + logout routes
# are overridden above; register still comes from upstream.
# ---------------------------------------------------------------------------

router.include_router(
    fastapi_users.get_auth_router(cookie_backend),
    prefix="/auth",
)
router.include_router(
    fastapi_users.get_auth_router(bearer_backend),
    prefix="/auth/bearer",
)
router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
)
router.include_router(
    fastapi_users.get_reset_password_router(),
    prefix="/auth",
)
router.include_router(
    fastapi_users.get_verify_router(UserRead),
    prefix="/auth",
)

router.include_router(api_keys_router)

from pocketpaw_ee.cloud.auth.sso import router as sso_router  # noqa: E402

router.include_router(sso_router)


# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------


@router.get("/auth/me", response_model=ProfileOut)
async def get_me(
    ctx: RequestContext = Depends(request_context),
) -> ProfileOut:
    user = await auth_service.get_profile(ctx)
    return auth_user_to_profile_out(user)


@router.patch("/auth/me", response_model=ProfileOut)
async def update_me(
    body: ProfileUpdateRequest,
    ctx: RequestContext = Depends(request_context),
) -> ProfileOut:
    user = await auth_service.update_profile(
        ctx,
        full_name=body.full_name,
        avatar=body.avatar,
        status=body.status,
    )
    return auth_user_to_profile_out(user)


@router.post("/auth/set-active-workspace")
async def set_active_workspace(
    body: SetWorkspaceRequest,
    ctx: RequestContext = Depends(request_context),
) -> dict:
    await auth_service.set_active_workspace(ctx, body.workspace_id)
    return {"ok": True, "activeWorkspace": body.workspace_id}


# ---------------------------------------------------------------------------
# MFA / TOTP enrollment (Wave 3 Task 3)
# ---------------------------------------------------------------------------


class _MfaVerifyRequest(BaseModel):
    code: str


class _MfaDisableRequest(BaseModel):
    password: str
    code: str


async def _audit_mfa(workspace: str | None, user_id: str, action: str) -> None:
    await audit_service.record(
        workspace or "system",
        user_id,
        action,
        target_type="user",
        target_id=user_id,
    )


@router.post("/auth/mfa/setup")
async def mfa_setup(
    user: Any = Depends(current_active_user),
) -> dict:
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="mfa_already_enabled")

    secret = mfa_service.generate_secret()
    user.mfa_totp_secret = secret
    user.mfa_pending_setup = True
    await user.save()

    otpauth_url = mfa_service.build_otpauth_url(secret, user.email)
    qr_svg = mfa_service.build_qr_svg(otpauth_url)
    return {"secret": secret, "otpauth_url": otpauth_url, "qr_svg": qr_svg}


@router.post("/auth/mfa/verify")
async def mfa_verify(
    body: _MfaVerifyRequest,
    user: Any = Depends(current_active_user),
) -> dict:
    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="mfa_already_enabled")
    if not user.mfa_pending_setup or not user.mfa_totp_secret:
        raise HTTPException(status_code=400, detail="mfa_setup_not_started")
    if not mfa_service.verify_totp(user.mfa_totp_secret, body.code):
        raise HTTPException(status_code=400, detail="mfa_invalid_code")

    plaintext, hashed = mfa_service.generate_backup_codes()
    user.mfa_enabled = True
    user.mfa_pending_setup = False
    user.mfa_verified_at = datetime.now(UTC)
    user.mfa_backup_codes = hashed
    await user.save()

    await _audit_mfa(user.active_workspace, str(user.id), "mfa.enable")
    return {"enabled": True, "backup_codes": plaintext}


@router.post("/auth/mfa/disable")
async def mfa_disable(
    body: _MfaDisableRequest,
    user: Any = Depends(current_active_user),
    manager: UserManager = Depends(get_user_manager),
) -> dict:
    if not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="mfa_not_enabled")

    verified, _ = manager.password_helper.verify_and_update(body.password, user.hashed_password)
    if not verified:
        raise HTTPException(status_code=400, detail="mfa_invalid_password")
    if not mfa_service.verify_totp(user.mfa_totp_secret or "", body.code):
        raise HTTPException(status_code=400, detail="mfa_invalid_code")

    user.mfa_enabled = False
    user.mfa_pending_setup = False
    user.mfa_totp_secret = None
    user.mfa_backup_codes = []
    user.mfa_verified_at = None
    await user.save()

    await _audit_mfa(user.active_workspace, str(user.id), "mfa.disable")
    return {"enabled": False}


@router.post("/auth/mfa/backup-codes/regenerate")
async def mfa_regenerate_backup_codes(
    body: _MfaDisableRequest,
    user: Any = Depends(current_active_user),
    manager: UserManager = Depends(get_user_manager),
) -> dict:
    if not user.mfa_enabled:
        raise HTTPException(status_code=400, detail="mfa_not_enabled")

    verified, _ = manager.password_helper.verify_and_update(body.password, user.hashed_password)
    if not verified:
        raise HTTPException(status_code=400, detail="mfa_invalid_password")
    if not mfa_service.verify_totp(user.mfa_totp_secret or "", body.code):
        raise HTTPException(status_code=400, detail="mfa_invalid_code")

    plaintext, hashed = mfa_service.generate_backup_codes()
    user.mfa_backup_codes = hashed
    await user.save()

    await _audit_mfa(user.active_workspace, str(user.id), "mfa.backup_codes.regenerate")
    return {"backup_codes": plaintext}


# ---------------------------------------------------------------------------
# Avatar upload + serve — file I/O stays here; persistence via service
# ---------------------------------------------------------------------------


@router.post("/auth/avatar", response_model=ProfileOut)
async def upload_avatar(
    file: UploadFile = File(...),
    user: Any = Depends(current_active_user),
    ctx: RequestContext = Depends(request_context),
) -> ProfileOut:
    """Upload a profile picture. Returns the updated profile with the avatar URL."""
    if file.content_type not in _ALLOWED_AVATAR_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(_ALLOWED_AVATAR_TYPES)}",
        )

    content = await file.read()
    if len(content) > _MAX_AVATAR_SIZE:
        raise HTTPException(status_code=413, detail="Avatar must be under 5MB")

    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    ext = ext_map.get(file.content_type or "", ".png")
    filename = f"{user.id}{ext}"
    dest = _AVATAR_DIR / filename

    for old in _AVATAR_DIR.glob(f"{user.id}.*"):
        if old.name != filename:
            try:
                old.unlink()
            except OSError:
                pass

    dest.write_bytes(content)

    avatar_path = f"/api/v1/auth/avatar/{filename}"
    updated = await auth_service.set_avatar_path(ctx, avatar_path)
    return auth_user_to_profile_out(updated)


@router.get("/auth/avatar/{filename}")
async def get_avatar(filename: str):
    """Serve a user's avatar file."""
    from fastapi.responses import FileResponse

    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = _AVATAR_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Avatar not found")

    return FileResponse(path)
