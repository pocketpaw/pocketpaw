"""Enterprise auth — fastapi-users with JWT cookie + bearer transport.

Changes: Added seed_workspace() to auto-create default workspace + General group
on first boot, so admin can immediately use the app after seeding.

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

from ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership
from ee.cloud.models.workspace import Workspace, WorkspaceSettings

logger = logging.getLogger(__name__)

SECRET = os.environ.get("AUTH_SECRET", "change-me-in-production-please")
TOKEN_LIFETIME = 60 * 60 * 24 * 7  # 7 days


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
    cookie_secure=False,  # Set True in production with HTTPS
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


async def seed_workspace(admin: User | None = None) -> Workspace | None:
    """Create a default workspace and General chat group if none exist.

    Called after seed_admin() on startup. Skips if any workspace already exists.
    """
    from datetime import UTC, datetime

    if admin is None:
        admin = await User.find_one(User.is_superuser == True)  # noqa: E712
        if not admin:
            logger.debug("No admin user found — skipping workspace seed")
            return None

    # Skip if admin already has a workspace
    if admin.workspaces:
        logger.debug("Admin already has workspace(s) — skipping seed")
        return None

    # Also skip if any workspace exists at all
    existing = await Workspace.find_one()
    if existing:
        logger.debug("Workspace already exists — skipping seed")
        return None

    ws_name = os.environ.get("DEFAULT_WORKSPACE_NAME", "PocketPaw")
    ws_slug = os.environ.get("DEFAULT_WORKSPACE_SLUG", "pocketpaw")

    try:
        ws = Workspace(
            name=ws_name,
            slug=ws_slug,
            owner=str(admin.id),
            plan="enterprise",
            seats=50,
            settings=WorkspaceSettings(),
        )
        await ws.insert()

        admin.workspaces.append(
            WorkspaceMembership(
                workspace=str(ws.id),
                role="owner",
                joined_at=datetime.now(UTC),
            )
        )
        admin.active_workspace = str(ws.id)
        await admin.save()

        logger.info(
            "Default workspace created: %s (slug: %s, id: %s)",
            ws_name,
            ws_slug,
            ws.id,
        )

        # Create a default "General" chat group
        try:
            from ee.cloud.models.group import Group

            group = Group(
                workspace=str(ws.id),
                name="General",
                slug="general",
                description="Default channel for team discussion",
                type="public",
                owner=str(admin.id),
                members=[str(admin.id)],
            )
            await group.insert()
            logger.info("Default 'General' group created in workspace %s", ws_name)
        except Exception as exc:
            logger.warning("Failed to create default group (non-fatal): %s", exc)

        return ws
    except Exception as exc:
        logger.error("Failed to seed workspace: %s", exc)
        return None
