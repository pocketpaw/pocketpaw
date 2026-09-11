# ee/pocketpaw_ee/cloud/byok/service.py — the only reader of ByokProviderKey.
#
# Updated 2026-09-11 (feat/byok-image-key), on review:
#
#   * ``delete_key`` clears the gateway columns only when the document
#     declares them. They arrive with the sibling feat/byok-custom-gateway
#     branch, and assigning a field pydantic does not know about raises — so
#     the unguarded version failed every delete on a row that also held an
#     image key, which is the exact case this function exists to handle.
#   * Provider error text goes through ``_safe_provider_error`` before it is
#     stored. ``last_error`` / ``image_last_error`` are returned by the status
#     API and rendered verbatim, and the text is written by whoever refused
#     the call.
#
# Updated 2026-09-01 (feat/byok-guest-backend): ``set_key`` gained
# ``validate: bool = True`` so the guest-mint route (which validates BEFORE
# minting anything) can store without a second provider round trip. Also added
# ``SUPPORTED_PROVIDERS`` — the turn pipeline (validation call, LiteLLM
# x-api-key forward, claude model set) is Anthropic-only today, and accepting
# another provider's key would mint accounts whose every turn dead-ends.
#
# Updated 2026-09-09 (feat/byok-custom-gateway): ``openai_compatible`` joins
# ``anthropic`` in ``SUPPORTED_PROVIDERS``. All three things the old comment
# said had to widen together did widen: ``validate_key`` now calls the
# gateway's own ``/chat/completions`` instead of Anthropic, the override
# builder points the runtime's ``openai_compatible`` provider at the gateway
# instead of forwarding an ``x-api-key`` through LiteLLM, and
# ``provider_allows_model`` stops second-guessing model names it cannot know.
#
# A gateway turn therefore does NOT go through the LiteLLM proxy: the runtime
# talks to the user's base URL directly. That is the price of accepting any
# base URL, and it costs the proxy's spend log and guardrails for those turns.
#
# Updated 2026-09-11 (review B1/B2/S6/S7): the gateway base URL is an SSRF
# boundary and was only shape-checked. ``validate_external_url_strict`` blocks
# internal hosts written as IP LITERALS and says in its own helper's docstring
# that resolving a name is the caller's job — and no caller resolved. So
# ``https://10-0-0-5.nip.io/v1`` passed both write paths, and ``POST
# /auth/guest`` puts that behind no authentication at all. Now:
#
#   * ``assert_gateway_egress`` runs the real egress guard (DNS + every
#     resolved address checked, internal rejected unconditionally) and the
#     validation request goes out pinned to the vetted IP with redirects off.
#   * ``resolve_turn_credentials`` re-runs it, because a row stored before this
#     check, or a host whose DNS moved afterwards, is dialed on every turn.
#     It REFUSES the turn rather than falling back to platform credentials —
#     falling back would spend our money on the tenant's broken config.
#   * ``validate_key`` returns the canonical URL so callers store the string
#     that passed the guard rather than the one that arrived.
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
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx

from pocketpaw.security.redact import redact_output

if TYPE_CHECKING:
    from pocketpaw.security.url_validators import EgressTarget

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
# Each entry needs all three of: a ``validate_key`` branch that proves the
# credential, a ``build_settings_override`` branch that points the runtime at
# it, and a ``provider_allows_model`` rule. Adding a name here without those is
# how you mint accounts whose every turn dead-ends.
SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai_compatible"})


class GatewayEgressRejected(ValidationError):
    """A gateway base URL does not pass the egress guard.

    A ``ValidationError`` subclass, so the two write paths keep answering 422
    with ``byok.base_url_rejected`` exactly as before. It is a distinct TYPE
    because the turn path has to tell this apart from everything else that can
    go wrong while resolving credentials: a generic failure there degrades to
    platform billing, and doing that here would spend OUR money running a turn
    whose stored address points somewhere it must not. The turn is refused
    instead — see ``resolve_turn_credentials``.
    """

    def __init__(self, message: str) -> None:
        super().__init__("byok.base_url_rejected", message)


