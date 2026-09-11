# ee/pocketpaw_ee/cloud/byok/service.py — the only reader of ByokProviderKey.
#
# Updated 2026-09-01 (feat/byok-guest-backend): ``set_key`` gained
# ``validate: bool = True`` so the guest-mint route (which validates BEFORE
# minting anything) can store without a second provider round trip. Also added
# ``SUPPORTED_PROVIDERS`` — the turn pipeline (validation call, LiteLLM
# x-api-key forward, claude model set) is Anthropic-only today, and accepting
# another provider's key would mint accounts whose every turn dead-ends.
#
# Created 2026-08-28 (feat/other-hand-byok).
#
# Two audiences, deliberately separated:
#
#   * The ROUTER calls ``get_status`` / ``set_key`` / ``delete_key``. None of
#     these ever produce plaintext — ``get_status`` answers from display-only
#     columns without decrypting at all.
#   * The TURN PATH calls ``resolve_turn_credentials``. That is the one function
#     that decrypts, it returns a value nothing serializes, and it is the seam
#     the paid tiers plug into later (see ``TurnCredentials`` below).
#
# WHY validate on save: a typo'd key that is stored happily fails at the user's
# first turn, in a place that looks like the product being broken rather than
# the credential being wrong. One cheap round trip at entry moves that error to
# where the user can act on it.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.byok.dto import ByokStatus
from pocketpaw_ee.cloud.models.byok_key import ByokProviderKey

logger = logging.getLogger(__name__)

_VALIDATE_URL = "https://api.anthropic.com/v1/messages"
_VALIDATE_TIMEOUT_S = 15.0
# The cheapest possible real call: one token out of the smallest model. A 200 or
# a 400 both prove the credential is good (400 == we were understood, then
# refused on content); only 401/403 prove it is not.
_VALIDATE_MODEL = "claude-haiku-4-5-20251001"

# The providers this deployment can actually spend a key against, end to end.
# v1 is Anthropic-only: ``validate_key`` calls Anthropic, the LiteLLM forward
# targets Anthropic upstreams, and the model set is claude-*. Widening this set
# means widening ALL THREE, not just this constant.
SUPPORTED_PROVIDERS = frozenset({"anthropic"})


@dataclass(frozen=True)
class TurnCredentials:
    """How ONE turn is paid for.

    The seam the captain's "same pipeline for people who want to purchase
    tokens" lands on. Today it resolves to exactly two shapes:

      * ``source="platform"`` — our own subscription/session credentials, i.e.
        the deployment's existing behaviour. ``api_key`` is None; the runtime
        keeps doing whatever it already did.
      * ``source="byok"`` — the workspace's own key. ``api_key`` is set, and the
        runtime MUST spawn with it and MUST NOT also pass platform credentials.

    A future ``source="plan_tokens"`` (Dodo-purchased balance) slots in here
    without re-plumbing the runtime: it is another way to answer the same
    question, "whose credential does this turn use?"
    """

    source: Literal["platform", "byok"]
    api_key: str | None = None
    provider: str = "anthropic"


async def get_status(workspace_id: str) -> ByokStatus:
    """What the UI may know. Never decrypts."""
    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        return ByokStatus(configured=False)
    return ByokStatus(
        configured=bool(doc.encrypted_key),
        provider=doc.provider,
        last4=doc.last4,
        key_hint=doc.key_hint,
        last_verified_at=doc.last_verified_at,
        last_error=doc.last_error,
        image_configured=bool(doc.image_encrypted_key),
        image_last4=doc.image_last4,
        image_key_hint=doc.image_key_hint,
        image_last_error=doc.image_last_error,
    )


