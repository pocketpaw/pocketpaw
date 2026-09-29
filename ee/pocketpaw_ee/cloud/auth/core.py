"""Enterprise auth: fastapi-users with JWT cookie + bearer transport.

Provides the UserManager (registration, login, password reset), the cookie and
bearer auth backends, the FastAPIUsers instance, and first-boot seeding
(seed_admin, seed_workspace). Routes are mounted in ``auth/router.py``.

Import-time side effect (read this before touching imports or tests):
    ``SECRET = _resolve_secret()`` runs at MODULE IMPORT time. In a production
    posture (POCKETPAW_ENV=production/prod, or POCKETPAW_AUTH_COOKIE_SECURE=true)
    with no real ``AUTH_SECRET``, importing this module raises RuntimeError.
    In dev it substitutes an ephemeral random secret and warns (loudly for a
    non-dev, non-prod label such as ``staging``). Tests must set the env vars
    BEFORE the first import; after it ``SECRET`` is frozen.

Password hashing never runs on the event loop. UserManager overrides the
fastapi-users methods that hash (create, authenticate, forgot_password,
reset_password, _update) so they go through ``auth.password_hashing``, and
defaults to that module's OWASP-parameter helper. The overrides copy upstream
logic from fastapi-users 15.0.5; ``tests/cloud/auth/test_password_offload.py``
fails if the upstream source of those methods changes.

seed_admin never defaults to a known password: ADMIN_PASSWORD (not the legacy
``admin123``) or a generated one printed once to stdout, never to the logger.
The cookie is httponly; Secure comes from POCKETPAW_AUTH_COOKIE_SECURE.
"""

from __future__ import annotations

import logging
import os
import secrets
import string
import uuid
from typing import Any

import jwt
from beanie import PydanticObjectId
from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers, exceptions
from fastapi_users import schemas as fastapi_users_schemas
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    CookieTransport,
    JWTStrategy,
)
from fastapi_users.jwt import decode_jwt, generate_jwt
from fastapi_users.password import PasswordHelperProtocol
from fastapi_users_db_beanie import BeanieUserDatabase, ObjectIDIDMixin

from pocketpaw_ee.cloud.auth import password_hashing
from pocketpaw_ee.cloud.auth.password_policy import validate_password_async
from pocketpaw_ee.cloud.models.user import OAuthAccount, User, WorkspaceMembership

logger = logging.getLogger(__name__)

# The historical placeholder. Shipping with this value means every deployment
# signs JWTs with a public secret — anyone can forge an admin token. Treated as
# "no real secret set" everywhere below.
_DEFAULT_SECRET = "change-me-in-production-please"

# The historical hardcoded admin password. Kept here only so we can detect and
# refuse it; it is never used as a live credential anymore.
_LEGACY_ADMIN_PASSWORD = "admin123"


def _is_production() -> bool:
    """True when this process is running in a production posture.

    Two signals, OR'd, so a real deployment trips at least one:
      * ``POCKETPAW_ENV`` set to ``production`` / ``prod`` (the explicit knob —
        documented in the Dockerfile so the shipped container is prod by
        default), or
      * ``POCKETPAW_AUTH_COOKIE_SECURE=true`` — already mandatory for any
        TLS-terminated deployment (see the cookie-hardening note above), so an
        operator who set it has, by definition, a production front door.

    Dev / test set neither, so they fall through to the ergonomic path
    (generated secret + generated admin password). Deny-by-default would break
    the test suite and local boot, so the compromise is: prod must be
    *signalled*, but the realistic prod path (TLS + Secure cookies, or the
    shipped Docker image) signals it without extra operator effort.
    """
    env = os.environ.get("POCKETPAW_ENV", "").strip().lower()
    if env in {"production", "prod"}:
        return True
    return os.environ.get("POCKETPAW_AUTH_COOKIE_SECURE", "false").strip().lower() == "true"


# Env labels we treat as unambiguous local development. Anything else that is
# *set* (e.g. "staging", "qa", "preprod") is an ambiguous non-prod label: it is
# clearly not a dev box, yet ``_is_production()`` didn't positively detect prod,
# so the insecure dev defaults would apply silently. We warn loudly in that gap.
_DEV_ENV_LABELS = {"dev", "development", "local", "test"}


def _is_ambiguous_nonprod_label() -> bool:
    """True for a non-prod posture wearing a label that isn't obviously dev.

    Returns True only when ALL of:
      * ``_is_production()`` is False (prod was not positively detected), and
      * ``POCKETPAW_ENV`` is *set* to a non-empty value, and
      * that value is not one of the known local-dev labels (``dev`` /
        ``development`` / ``local`` / ``test``).

    The motivating case is ``POCKETPAW_ENV=staging`` with no cookie-secure and
    no real ``AUTH_SECRET`` / no operator license key: it boots on the insecure
    defaults with zero signal. Unset env and explicit dev labels return False
    (their ergonomic silent-dev path is intentional); explicit prod is handled
    by the hard fail-fast upstream, never reaching here.
    """
    if _is_production():
        return False
    env = os.environ.get("POCKETPAW_ENV", "").strip().lower()
    return bool(env) and env not in _DEV_ENV_LABELS


