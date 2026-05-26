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

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.auth import mfa as mfa_service
from pocketpaw_ee.cloud.auth import service as auth_service
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

router = APIRouter(tags=["Auth"])

# Avatar storage — local filesystem for now (could swap for S3/R2 later)
_AVATAR_DIR = Path.home() / ".pocketpaw" / "avatars"
_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
_ALLOWED_AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# fastapi-users sub-routers (login/logout/register)
#
# Both transports stay live during the cookie+CSRF rollout (security
# #1117 P1). Cookie is the long-term path for browser clients; Bearer
# is retained for back-compat with the Tauri desktop client and any
# automation / MCP tools that hold a token directly.
#
# Deprecation timeline: drop the ``/auth/bearer/*`` sub-router once the
# Tauri client moves to the OS keychain flow (#1117 P2) and we've
# audited internal scripts. Until then, removing Bearer would force a
# coordinated multi-repo migration with no rollback path.
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
