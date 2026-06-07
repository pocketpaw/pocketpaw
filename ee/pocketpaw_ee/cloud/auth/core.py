"""Enterprise auth — fastapi-users with JWT cookie + bearer transport.

Changes:
    2026-05-17 (security #1117 P1) — Cookie transport hardening:
        - cookie_secure is now env-driven (POCKETPAW_AUTH_COOKIE_SECURE,
          defaults to false for local HTTP dev; production must set true).
        - cookie_httponly explicitly pinned to True so JS can never read
          the JWT (defence against XSS token theft).
        - Bearer transport stays registered for back-compat (native /
          Tauri / API consumers); web build moves to cookie + CSRF.
        - Slated for removal once all clients ship the cookie path —
          see ee/cloud/auth/router.py for the deprecation note.
    Earlier: Added seed_workspace() to auto-create default workspace +
        General group on first boot.

Provides:
- POST /auth/register — sign up with email + password
- POST /auth/login — sign in, returns JWT cookie + token
- POST /auth/logout — clear cookie
- GET  /auth/me — current user
- PATCH /auth/me — update profile

Admin seeding: call seed_admin() on startup to ensure a default admin exists.
Workspace seeding: call seed_workspace() after seed_admin() to bootstrap first workspace.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from beanie import PydanticObjectId
from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users import schemas as fastapi_users_schemas
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users_db_beanie import BeanieUserDatabase, ObjectIDIDMixin

from pocketpaw_ee.cloud.models.user import OAuthAccount, User

logger = logging.getLogger(__name__)

SECRET = os.environ.get("AUTH_SECRET", "change-me-in-production-please")
TOKEN_LIFETIME = 60 * 60 * 24 * 7  # 7 days

# Cookie hardening — flip to true via env in any deployment that terminates
# TLS in front of the cloud (i.e. production). Local dev runs over plain
# HTTP, where Secure cookies would be silently dropped by the browser.
_COOKIE_SECURE = os.environ.get("POCKETPAW_AUTH_COOKIE_SECURE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# User database adapter
# ---------------------------------------------------------------------------


async def get_user_db():
    yield BeanieUserDatabase(User, OAuthAccount)


# ---------------------------------------------------------------------------
# User manager (handles registration, password hashing, etc.)
# ---------------------------------------------------------------------------


class UserManager(ObjectIDIDMixin, BaseUserManager[User, PydanticObjectId]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    async def on_after_register(self, user: User, request: Request | None = None):
        logger.info("User registered: %s (%s)", user.email, user.id)

    async def on_after_login(self, user: User, request: Request | None = None, response=None):
        logger.debug("User logged in: %s", user.email)


async def get_user_manager(user_db=Depends(get_user_db)):
    yield UserManager(user_db)


# ---------------------------------------------------------------------------
# Auth backends — cookie (browser) + bearer (API/Tauri)
# ---------------------------------------------------------------------------

cookie_transport = CookieTransport(
    cookie_name="paw_auth",
    cookie_max_age=TOKEN_LIFETIME,
    cookie_secure=_COOKIE_SECURE,  # env-driven; True in prod (HTTPS), False locally
    cookie_httponly=True,  # explicit — JS must never read the JWT
    cookie_samesite="lax",
)

bearer_transport = BearerTransport(tokenUrl="/api/v1/auth/login")


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=TOKEN_LIFETIME)


cookie_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)

bearer_backend = AuthenticationBackend(
    name="bearer",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

# ---------------------------------------------------------------------------
# FastAPIUsers instance
# ---------------------------------------------------------------------------

fastapi_users = FastAPIUsers[User, PydanticObjectId](
    get_user_manager,
    [cookie_backend, bearer_backend],
)

# Current user dependencies
current_active_user = fastapi_users.current_user(active=True)
current_optional_user = fastapi_users.current_user(active=True, optional=True)


# ---------------------------------------------------------------------------
# Schemas for register/read
# ---------------------------------------------------------------------------


class UserRead(fastapi_users_schemas.BaseUser[PydanticObjectId]):
    full_name: str = ""
    avatar: str = ""


class UserCreate(fastapi_users_schemas.BaseUserCreate):
    full_name: str = ""


# ---------------------------------------------------------------------------
# Admin seeding
# ---------------------------------------------------------------------------


async def seed_admin(
    email: str | None = None,
    password: str | None = None,
    full_name: str | None = None,
) -> User | None:
    """Create default admin user if it doesn't exist.

    Reads from env vars if args not provided:
      ADMIN_EMAIL (default: admin@pocketpaw.ai)
      ADMIN_PASSWORD (default: admin123)
      ADMIN_NAME (default: Admin)
    """
    email = email or os.environ.get("ADMIN_EMAIL", "admin@pocketpaw.ai")
    password = password or os.environ.get("ADMIN_PASSWORD", "admin123")
    full_name = full_name or os.environ.get("ADMIN_NAME", "Admin")

    existing = await User.find_one(User.email == email)
    if existing:
        logger.debug("Admin user already exists: %s", email)
        return existing

    from fastapi_users.exceptions import UserAlreadyExists

    db = BeanieUserDatabase(User, OAuthAccount)
    manager = UserManager(db)
    try:
        user = await manager.create(
            UserCreate(
                email=email,
                password=password,
                full_name=full_name,
                is_superuser=True,
                is_verified=True,
            ),
        )
        user.full_name = full_name
        await user.save()
        logger.info("Admin user created: %s (password: %s)", email, password)
        return user
    except UserAlreadyExists:
        return await User.find_one(User.email == email)
    except Exception as exc:
        logger.error("Failed to seed admin: %s", exc)
        return None


async def seed_workspace(admin: User | None = None) -> Any | None:
    """Bootstrap a default workspace, General chat group, and pocketpaw
    agent on first boot. Idempotent — skips if a workspace already exists.

    Thin orchestrator: each entity's seed lives in its own service module
    so this file doesn't touch other entities' Beanie docs directly.
    """
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.chat import group_service
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    if admin is None:
        admin = await User.find_one(User.is_superuser == True)  # noqa: E712
        if not admin:
            logger.debug("No admin user found — skipping workspace seed")
            return None

    ws_name = os.environ.get("DEFAULT_WORKSPACE_NAME", "PocketPaw")
    ws_slug = os.environ.get("DEFAULT_WORKSPACE_SLUG", "pocketpaw")

    ws = await workspace_service.seed_default_workspace(str(admin.id), name=ws_name, slug=ws_slug)
    if ws is None:
        # Skipped or failed — service logged the reason.
        return None

    # Default "General" chat group — best-effort.
    await group_service.seed_default_group(str(ws.id), str(admin.id))

    # Default "pocketpaw" agent — the agent that users DM through the
    # runtime SSE chat endpoint. Gives DMs a stable identity so sessions
    # can be keyed by agent_id.
    try:
        await agents_service.seed_default_agent(str(ws.id), str(admin.id))
    except Exception as exc:
        logger.warning("Failed to seed default agent (non-fatal): %s", exc)

    return ws


async def ensure_default_agent_all_workspaces() -> int:
    """Compatibility re-export — agents own this back-fill now."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    return await agents_service.ensure_default_agent_all_workspaces()


async def seed_default_agent(workspace_id: str, owner_id: str):
    """Compatibility re-export — agents own the seed now."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    return await agents_service.seed_default_agent(workspace_id, owner_id)