async def validate_key(api_key: str) -> None:
    """Prove the key works, or raise ValidationError naming why.

    Network trouble is NOT a bad key: a timeout raises the transport error so
    the caller can decide, rather than telling the user their good key is bad.
    """
    payload = {
        "model": _VALIDATE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT_S) as client:
        resp = await client.post(_VALIDATE_URL, json=payload, headers=headers)

    if resp.status_code in (401, 403):
        raise ValidationError(
            "byok.key_rejected",
            "Anthropic rejected that key. Check you copied the whole key from "
            "console.anthropic.com, and that it has not been revoked.",
        )
    if resp.status_code == 429:
        raise ValidationError(
            "byok.key_rate_limited",
            "That key is rate-limited right now, so we could not verify it. Try again in a minute.",
        )
    if resp.status_code >= 500:
        raise ValidationError(
            "byok.provider_unavailable",
            "Anthropic did not respond. Your key was not saved — try again shortly.",
        )
    # Anything else (200, or a 400 about the tiny payload) means the credential
    # was accepted and the request was understood. That is what we are testing.


async def set_key(
    workspace_id: str,
    api_key: str,
    *,
    provider: str = "anthropic",
    user_id: str | None = None,
    validate: bool = True,
) -> ByokStatus:
    """Validate, then encrypt-and-upsert. Never stores an unverified key.

    ``validate=False`` (2026-09-01, feat/byok-guest-backend) is for callers
    that ALREADY ran ``validate_key`` in the same request — the guest-mint
    route validates before minting anything, and a second provider round trip
    would double the cost and the latency of every mint. Default True keeps
    every existing caller byte-identical; never pass False with a key that has
    not just been validated.
    """
    if not crypto.is_configured():
        raise ValidationError(
            "byok.encryption_unavailable",
            "This deployment cannot store provider keys — CLOUD_ENCRYPTION_KEY "
            "is not set. Contact the operator.",
        )

    if validate:
        await validate_key(api_key)

    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        doc = ByokProviderKey(
            workspace=workspace_id,
            provider=provider,
            encrypted_key=crypto.encrypt(api_key),
            last4=api_key[-4:],
            key_hint=_hint(api_key),
        )
    else:
        doc.provider = provider
        doc.encrypted_key = crypto.encrypt(api_key)
        doc.last4 = api_key[-4:]
        doc.key_hint = _hint(api_key)
    doc.set_by_user = user_id
    doc.last_verified_at = datetime.now(UTC)
    doc.last_error = None
    await doc.save()

    logger.info("byok: key set for workspace=%s provider=%s", workspace_id, provider)
    return await get_status(workspace_id)


async def delete_key(workspace_id: str) -> ByokStatus:
    """Remove the workspace's LLM key. Idempotent — deleting nothing is success.

    Clears the columns rather than dropping the row whenever an IMAGE key still
    lives on it. One row holds two independent credentials (2026-09-11), and
    removing one must never take the other with it — a user who rotates their
    Anthropic key would otherwise silently lose their illustrator.
    """
    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        return ByokStatus(configured=False)
    if doc.image_encrypted_key:
        doc.encrypted_key = ""
        doc.last4 = ""
        doc.key_hint = None
        doc.base_url = None
        doc.model = None
        doc.last_verified_at = None
        doc.last_error = None
        await doc.save()
    else:
        await doc.delete()
    logger.info("byok: key removed for workspace=%s", workspace_id)
    return await get_status(workspace_id)


async def record_auth_failure(workspace_id: str, message: str) -> None:
    """Mark a stored key as having failed, so the UI stops showing it as good.

    Called from the turn path when a BYOK spawn comes back with an auth error.
    Best-effort: never let bookkeeping fail a turn that already failed.
    """
    try:
        doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
        if doc is not None:
            doc.last_error = message[:300]
            await doc.save()
    except Exception:  # noqa: BLE001 — diagnostics must not mask the real error
        logger.warning("byok: could not record auth failure", exc_info=True)


async def resolve_turn_credentials(workspace_id: str | None) -> TurnCredentials:
    """Decide whose credential pays for this turn. THE ONLY DECRYPT SITE.

    Returns ``platform`` whenever there is no usable BYOK key, so every caller
    can treat this as "ask, then spawn" without branching on absence. An
    undecryptable row (the deployment's Fernet key rotated) also degrades to
    platform rather than failing the turn — the user re-enters their key from a
    working product, not from a broken one.
    """
    if not workspace_id:
        return TurnCredentials(source="platform")

    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None or not doc.encrypted_key:
        return TurnCredentials(source="platform")

    try:
        plaintext = crypto.decrypt(doc.encrypted_key)
    except ValidationError:
        logger.warning(
            "byok: stored key for workspace=%s is undecryptable; using platform "
            "credentials for this turn",
            workspace_id,
        )
        return TurnCredentials(source="platform")

    if not plaintext:
        return TurnCredentials(source="platform")
    return TurnCredentials(source="byok", api_key=plaintext, provider=doc.provider)


