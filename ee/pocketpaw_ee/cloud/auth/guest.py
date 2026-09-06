# ee/pocketpaw_ee/cloud/auth/guest.py — server-minted anonymous guests
# (BYOK-first onboarding) and the upgrade that turns one into a real account.
#
# Created 2026-09-01 (feat/byok-guest-backend).
#
# Flow (the order is the security property):
#   rate-limit -> validate the key against the provider -> mint user ->
#   provision workspace (default agent + LiteLLM tenant key ride along) ->
#   store the key encrypted.
# A dead key fails BEFORE anything exists, with a plain message — never on the
# first turn (the switched-off-vs-broken rule applied to onboarding). The rate
# limit sits before validation because the validate call is a provider round
# trip on OUR egress: unthrottled, /auth/guest is a free key-checking oracle.
#
# CUSTODY DIVERGENCE from the design draft (flagged to the captain): the draft
# says the key is never stored server-side and rides each turn as a header.
# This implementation REUSES the reviewed Fernet store
# (``byok/service.set_key`` + ``resolve_turn_credentials``) instead — far less
# new surface than a per-turn header path, and it un-inerts the existing
# /agents BYOK field for every user, not just guests.
#
# NEVER log the key. Nothing in this module puts ``api_key`` into a log call,
# an exception message, or a stored field beyond the encrypted upsert;
# ``tests/cloud/auth/test_guest.py`` pins that with a caplog sweep.

from __future__ import annotations

import logging
import secrets
import uuid

from fastapi_users.password import PasswordHelper

from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.cloud.byok import service as byok_service
from pocketpaw_ee.cloud.models.user import GuestLimits, User

logger = logging.getLogger(__name__)

_pwd_helper = PasswordHelper()

#: Synthetic-address domain for guest rows. ``.invalid`` is RFC 2606 reserved —
#: it can never resolve, so a guest row can never receive (or leak into) mail.
_GUEST_EMAIL_DOMAIN = "guest.invalid"


def is_provider_supported(provider: str) -> bool:
    return provider in byok_service.SUPPORTED_PROVIDERS


async def mint_guest(api_key: str, *, provider: str = "anthropic") -> User:
    """Validate the key, then mint user + workspace + encrypted key row.

    Raises ``ValidationError`` (422) for an unsupported provider and lets
    ``byok_service.validate_key``'s errors (bad key / provider down) propagate
    untouched — their codes are part of the guest-mint wire contract. The
    caller (router) has already rate-limited.
    """
    if not is_provider_supported(provider):
        raise ValidationError(
            "byok.provider_unsupported",
            "Only Anthropic keys are supported right now — OpenAI and "
            "OpenRouter are coming. Paste a key from console.anthropic.com.",
        )
    if not api_key or not api_key.strip():
        raise ValidationError("byok.key_missing", "Enter an API key to try Otherhand.")
    api_key = api_key.strip()

    # 1. Prove the key works BEFORE anything is created.
    await byok_service.validate_key(api_key)

    # 2. Mint the anonymous user. Synthetic unique email (fastapi-users
    #    requires one), random password nobody knows — the account is only
    #    reachable through the session minted at the end of this request until
    #    /auth/guest/upgrade attaches real credentials.
    tag = uuid.uuid4().hex[:12]
    user = User(
        email=f"guest-{tag}@{_GUEST_EMAIL_DOMAIN}",
        hashed_password=_pwd_helper.hash(secrets.token_urlsafe(32)),
        full_name="Guest",
        is_active=True,
        is_verified=False,
        is_guest=True,
        guest_limits=GuestLimits(),
    )
    await user.insert()

    # 3. Provision the workspace through the REAL create path — the default
    #    agent seed (the notebook needs a DM target) and the best-effort
    #    LiteLLM tenant key ride along, and ``_add_member(set_active=True)``
    #    stamps ``active_workspace`` so /auth/me never routes the guest into
    #    the /welcome workspace funnel.
    from pocketpaw_ee.cloud.workspace import service as workspace_service
    from pocketpaw_ee.cloud.workspace.dto import CreateWorkspaceRequest

    try:
        ws = await workspace_service.create(
            workspace_service.legacy_ctx(user),
            CreateWorkspaceRequest(name="Guest Notebook", slug=f"guest-{tag}"),
        )
        # 4. Store the key encrypted, per-workspace. validate=False: step 1 just
        #    validated this exact key; a second provider round trip per mint
        #    buys nothing.
        await byok_service.set_key(
            ws.id,
            api_key,
            provider=provider,
            user_id=str(user.id),
            validate=False,
        )
    except Exception:
        # A guest row with no workspace or no key is a dead end the user
        # cannot escape — remove it so a retry starts clean. Best-effort.
        logger.warning("guest mint failed after user insert; rolling back user row")
        try:
            await user.delete()
        except Exception:
            logger.exception("could not roll back half-minted guest user %s", user.id)
        raise

    # Re-read: workspace_service.create mutated the row (_add_member saves its
    # own fetch), so ``user`` here is stale.
    fresh = await User.get(user.id)
    logger.info("guest minted: user=%s workspace=%s provider=%s", user.id, ws.id, provider)
    return fresh if fresh is not None else user


async def upgrade_guest(user: User, *, email: str, password: str) -> User:
    """Attach email+password to the SAME user id; flip ``is_guest`` off.

    Everything else — workspace, sessions, pages, the stored key — stays,
    which is the whole reason guests are minted server-side. fastapi-users'
    stock /auth/register cannot do this (it always creates a NEW user), hence
    the dedicated route.
    """
    if not user.is_guest:
        raise CloudError(409, "auth.not_a_guest", "This account is already registered.")
    email = email.strip().lower()
    if not email or "@" not in email:
        raise ValidationError("auth.email_invalid", "Enter a valid email address.")
    if len(password) < 8:
        raise ValidationError("auth.password_too_short", "Password must be at least 8 characters.")
    existing = await User.find_one(User.email == email)
    if existing is not None and existing.id != user.id:
        raise CloudError(409, "auth.email_taken", "That email already has an account — sign in.")

    user.email = email
    user.hashed_password = _pwd_helper.hash(password)
    user.is_guest = False
    user.guest_limits = None
    await user.save()
    logger.info("guest upgraded: user=%s", user.id)
    return user


__all__ = ["is_provider_supported", "mint_guest", "upgrade_guest"]
