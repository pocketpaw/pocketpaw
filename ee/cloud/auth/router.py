"""Auth domain — FastAPI router."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ee.cloud.auth.core import (
    UserCreate,
    UserRead,
    bearer_backend,
    cookie_backend,
    current_active_user,
    fastapi_users,
)
from ee.cloud.auth.schemas import ProfileUpdateRequest, SetWorkspaceRequest
from ee.cloud.auth.service import AuthService
from ee.cloud.models.user import User

router = APIRouter(tags=["Auth"])

# ---------------------------------------------------------------------------
# fastapi-users auth routes (login/logout)
# ---------------------------------------------------------------------------

router.include_router(
    fastapi_users.get_auth_router(cookie_backend),
    prefix="/auth",
)
router.include_router(
    fastapi_users.get_auth_router(bearer_backend),
    prefix="/auth/bearer",
)

# Register route
router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
)


# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------


@router.get("/auth/me")
async def get_me(user: User = Depends(current_active_user)):
    return await AuthService.get_profile(user)


@router.patch("/auth/me")
async def update_me(
    body: ProfileUpdateRequest,
    user: User = Depends(current_active_user),
):
    return await AuthService.update_profile(user, body)


@router.post("/auth/set-active-workspace")
async def set_active_workspace(
    body: SetWorkspaceRequest,
    user: User = Depends(current_active_user),
):
    await AuthService.set_active_workspace(user, body.workspace_id)
    return {"ok": True, "activeWorkspace": body.workspace_id}
