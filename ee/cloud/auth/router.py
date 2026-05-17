"""Auth domain — FastAPI router.

Profile endpoints use ``Depends(request_context)`` and call into
``ee.cloud.auth.service`` module functions directly. The fastapi-users
sub-routers (login/logout/register) and the avatar file-serving / upload
endpoints stay here unchanged in behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud.auth import service as auth_service
from ee.cloud.auth.core import (
    UserCreate,
    UserRead,
    bearer_backend,
    cookie_backend,
    current_active_user,
    fastapi_users,
)
from ee.cloud.auth.dto import (
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