async def assert_gateway_egress(base_url: str) -> EgressTarget:
    """Normalize a gateway base URL and prove it does not point inside.

    Two layers, and the second one is the point:

    * ``validate_external_url_strict`` — the cheap shape check the DTO edge
      also runs: https only, non-empty, and internal hosts written as IP
      LITERALS blocked. No DNS, so it is free.
    * ``assert_egress_allowed`` — resolves the hostname and rejects EVERY
      resolved address that is internal, then returns the single IP the
      request must dial.

    The shape check alone was the B1 hole. ``host_is_internal`` says so in its
    own docstring — "a bare hostname (not an IP literal) returns False — name
    resolution is the caller's job" — and no caller resolved, so
    ``https://10-0-0-5.nip.io/v1`` (or any attacker-owned name with an A record
    in RFC1918) passed both write paths and the server then POSTed to it. The
    status codes ``_validate_gateway_key`` maps back are a five-way response
    oracle, and ``POST /auth/guest`` is unauthenticated, so a stranger picked
    the target.

    ``allow_internal=False`` is passed explicitly rather than inherited.
    ``assert_egress_allowed`` otherwise honours ``POCKETPAW_ALLOW_INTERNAL_URLS``,
    and an operator who set that so localhost connectors keep working would
    reopen this boundary — which is the one boundary here a signed-out stranger
    can reach.

    The allow-list is the URL's own hostname. There is no registry of gateways
    a user may name (that is the whole feature), so what this call wants from
    the guard is the resolution and the pin, not a membership test.

    Returns the ``EgressTarget``: ``.url`` is the canonical (stripped,
    slash-trimmed) URL to store and request against, ``.pinned_ip`` the address
    ``PinnedTransport`` must dial so DNS cannot be re-resolved between the check
    and the connect.
    """
    from pocketpaw.security.url_validators import (
        EgressError,
        assert_egress_allowed,
        validate_external_url_strict,
    )

    try:
        base = validate_external_url_strict((base_url or "").strip().rstrip("/"))
    except ValueError as exc:
        raise GatewayEgressRejected(str(exc)) from exc

    host = urlsplit(base).hostname or ""
    try:
        return await assert_egress_allowed(base, {host}, allow_internal=False)
    except EgressError as exc:
        raise GatewayEgressRejected(
            f"That gateway address is not reachable from here: {exc}"
        ) from exc


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
    #: Only set for ``provider="openai_compatible"`` — where the key spends,
    #: and which model id that gateway knows it by.
    base_url: str | None = None
    model: str | None = None


async def get_status(workspace_id: str) -> ByokStatus:
    """What the UI may know. Never decrypts."""
    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        return ByokStatus(configured=False)
    return ByokStatus(
        configured=bool(doc.encrypted_key),
        provider=doc.provider,
        base_url=doc.base_url,
        model=doc.model,
        last4=doc.last4,
        key_hint=doc.key_hint,
        last_verified_at=doc.last_verified_at,
        last_error=doc.last_error,
        image_configured=bool(doc.image_encrypted_key),
        image_last4=doc.image_last4,
        image_key_hint=doc.image_key_hint,
        image_last_error=doc.image_last_error,
    )


async def validate_key(
    api_key: str,
    *,
    provider: str = "anthropic",
    base_url: str | None = None,
    model: str | None = None,
) -> str | None:
    """Prove the key works, or raise ValidationError naming why.

    Network trouble is NOT a bad key: a timeout raises the transport error so
    the caller can decide, rather than telling the user their good key is bad.

    A gateway (``provider="openai_compatible"``) is checked against its own
    ``/chat/completions`` with the model the user gave, because that pair is
    what a turn will actually use. Checking only the key would let a wrong
    model id through to fail on the first turn, which is the failure mode this
    whole function exists to move earlier.

    RETURNS the canonical base URL for a gateway, ``None`` for anthropic
    (2026-09-11, review S6). Callers must store the returned value, not the one
    they passed in: this function normalizes (``strip().rstrip("/")``) before
    guarding, and the guest-mint path used to throw that away and write the raw
    body value. Today the delta is only whitespace and a trailing slash, which
    is exactly the size of gap that a later normalization change turns into a
    stored value that never passed a guard.
    """
    if provider == "openai_compatible":
        return await _validate_gateway_key(api_key, base_url or "", model or "")

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
    return None


