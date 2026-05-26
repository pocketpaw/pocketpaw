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
import uuid
from typing import Any

import jwt
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
from fastapi_users.jwt import generate_jwt
from fastapi_users_db_beanie import BeanieUserDatabase, ObjectIDIDMixin

from pocketpaw_ee.cloud.auth.password_policy import validate_password_async
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership

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

    async def validate_password(self, password: str, user: Any) -> None:
        email = getattr(user, "email", None) or ""
        await validate_password_async(password, email=email)

    async def on_after_register(self, user: User, request: Request | None = None):
        logger.info("User registered: %s (%s)", user.email, user.id)
        # Wave 3 Task 12: best-effort auto-join via verified-domain capture.
        # Wrapped so any failure here (DNS, DB, audit) never blocks the
        # newly-minted account from being usable.
        try:
            email = (user.email or "").lower()
            if "@" not in email:
                return
            domain = email.split("@", 1)[1]

            # Local import to avoid the workspace package on the OSS-only
            # startup path (auth.core is imported broadly).
            from pocketpaw_ee.cloud.audit import service as audit_service
            from pocketpaw_ee.cloud.workspace import domains as domains_service

            ws = await domains_service.find_workspace_by_verified_domain(domain)
            if ws is None:
                return
            if any(m.workspace == str(ws.id) for m in user.workspaces):
                return

            user.workspaces.append(WorkspaceMembership(workspace=str(ws.id), role="member"))
            if user.active_workspace is None:
                user.active_workspace = str(ws.id)
            await user.save()

            try:
                await audit_service.record(
                    str(ws.id),
                    str(user.id),
                    "domain.auto_join",
                    target_type="user",
                    target_id=str(user.id),
                    metadata={"email": email, "domain": domain},
                )
            except Exception:  # noqa: BLE001
                logger.debug("auto-join audit record failed", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.warning("verified-domain auto-join failed for %s", user.email, exc_info=True)

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


class RevocableJWTStrategy(JWTStrategy):
    """JWTStrategy that mints a ``jti`` and refuses revoked tokens.

    The base strategy's ``write_token`` does not include ``jti``; we
    override it to embed one so :mod:`pocketpaw_ee.cloud.auth.sessions`
    can index per-session state. ``read_token`` short-circuits to None
    when the jti is in the Redis revocation set for the user.
    """

    async def read_token(self, token, user_manager):  # type: ignore[override]
        if token is None:
            return None
        from pocketpaw_ee.cloud.auth import sessions as sessions_service

        try:
            payload = jwt.decode(
                token,
                self.decode_key
                if isinstance(self.decode_key, str)
                else self.decode_key.get_secret_value(),
                audience=self.token_audience,
                algorithms=[self.algorithm],
            )
        except jwt.PyJWTError:
            return None
        jti = payload.get("jti")
        user_id = payload.get("sub")
        if jti and user_id and await sessions_service.is_revoked(user_id, jti):
            return None
        return await super().read_token(token, user_manager)

    async def write_token(self, user) -> str:  # type: ignore[override]
        data = {
            "sub": str(user.id),
            "aud": self.token_audience,
            "jti": uuid.uuid4().hex,
        }
        return generate_jwt(data, self.encode_key, self.lifetime_seconds, algorithm=self.algorithm)


def get_jwt_strategy() -> JWTStrategy:
    return RevocableJWTStrategy(secret=SECRET, lifetime_seconds=TOKEN_LIFETIME)


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