def _resolve_secret() -> str:
    """Return the JWT signing secret, refusing the insecure default in prod.

    Production posture: a real ``AUTH_SECRET`` is required. Unset or equal to
    the public placeholder → fail fast with a clear, actionable error so the
    tenant can never boot ownable.

    Dev / test posture: an unset / placeholder secret is tolerated, but we
    substitute a fresh random per-process secret (NOT the public default) and
    warn loudly. Ephemeral by design — tokens don't survive a restart, which is
    correct for dev and forces operators to set a stable secret before prod.
    """
    raw = os.environ.get("AUTH_SECRET", "").strip()
    if raw and raw != _DEFAULT_SECRET:
        return raw

    if _is_production():
        raise RuntimeError(
            "AUTH_SECRET is unset or still the public placeholder "
            f"({_DEFAULT_SECRET!r}). Refusing to boot in production: JWTs would "
            "be signed with a secret anyone can read, letting attackers forge "
            "admin sessions. Set AUTH_SECRET to a strong random value, e.g.\n"
            "  AUTH_SECRET=\"$(python -c 'import secrets; "
            "print(secrets.token_urlsafe(48))')\""
        )

    if _is_ambiguous_nonprod_label():
        # Staging-posture blind spot: a labelled-but-not-prod deployment (e.g.
        # POCKETPAW_ENV=staging) without a real AUTH_SECRET would otherwise boot
        # SILENTLY on the ephemeral default. Warn loudly so an operator who
        # meant this to be prod-like notices the insecure secret. We still
        # substitute the ephemeral secret (boot is unchanged) — only the signal
        # is added; flipping this to a hard fail would break the test suite and
        # any intentional staging-on-defaults run.
        logger.warning(
            "POCKETPAW_ENV=%s is a non-dev, non-prod label, but no real "
            "AUTH_SECRET is set and production was not positively detected. "
            "Falling back to an EPHEMERAL per-process JWT secret — tokens will "
            "not survive a restart and this deployment is NOT production-secure. "
            "Set a strong AUTH_SECRET (and POCKETPAW_ENV=production) before "
            "treating this as production.",
            os.environ.get("POCKETPAW_ENV", "").strip(),
        )

    ephemeral = secrets.token_urlsafe(48)
    logger.warning(
        "AUTH_SECRET is unset or the public default — using an ephemeral "
        "per-process secret for dev. Tokens will NOT survive a restart. Set a "
        "strong AUTH_SECRET (and POCKETPAW_ENV=production) before deploying."
    )
    return ephemeral


SECRET = _resolve_secret()
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

    def __init__(self, user_db: Any, password_helper: PasswordHelperProtocol | None = None):
        super().__init__(user_db, password_helper or password_hashing.password_helper)

    async def validate_password(self, password: str, user: Any) -> None:
        email = getattr(user, "email", None) or ""
        await validate_password_async(password, email=email)

    # --- Hashing overrides --------------------------------------------------
    # Upstream (fastapi-users 15.0.5) calls the sync password helper inside
    # these async methods, blocking the event loop for a full argon2 call.
    # Each override below is the upstream body with only the hash/verify line
    # swapped for its offloaded twin. test_password_offload.py pins the
    # upstream source; when it fails, re-diff these against the new version.

    async def _hash(self, password: str) -> str:
        return await password_hashing.hash_password(password, self.password_helper)

    async def _verify_and_update(self, plain: str, hashed: str) -> tuple[bool, str | None]:
        return await password_hashing.verify_and_update(plain, hashed, self.password_helper)

    async def create(self, user_create: Any, safe: bool = False, request: Request | None = None):
        await self.validate_password(user_create.password, user_create)

        existing_user = await self.user_db.get_by_email(user_create.email)
        if existing_user is not None:
            raise exceptions.UserAlreadyExists()

        user_dict = (
            user_create.create_update_dict() if safe else user_create.create_update_dict_superuser()
        )
        password = user_dict.pop("password")
        user_dict["hashed_password"] = await self._hash(password)

        created_user = await self.user_db.create(user_dict)

        await self.on_after_register(created_user, request)

        return created_user

    async def authenticate(self, credentials: Any) -> User | None:
        try:
            user = await self.get_by_email(credentials.username)
        except exceptions.UserNotExists:
            # Still pay for one hash so an unknown email takes as long as a
            # wrong password (timing attack mitigation, as upstream does).
            await self._hash(credentials.password)
            return None

        verified, updated_password_hash = await self._verify_and_update(
            credentials.password, user.hashed_password
        )
        if not verified:
            return None
        # Rehash to the current parameters (e.g. an old 64 MiB argon2 hash).
        if updated_password_hash is not None:
            await self.user_db.update(user, {"hashed_password": updated_password_hash})

        return user

    async def forgot_password(self, user: User, request: Request | None = None) -> None:
        if not user.is_active:
            raise exceptions.UserInactive()

        token_data = {
            "sub": str(user.id),
            "password_fgpt": await self._hash(user.hashed_password),
            "aud": self.reset_password_token_audience,
        }
        token = generate_jwt(
            token_data,
            self.reset_password_token_secret,
            self.reset_password_token_lifetime_seconds,
        )
        await self.on_after_forgot_password(user, token, request)

    async def reset_password(self, token: str, password: str, request: Request | None = None):
        try:
            data = decode_jwt(
                token,
                self.reset_password_token_secret,
                [self.reset_password_token_audience],
            )
        except jwt.PyJWTError:
            raise exceptions.InvalidResetPasswordToken()

        try:
            user_id = data["sub"]
            password_fingerprint = data["password_fgpt"]
        except KeyError:
            raise exceptions.InvalidResetPasswordToken()

        try:
            parsed_id = self.parse_id(user_id)
        except exceptions.InvalidID:
            raise exceptions.InvalidResetPasswordToken()

        user = await self.get(parsed_id)

        valid_password_fingerprint, _ = await self._verify_and_update(
            user.hashed_password, password_fingerprint
        )
        if not valid_password_fingerprint:
            raise exceptions.InvalidResetPasswordToken()

        if not user.is_active:
            raise exceptions.UserInactive()

        updated_user = await self._update(user, {"password": password})

        await self.on_after_reset_password(user, request)

        return updated_user

    async def _update(self, user: User, update_dict: dict[str, Any]) -> User:
        # Hash the new password here, off the loop, and hand upstream a
        # ready ``hashed_password``, which its loop passes through untouched.
        password = update_dict.get("password")
        if password is not None:
            await self.validate_password(password, user)
            update_dict = {k: v for k, v in update_dict.items() if k != "password"}
            update_dict["hashed_password"] = await self._hash(password)
        return await super()._update(user, update_dict)

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


