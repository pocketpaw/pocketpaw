# ee/pocketpaw_ee/cloud/models/litellm_key.py — the per-tenant LiteLLM virtual-key
# mapping document (MCG-8, the per-tenant key + budget provisioning seam).
#
# One Beanie document backs the workspace -> LiteLLM virtual-key mapping:
#
#   * ``LiteLLMTenantKey`` — exactly one row per workspace (the ``workspace``
#     index is UNIQUE). Records the virtual key the LiteLLM proxy minted for the
#     tenant (via ``POST /key/generate``), plus the budget / rpm / tpm / allowed
#     models the key was provisioned with, and the high-water mark of the last
#     spend log row already ingested into the credit ledger (so spend ingestion
#     never re-reads the same rows). The provisioning service is idempotent: it
#     upserts on ``workspace`` and skips the proxy ``/key/generate`` call when a
#     row with a live key already exists, so a double-provision is a no-op.
#
# WHY a separate doc (not a field on Workspace / WorkspaceSettings): RFC 03 keeps
# domain-specific config in domain-owned docs. Only ``ee.cloud.llm_provisioning.
# service`` reads/writes this document — the same entity-isolation boundary the
# credit / foresight docs use (one service owns one doc family). The key itself
# is a proxy-scoped virtual key (NOT a provider API key) — its blast radius is the
# budget + allowed-models the proxy enforces, so it is stored as-is rather than
# encrypted-at-rest like a raw provider credential; tightening this to the
# secret-store path is a noted follow-up.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): new entity. Registered
# in ``cloud.models.__init__`` (``get_all_documents()`` + ``__all__``) so
# ``init_beanie`` wires the ``litellm_tenant_keys`` collection.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class LiteLLMTenantKey(TimestampedDocument):
    """The LiteLLM virtual key minted for one workspace (tenant).

    Exactly one row per workspace (the ``workspace`` index is UNIQUE). The
    provisioning service upserts on this key and short-circuits when a live key
    already exists, so provisioning is idempotent — a workspace never gets two
    proxy keys.

    ``litellm_key`` is the proxy virtual key (the ``key`` field returned by
    ``POST /key/generate``); ``key_alias`` is the human-readable alias we asked
    the proxy to stamp on it (``ws-<workspace>``). The budget / limit columns
    record what the key was provisioned WITH (so a later re-provision can detect
    drift); they are NOT the live proxy state — the proxy is the source of truth
    for current spend, read back via ``GET /key/info``.

    ``last_spend_ingest_ts`` is the high-water mark: the ``startTime`` of the most
    recent ``/spend/logs`` row already debited to the credit ledger. The spend
    sweep reads only rows newer than this, then advances it — so each spend row is
    ingested at most once even before BC-1's idempotency key is consulted (the key
    is the real exactly-once guard; the high-water mark just bounds the read).
    """

    # UNIQUE — one virtual key per workspace. The provisioning upsert keys on it.
    workspace: Indexed(str, unique=True)  # type: ignore[valid-type]
    # The proxy virtual key (the ``key`` from POST /key/generate). Sent as the
    # Bearer token on this tenant's proxy calls so the proxy attributes spend +
    # enforces the budget.
    litellm_key: str
    # The alias the proxy stamped on the key (``ws-<workspace>``) — for operator
    # legibility in the proxy admin UI / spend logs.
    key_alias: str | None = None
    # What the key was provisioned WITH (provenance, NOT the live proxy state):
    #   * ``max_budget_usd`` — the USD budget ceiling the proxy enforces over
    #     ``budget_duration`` (None == unlimited; we always set one from config).
    #   * ``budget_duration`` — the reset window, a LiteLLM duration string
    #     (e.g. "30d", "1mo"). None == no reset.
    #   * ``rpm_limit`` / ``tpm_limit`` — requests / tokens-per-minute caps
    #     (None == unlimited).
    #   * ``models`` — the allowed-model allowlist the key may route to (empty ==
    #     all models the proxy serves).
    max_budget_usd: float | None = None
    budget_duration: str | None = None
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    models: list[str] = []  # noqa: RUF012 — Beanie field default, not a shared mutable
    # High-water mark for spend ingestion: the ISO ``startTime`` of the newest
    # /spend/logs row already debited to the ledger. The sweep reads rows AFTER
    # this and advances it; None means nothing has been ingested yet.
    last_spend_ingest_ts: str | None = None

    class Settings:
        name = "litellm_tenant_keys"