async def _validate_gateway_key(api_key: str, base_url: str, model: str) -> str:
    """Same proof, against an OpenAI-compatible gateway the user named.

    The URL passed ``validate_external_url_strict`` at the DTO edge, but that
    is a shape check only. The full egress guard runs HERE because this is the
    one point both write paths share: the guest-mint route's
    ``_GuestMintRequest`` has plain ``str`` fields and never touches the DTO,
    so on the unauthenticated path this is the only guard there is.

    The request then goes out through ``PinnedTransport``, dialing the exact IP
    the guard vetted, with redirects off. Without the pin, DNS is resolved a
    second time when the connection opens and a rebinding host can answer with
    an internal address in the gap (the TOCTOU the guard exists to close).
    Redirects are pinned OFF rather than inherited from httpx's default: a
    cooperating gateway could otherwise bounce the request onto an internal
    host, and that is a security property, not a default worth inheriting.

    Returns the canonical base URL — the caller stores THIS, not its input.
    """
    from pocketpaw.security.url_validators import PinnedTransport

    target = await assert_gateway_egress(base_url)
    base = target.url

    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=_VALIDATE_TIMEOUT_S,
        follow_redirects=False,
        transport=PinnedTransport(target.pinned_ip),
    ) as client:
        resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)

    if resp.status_code == 401:
        # Measured against api.experientiallabs.ai on 2026-09-09: a base URL
        # missing its version path answers 401, not 404, because auth runs
        # before routing. So the honest 404 hint below never fires for the
        # single most likely mistake, and it has to be said here instead.
        if not base.rstrip("/").rpartition("//")[2].partition("/")[2]:
            raise ValidationError(
                "byok.base_url_rejected",
                f"{base} rejected the key, and it has no path — most gateways "
                "serve this at /v1. Try adding it before re-checking the key.",
            )
        raise ValidationError(
            "byok.key_rejected",
            "That gateway rejected the key. Check you copied the whole key, and "
            "that it has not been revoked.",
        )
    if resp.status_code == 403:
        # A gateway commonly answers 403 for a model the key may not use, or a
        # model id in the wrong shape. Saying "bad key" here sends the user to
        # re-copy a key that was fine.
        raise ValidationError(
            "byok.key_rejected",
            f"The gateway refused '{model}' with that key. Check the model id is "
            "one your gateway serves and that your key is allowed to use it.",
        )
    if resp.status_code == 404:
        raise ValidationError(
            "byok.base_url_rejected",
            f"Nothing answered at {base}/chat/completions. The base URL usually "
            "needs to end in /v1.",
        )
    if resp.status_code == 429:
        raise ValidationError(
            "byok.key_rate_limited",
            "That key is rate-limited right now, so we could not verify it. Try again in a minute.",
        )
    if resp.status_code >= 500:
        raise ValidationError(
            "byok.provider_unavailable",
            "The gateway did not respond. Your key was not saved — try again shortly.",
        )
    return base