# ── The illustration credential (fal.ai) ────────────────────────────────────
#
# Added 2026-09-11 (feat/byok-image-key). Illustrations are generated through
# fal and billed per image on the PLATFORM's account, which is why guests are
# refused them outright: a guest can mint a fresh workspace for a fresh daily
# ceiling, so the cap alone left a bill attached to a signup form. A workspace
# that brings its own fal key pays for its own pictures, and that objection
# disappears.
#
# NOT validated on save. fal has no free endpoint that proves a key without
# generating an image, so a validate-on-save would spend money every time
# someone pasted one. ``record_image_auth_failure`` stamps the row the first
# time a generation is refused, which is the same signal one picture later.


def _fal_hint(api_key: str) -> str:
    """The non-secret half of a fal key.

    fal spells its credential ``<key-id>:<secret>``. The id is not secret and
    is what tells two keys apart; the secret is the part that must never be
    displayed. ``_hint`` above splits on ``-`` and would print three UUID
    segments of a fal key, so this one exists rather than reusing it.
    """
    return api_key.partition(":")[0] if ":" in api_key else ""


def _fal_last4(api_key: str) -> str:
    """Last four of the SECRET half, so two keys sharing an id still differ."""
    secret = api_key.partition(":")[2] if ":" in api_key else api_key
    return secret[-4:]


async def set_image_key(
    workspace_id: str,
    api_key: str,
    *,
    user_id: str | None = None,
) -> ByokStatus:
    """Encrypt-and-upsert the workspace's fal key. Never touches the LLM key."""
    if not crypto.is_configured():
        raise ValidationError(
            "byok.encryption_unavailable",
            "This deployment cannot store provider keys — CLOUD_ENCRYPTION_KEY "
            "is not set. Contact the operator.",
        )

    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        # A workspace may bring an image key and no LLM key at all — on a
        # platform-credential deployment that is the ordinary case. The row is
        # created with an EMPTY ``encrypted_key``, which ``get_status`` reads as
        # "no LLM key configured" and ``resolve_turn_credentials`` reads as
        # platform. Both already handle the empty string.
        doc = ByokProviderKey(workspace=workspace_id, encrypted_key="", last4="")

    doc.image_encrypted_key = crypto.encrypt(api_key)
    doc.image_last4 = _fal_last4(api_key)
    doc.image_key_hint = _fal_hint(api_key)
    doc.image_last_error = None
    doc.set_by_user = user_id or doc.set_by_user
    await doc.save()

    logger.info("byok: image key set for workspace=%s", workspace_id)
    return await get_status(workspace_id)


async def delete_image_key(workspace_id: str) -> ByokStatus:
    """Remove the fal key. Idempotent, and it never touches the LLM key.

    Unlike the LLM key, removing this one is SAFE and the UI offers it: a
    workspace with no image key falls back to today's behaviour (the platform
    key under the daily cap for accounts, a refusal for guests), where a
    workspace with no LLM key answers 402 on every turn.
    """
    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        return ByokStatus(configured=False)
    if doc.encrypted_key:
        doc.image_encrypted_key = None
        doc.image_last4 = None
        doc.image_key_hint = None
        doc.image_last_error = None
        await doc.save()
    else:
        # Nothing left on the row once the image key goes.
        await doc.delete()
        return ByokStatus(configured=False)
    logger.info("byok: image key removed for workspace=%s", workspace_id)
    return await get_status(workspace_id)


async def resolve_image_key(workspace_id: str | None) -> str | None:
    """The workspace's own fal key, or None. A DECRYPT SITE.

    Same degrade-to-None shape as ``resolve_turn_credentials``: an undecryptable
    row (the deployment's Fernet key rotated) reads as "no key", so the caller
    falls back to the platform's rather than failing. Never raises — an
    illustration is not worth breaking a turn over.
    """
    if not workspace_id:
        return None
    try:
        doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
        if doc is None or not doc.image_encrypted_key:
            return None
        return crypto.decrypt(doc.image_encrypted_key) or None
    except Exception:  # noqa: BLE001 — an unreadable key is a missing key
        logger.warning(
            "byok: stored image key for workspace=%s is unreadable; using the "
            "platform illustrator this time",
            workspace_id,
        )
        return None


