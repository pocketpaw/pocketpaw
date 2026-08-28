# ee/pocketpaw_ee/cloud/byok/service.py — the only reader of ByokProviderKey.
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
        configured=True,
        provider=doc.provider,
        last4=doc.last4,
        key_hint=doc.key_hint,
        last_verified_at=doc.last_verified_at,
        last_error=doc.last_error,
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
            "That key is rate-limited right now, so we could not verify it. "
            "Try again in a minute.",
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
) -> ByokStatus:
    """Validate, then encrypt-and-upsert. Never stores an unverified key."""
    if not crypto.is_configured():
        raise ValidationError(
            "byok.encryption_unavailable",
            "This deployment cannot store provider keys — CLOUD_ENCRYPTION_KEY "
            "is not set. Contact the operator.",
        )

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
    """Remove the workspace's key. Idempotent — deleting nothing is success."""
    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is not None:
        await doc.delete()
        logger.info("byok: key removed for workspace=%s", workspace_id)
    return ByokStatus(configured=False)


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


def _hint(api_key: str) -> str:
    """The non-secret prefix, e.g. 'sk-ant-api03'. Empty when the shape is odd."""
    parts = api_key.split("-")
    return "-".join(parts[:3]) if len(parts) >= 3 else ""


def build_settings_override(creds: TurnCredentials) -> dict[str, object]:
    """Turn resolved credentials into a ``create_isolated_backend`` override.

    The hosted cloud runs the ``pydantic_ai`` backend, which reads its provider
    credential straight off ``settings.anthropic_api_key`` — so BYOK needs no
    backend-specific plumbing, only a settings copy with the tenant's key in it.

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
    return {"anthropic_api_key": creds.api_key}