async def set_key(
    workspace_id: str,
    api_key: str,
    *,
    provider: str = "anthropic",
    base_url: str | None = None,
    model: str | None = None,
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
        # Store what passed the guard, not what arrived (review S6). The
        # validator normalizes before checking, and writing the un-normalized
        # copy is the shape a future normalization change turns into a hole.
        canonical = await validate_key(api_key, provider=provider, base_url=base_url, model=model)
        if canonical:
            base_url = canonical

    # An anthropic row never carries a gateway address. Writing one through
    # would leave a stale URL behind after a switch back, and the resolver
    # reads both columns.
    if provider != "openai_compatible":
        base_url = None
        model = None

    doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
    if doc is None:
        doc = ByokProviderKey(
            workspace=workspace_id,
            provider=provider,
            base_url=base_url,
            model=model,
            encrypted_key=crypto.encrypt(api_key),
            last4=api_key[-4:],
            key_hint=_hint(api_key),
        )
    else:
        doc.provider = provider
        doc.base_url = base_url
        doc.model = model
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
        doc.last_verified_at = None
        doc.last_error = None
        # ``base_url`` / ``model`` belong to the gateway credential that
        # feat/byok-custom-gateway adds to this row. That branch is a SIBLING
        # of this one, so the columns are absent until it merges — and
        # assigning a field the document does not declare RAISES in pydantic,
        # which would fail this delete for every workspace that also has an
        # image key. Ask the document instead of assuming, so the clear is
        # correct on either side of that merge.
        for gateway_field in ("base_url", "model"):
            if gateway_field in type(doc).model_fields:
                setattr(doc, gateway_field, None)
        await doc.save()
    else:
        await doc.delete()
    logger.info("byok: key removed for workspace=%s", workspace_id)
    return await get_status(workspace_id)


#: How much provider error text is worth keeping. Long enough to say what went
#: wrong, short enough that a stack trace or an echoed request body does not
#: end up in a settings panel.
_PROVIDER_ERROR_MAX_LEN = 300


def _safe_provider_error(message: str) -> str:
    """Provider error text, fit to be stored and handed back to the client.

    ``last_error`` / ``image_last_error`` are returned by ``ByokStatus`` and
    rendered verbatim in the settings panel, and the text comes from whoever
    refused the call — fal, Anthropic, or a gateway the workspace named. Some
    providers echo the submitted credential back in their error body, so this
    goes through the same redactor the agent's output does before it is stored.

    Redact THEN truncate: truncating first can cut a key in half and leave a
    remnant no pattern matches.
    """
    return redact_output(message)[:_PROVIDER_ERROR_MAX_LEN]


async def record_auth_failure(workspace_id: str, message: str) -> None:
    """Mark a stored key as having failed, so the UI stops showing it as good.

    Called from the turn path when a BYOK spawn comes back with an auth error.
    Best-effort: never let bookkeeping fail a turn that already failed.
    """
    try:
        doc = await ByokProviderKey.find_one(ByokProviderKey.workspace == workspace_id)
        if doc is not None:
            doc.last_error = _safe_provider_error(message)
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

    ONE case raises instead of degrading: a gateway row whose base URL no
    longer passes the egress guard (review B2). Write-time validation is not a
    runtime guard — a row stored before this check existed, or a host whose DNS
    moved inside afterwards, would otherwise be dialed on every turn, with the
    upstream's answer flowing back to the user. It raises
    ``GatewayEgressRejected`` rather than returning ``platform`` DELIBERATELY:
    degrading would run the tenant's turn on our credential, so a tenant's
    broken (or hostile) configuration would spend our money. Refuse the turn.
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

    base_url = doc.base_url
    if doc.provider == "openai_compatible":
        # Re-guard at turn time, not just at write time. Raises
        # ``GatewayEgressRejected``; see this function's docstring for why that
        # is a raise and not a degrade-to-platform.
        #
        # ponytail: this vets the address, it does not pin the connection —
        # the runtime's own OpenAI client resolves the hostname again when it
        # dials, so a host that rebinds between here and the connect is still
        # open. Closing it means plumbing ``target.pinned_ip`` through the
        # settings override into the backend's transport, which is a change to
        # the runtime rather than to this seam. The un-resolved-at-all hole
        # (B1) is what this closes.
        base_url = (await assert_gateway_egress(base_url or "")).url

    return TurnCredentials(
        source="byok",
        api_key=plaintext,
        provider=doc.provider,
        base_url=base_url,
        model=doc.model,
    )


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
            doc.image_last_error = _safe_provider_error(message)
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

    ``openai_compatible`` always passes, and deliberately: a gateway's model
    ids are its own namespace, so there is no name shape we could check
    against. The gateway's own refusal is the only honest judge.

    On what the turn actually runs (corrected 2026-09-11, review S7). The old
    wording here claimed the turn "is pinned to the stored model anyway", as if
    that were structural. It is not. ``run_core`` asks this function about
    ``ctx.model_override or agent_model``, and ``AgentPool.run`` forwards a
    non-None ``model_override`` to WHATEVER backend is running
    (``agents/pool.py``) — the per-send chip in paw-enterprise #902 and the
    ``model_override`` field in #2140 both put a client-chosen name on that
    wire. What makes the stored model win on a gateway turn is narrower: a
    gateway turn runs on the pydantic_ai backend, and that backend declares
    ``model_override`` only to ignore it (``agents/pydantic_ai.py``, the
    ``# noqa: ARG002`` block — "consumed only by the Claude SDK backend"), so
    the model that runs is the ``pydantic_ai_model`` this service pinned in
    ``build_settings_override``.

    So the effect holds today, but it rests on one backend's deliberate
    ignore, not on an invariant anything enforces. If pydantic_ai ever honours
    ``model_override``, a client could name a model this function never
    checked. No allowlist is added here on purpose — #2140 owns the per-send
    model surface and is where a real check belongs; the consequence meanwhile
    is confined to the tenant's own key against the tenant's own gateway.

    Any other provider returns False for every real model name — the pipeline
    cannot serve it end to end (see ``SUPPORTED_PROVIDERS``), and a loud
    mismatch beats a silent dead turn.
    """
    m = (model or "").strip().lower()
    if m in _MODEL_SENTINELS:
        return True
    if provider == "anthropic":
        return m.startswith("claude") or m.startswith("anthropic/")
    if provider == "openai_compatible":
        return True
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

    A GATEWAY key (``provider="openai_compatible"``, 2026-09-09) takes the
    other road. There is no LiteLLM model group pointing at a URL one guest
    typed, so forwarding a header would send the turn to whatever LiteLLM was
    already configured for and bill the wrong account. Instead the override
    points the runtime's own ``openai_compatible`` provider straight at the
    gateway. Both ``pydantic_ai_provider`` and ``pydantic_ai_model`` are
    pinned: the provider setting alone leaves ``pydantic_ai_model`` naming
    whatever the agent was configured with, and that name wins over
    ``openai_compatible_model`` in ``_parse_provider_model``.

    The cost, stated plainly: a gateway turn does not pass through the LiteLLM
    proxy, so it produces no spend-log row and skips the proxy's guardrails.
    The isolation requirement is unchanged and matters just as much — these
    settings are as tenant-specific as the key.
    """
    if creds.source != "byok" or not creds.api_key:
        return {}
    if creds.provider == "openai_compatible":
        return {
            "pydantic_ai_provider": "openai_compatible",
            "pydantic_ai_model": creds.model or "",
            "llm_provider": "openai_compatible",
            "openai_compatible_base_url": creds.base_url or "",
            "openai_compatible_api_key": creds.api_key,
            "openai_compatible_model": creds.model or "",
        }
    return {"byok_provider_api_key": creds.api_key}
