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
# read rule in the codebase — exactly one service decrypts it, on the turn path.
# Folding it into a broadly-read doc would put a live provider credential inside
# every workspace fetch.
#
# Created 2026-08-28 (feat/other-hand-byok): new entity. Registered in
# ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``byok_provider_keys`` collection.

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

    class Settings:
        name = "byok_provider_keys"
