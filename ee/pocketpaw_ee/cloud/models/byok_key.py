# ee/pocketpaw_ee/cloud/models/byok_key.py — the per-workspace BYOK provider
# credential ("bring your own key").
#
# One Beanie document backs the workspace -> provider-key mapping:
#
#   * ``ByokProviderKey`` — at most one row per workspace (the ``workspace``
#     index is UNIQUE). Holds the user's OWN Anthropic API key, so their turns
#     bill to their Anthropic account instead of ours. Set/replace/delete is
#     idempotent: the service upserts on ``workspace``.
#
# ENCRYPTED AT REST, unlike the sibling ``LiteLLMTenantKey``. That doc stores
# its key as-is and says why: a LiteLLM virtual key is proxy-scoped, so its
# blast radius is the budget + allowed-models the proxy enforces. THIS key is a
# raw provider credential with none of that containment — a leak is the user's
# whole Anthropic account. It goes through ``cloud._core.crypto`` (deployment
# Fernet envelope), and the plaintext never leaves the service that decrypts it.
#
# ``last4`` and ``key_hint`` exist so the UI can show WHICH key is configured
# without the service ever decrypting to answer a status call. Status reads must
# never touch ``encrypted_key``.
#
# WHY a separate doc (not a field on Workspace / WorkspaceSettings): RFC 03 keeps
# domain-specific config in domain-owned docs, and this one has the strictest
# read rule in the codebase — exactly one service decrypts it, and only on a
# path that is about to spend the credential. Folding it into a broadly-read doc
# would put a live provider credential inside every workspace fetch.
#
# Created 2026-08-28 (feat/other-hand-byok): new entity. Registered in
# ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``byok_provider_keys`` collection.
#
# Updated 2026-09-09 (feat/byok-custom-gateway): two nullable columns,
# ``base_url`` and ``model``, so a key can belong to an OpenAI-compatible
# gateway (Experiential Labs, OpenRouter, LiteLLM, vLLM) instead of Anthropic.
# Both are NON-SECRET and are safe to return in ``ByokStatus`` — the whole
# point of a gateway is that its address is public. Existing anthropic rows
# read back as None on both, so no migration.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ByokProviderKey(TimestampedDocument):
    """One workspace's own provider API key, encrypted at rest.

    At most one row per workspace (the ``workspace`` index is UNIQUE). Setting a
    key upserts; there is no history, because a replaced credential is one the
    user wants gone.

    ``encrypted_key`` is a ``cloud._core.crypto`` Fernet token, NEVER plaintext
    and NEVER serialized into an API response. The only legitimate reader is
    ``cloud.byok.service.resolve_plaintext_key`` on the turn path.
    """

    # UNIQUE — one BYOK credential per workspace. The set/delete upsert keys on it.
    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    # Which provider the key belongs to. Only "anthropic" is accepted today; the
    # column exists so adding a second provider is a value, not a migration.
    provider: str = "anthropic"
    # Where the key spends, for an ``openai_compatible`` provider only. Always
    # None for anthropic (the endpoint is api.anthropic.com by definition).
    # Validated at the DTO edge with ``validate_external_url_strict`` — https
    # only, no loopback or private ranges — because this URL is a stranger's
    # input that our server then makes requests to.
    base_url: str | None = None
    # The gateway's own model id, e.g. "claude-opus-5" on Experiential Labs.
    # A gateway's ids are its own, so there is no list to pick from.
    model: str | None = None
    # Fernet token from cloud._core.crypto.encrypt(). Never returned by the API.
    encrypted_key: str
    # Display-only provenance so status calls never decrypt:
    #   * ``last4``     — the final 4 characters, to tell two keys apart.
    #   * ``key_hint``  — the provider's key prefix (e.g. "sk-ant-api03"), which
    #     is not secret and makes a pasted-the-wrong-thing mistake obvious.
    last4: str
    key_hint: str | None = None
    # Who last set it, and when it was last known good. ``last_verified_at`` is
    # stamped by the validation call the service makes on save — a key that has
    # never verified is one we should not silently route a turn through.
    set_by_user: str | None = None
    last_verified_at: datetime | None = None
    # Set when a turn fails with an auth error, so the UI can say "your key
    # stopped working" instead of showing a green state over a dead credential.
    last_error: str | None = None

    # -- the illustration credential (fal.ai) --------------------------------
    # Independent of the LLM key above: a workspace may have either, both, or
    # neither, and setting one never touches the other. Absent on every existing
    # row, which reads as "no image key" — today's behaviour exactly.
    image_encrypted_key: str | None = None
    # Display-only, same contract as ``last4`` / ``key_hint``: a status call
    # answers from these and never decrypts. A fal key is ``<key-id>:<secret>``,
    # so the hint is the key id (not secret) and last4 comes from the secret.
    image_last4: str | None = None
    image_key_hint: str | None = None
    # Stamped when a generation comes back 401/403, cleared on the next success.
    # This is the whole verification story for this key — see the header.
    image_last_error: str | None = None

    class Settings:
        name = "byok_provider_keys"