async def record_image_auth_failure(workspace_id: str | None, message: str) -> None:
    """Mark the stored fal key as having failed, so the UI stops showing it good.

    This IS the verification story for this credential (see the header): there
    is no save-time check, so the first refused generation is where a bad key
    becomes visible. Best-effort — bookkeeping never fails the caller.
    """
    if not workspace_id:
        return
    try:
        doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
        if doc is not None and doc.image_encrypted_key:
            doc.image_last_error = message[:300]
            await doc.save()
    except Exception:  # noqa: BLE001
        logger.debug("byok: could not record image auth failure", exc_info=True)


#: Model names that mean "let the backend pick" — never a cross-provider pin.
_MODEL_SENTINELS = frozenset({"", "default", "auto", "inherit"})


def provider_allows_model(provider: str, model: str | None) -> bool:
    """Whether a turn pinned to ``provider``'s key may run ``model``.

    The BYOK model-pinning rule (feat/byok-guest-backend): when a turn runs on
    a stored key, the model must belong to that key's provider — an OpenAI key
    cannot run a Claude model. A mismatch must surface as a CLEAR error at the
    seam that decides, never as an upstream 401 that reads like the product
    being broken. Sentinel names ("default" etc.) always pass: the backend's
    own default is provider-correct by construction.

    Anything but ``anthropic`` returns False for every real model name — the
    pipeline cannot serve another provider end to end today (see
    ``SUPPORTED_PROVIDERS``), and a loud mismatch beats a silent dead turn.
    """
    m = (model or "").strip().lower()
    if m in _MODEL_SENTINELS:
        return True
    if provider == "anthropic":
        return m.startswith("claude") or m.startswith("anthropic/")
    return False


def _hint(api_key: str) -> str:
    """The non-secret prefix, e.g. 'sk-ant-api03'. Empty when the shape is odd."""
    parts = api_key.split("-")
    return "-".join(parts[:3]) if len(parts) >= 3 else ""


def build_settings_override(creds: TurnCredentials) -> dict[str, object]:
    """Turn resolved credentials into a ``create_isolated_backend`` override.

    The hosted cloud routes every turn through the LiteLLM proxy, and LiteLLM
    has its own BYOK path: with ``forward_llm_provider_auth_headers: true`` in
    the proxy's ``general_settings``, an ``x-api-key`` header on the request is
    forwarded upstream, so the user's own provider account is billed while the
    turn still passes through our routing, logging and guardrails.

    That is why this returns ``byok_provider_api_key`` rather than
    ``anthropic_api_key``: the key is a HEADER we hand the proxy, not a
    credential we use to call Anthropic ourselves. One pipeline, two payers —
    and the same pipeline a purchased-token balance would ride later.

    Operator note: the proxy config change is a DEPLOY step, not a code one.
    Without it LiteLLM strips ``x-api-key`` and every BYOK turn silently bills
    the platform. LiteLLM's own docs warn against forwarding client headers on a
    public API; that caveat is about untrusted callers reaching the proxy
    directly. Here the browser never sees the proxy — our server holds the key
    and sets the header — so the trust boundary is server-to-server.

    MUST be paired with ``AgentRouter.create_isolated_backend``, never with the
    pooled backend. ``AgentPool`` drives every session and surface through ONE
    cached instance; handing it a tenant's key would let the next tenant's turn
    run on that credential. (The pydantic_ai agent cache also keys on a
    credential fingerprint as a second line of defence, but the isolation is the
    real guarantee — do not rely on the cache key alone.)

    Returns an EMPTY dict for platform credentials, so the caller can pass it
    unconditionally and get today's behaviour when no key is configured.
    """
    if creds.source != "byok" or not creds.api_key:
        return {}
    return {"byok_provider_api_key": creds.api_key}
