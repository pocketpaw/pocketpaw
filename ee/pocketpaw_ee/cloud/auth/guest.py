# ee/pocketpaw_ee/cloud/auth/guest.py — server-minted anonymous guests
# (BYOK-first onboarding) and the upgrade that turns one into a real account.
#
# Providers: ``byok_service.SUPPORTED_PROVIDERS`` is the only gate; an
# OpenAI-compatible gateway key brings its own ``base_url`` and ``model``.
# ``mint_guest`` stores the base URL ``validate_key`` hands back (normalized
# before it was guarded), never the raw body string: this route skips
# ``ByokSetRequest``, so ``validate_key`` is the whole guard.
# ``upgrade_guest`` / ``upgrade_guest_via_social`` promote the SAME user id in
# place (password or social identity), so pages and the stored key survive.
# Password hashes use the shared ``auth.password_hashing`` helper, off the loop.
# ``upgrade_guest`` runs the register password policy (incl. HIBP) first.
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

from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.cloud.auth.password_hashing import hash_password
from pocketpaw_ee.cloud.auth.password_policy import validate_password_async
from pocketpaw_ee.cloud.byok import service as byok_service
from pocketpaw_ee.cloud.models.user import GuestLimits, User

logger = logging.getLogger(__name__)

#: Synthetic-address domain for guest rows. ``.invalid`` is RFC 2606 reserved —
#: it can never resolve, so a guest row can never receive (or leak into) mail.
_GUEST_EMAIL_DOMAIN = "guest.invalid"


def is_provider_supported(provider: str) -> bool:
    return provider in byok_service.SUPPORTED_PROVIDERS


async def mint_guest(
    api_key: str,
    *,
    provider: str = "anthropic",
    base_url: str | None = None,
    model: str | None = None,
) -> User:
    """Validate the key, then mint user + workspace + encrypted key row.

    Raises ``ValidationError`` (422) for an unsupported provider and lets
    ``byok_service.validate_key``'s errors (bad key / provider down) propagate
    untouched — their codes are part of the guest-mint wire contract. The
    caller (router) has already rate-limited.
    """
    if not is_provider_supported(provider):
        raise ValidationError(
            "byok.provider_unsupported",
            "That key's provider is not supported yet. Paste an Anthropic key, "
            "or pick the custom gateway option and give its address.",
        )
    if provider == "openai_compatible":
        if not (base_url or "").strip():
            raise ValidationError(
                "byok.base_url_required",
                "A custom gateway needs its address, e.g. https://host/v1.",
            )
        if not (model or "").strip():
            raise ValidationError(
                "byok.model_required",
                "A custom gateway needs the model id it serves.",
            )
    if not api_key or not api_key.strip():
        raise ValidationError("byok.key_missing", "Enter an API key to try Otherhand.")
    api_key = api_key.strip()

    # 1. Prove the key works BEFORE anything is created. For a gateway this is
    #    also the ONLY SSRF guard on this path — ``_GuestMintRequest`` carries
    #    plain ``str`` fields and never touches ``ByokSetRequest`` — and the
    #    route is unauthenticated, so a stranger picks the address.
    #    Keep the URL it hands back: it normalized before guarding, and the
    #    value that reaches the database must be the value that passed
    #    (review S6). Previously the raw body string was stored instead.
    canonical_base_url = await byok_service.validate_key(
        api_key, provider=provider, base_url=base_url, model=model
    )
    if canonical_base_url:
        base_url = canonical_base_url

    # 2. Mint the anonymous user. Synthetic unique email (fastapi-users
    #    requires one), random password nobody knows — the account is only
    #    reachable through the session minted at the end of this request until
    #    /auth/guest/upgrade attaches real credentials.
    tag = uuid.uuid4().hex[:12]
    user = User(
        email=f"guest-{tag}@{_GUEST_EMAIL_DOMAIN}",
        hashed_password=await hash_password(secrets.token_urlsafe(32)),
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
            base_url=base_url,
            model=model,
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
    # Same policy (and HIBP check) as /auth/register; raises
    # InvalidPasswordException, which the route maps to register's 400 shape.
    await validate_password_async(password, email=email)

    user.email = email
    user.hashed_password = await hash_password(password)
    user.is_guest = False
    user.guest_limits = None
    await user.save()
    logger.info("guest upgraded: user=%s", user.id)
    return user


async def upgrade_guest_via_social(user: User, *, email: str) -> User:
    """Promote a guest whose provider identity has just been attached.

    The sibling of :func:`upgrade_guest`, for the door that has no password.
    Same promotion, same user id, so the workspace, sessions, pages and stored
    key stay exactly where they are — which is the only reason the kiosk can
    offer "Continue with Google" at all.

    No password is set. The account is reachable through the attached identity,
    which ``_find_by_oauth_account`` matches on the provider's immutable id, and
    the guest's original hash is a random string nobody has ever held. It stays
    put rather than being blanked: ``_has_usable_password`` reads the field, and
    an empty hash compares equal in some verifiers.

    ``is_verified`` becomes True because the provider vouched for the address —
    ``decide_link`` refuses an identity it will not vouch for, so by the time we
    are here that is established, not assumed.

    Callers MUST have checked the address is free first (see
    ``social.service._apply_link_policy``). This function does not re-check:
    the check has to happen BEFORE the identity is attached, or a refusal
    leaves a half-upgraded row behind.
    """
    if not user.is_guest:
        raise CloudError(409, "auth.not_a_guest", "This account is already registered.")

    user.email = email.strip().lower()
    user.is_guest = False
    user.guest_limits = None
    user.is_verified = True
    await user.save()
    logger.info("guest upgraded via social: user=%s", user.id)
    return user


__all__ = [
    "is_provider_supported",
    "mint_guest",
    "upgrade_guest",
    "upgrade_guest_via_social",
]