def _generate_admin_password(length: int = 24) -> str:
    """Generate a strong random password that satisfies the password policy.

    The policy (``auth.password_policy``) requires upper, lower, digit and
    symbol, plus a minimum length. We guarantee one of each class, then fill
    the rest from the full alphabet and shuffle, so the seeded admin always
    passes ``validate_password`` and is never weaker than a user-chosen one.
    """
    # Symbols restricted to a shell/URL-safe set so the operator can copy the
    # printed password without quoting headaches.
    symbols = "!@#$%^&*-_=+?"
    alphabet = string.ascii_letters + string.digits + symbols
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(symbols),
    ]
    fill_count = max(length, len(required)) - len(required)
    remaining = [secrets.choice(alphabet) for _ in range(fill_count)]
    chars = required + remaining
    # secrets-backed Fisher-Yates so the guaranteed-class chars aren't pinned
    # to the front (random.shuffle isn't cryptographically seeded).
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


async def seed_admin(
    email: str | None = None,
    password: str | None = None,
    full_name: str | None = None,
) -> User | None:
    """Create the default admin user if it doesn't already exist.

    Resolution order for the initial password:
      1. The ``password`` argument, if passed explicitly.
      2. ``ADMIN_PASSWORD`` from the environment (operator-supplied) — the
         legacy ``admin123`` is rejected so a stale ``.env`` can't reintroduce
         the weak credential.
      3. A freshly generated strong random password, printed ONCE to stdout for
         the operator. The password is NEVER written to the application logger.

    Other env defaults:
      ADMIN_EMAIL (default: admin@pocketpaw.ai)
      ADMIN_NAME  (default: Admin)
    """
    email = email or os.environ.get("ADMIN_EMAIL", "admin@pocketpaw.ai")
    full_name = full_name or os.environ.get("ADMIN_NAME", "Admin")

    # Resolve the initial password without ever defaulting to a known value.
    generated = False
    if password is None:
        env_password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
        if env_password and env_password != _LEGACY_ADMIN_PASSWORD:
            password = env_password
        else:
            if env_password == _LEGACY_ADMIN_PASSWORD:
                logger.warning(
                    "ADMIN_PASSWORD is set to the legacy default — ignoring it "
                    "and generating a strong random initial admin password."
                )
            password = _generate_admin_password()
            generated = True

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
        # Log WITHOUT the password — the credential never touches the logger.
        logger.info("Admin user created: %s", email)
        if generated:
            # One-time, stdout-only disclosure for the operator who just booted
            # the tenant. Deliberately not logger.info so it can't land in log
            # aggregation / files. Print rather than return-only so a headless
            # first boot still surfaces the credential to the console operator.
            print(  # noqa: T201 — intentional one-time operator channel
                "\n"
                "============================================================\n"
                " PocketPaw — initial admin account created\n"
                f"   email:    {email}\n"
                f"   password: {password}\n"
                " Save this now. It is shown once and is NOT written to logs.\n"
                " Set ADMIN_PASSWORD to choose your own before first boot.\n"
                "============================================================\n",
                flush=True,
            )
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
