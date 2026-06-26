# ee/pocketpaw_ee/cloud/llm_provisioning/service.py — the per-tenant LiteLLM
# virtual-key lifecycle (MCG-8). Module-level ``async def`` API (NOT a class, per
# the EE cloud rule, mirroring ``credits.service`` / ``metering.service``). Sole
# owner of writes to the ``LiteLLMTenantKey`` doc (entity isolation — only THIS
# module imports ``models.litellm_key``).
#
# Two jobs:
#
#   1. PROVISIONING (``ensure_tenant_key``) — idempotently ensure a budgeted,
#      rate-limited LiteLLM virtual key exists for a workspace. Mints one via the
#      proxy admin API (POST /key/generate, with metadata={workspace_id}) the
#      FIRST time, persists the workspace -> key mapping (upsert on the unique
#      ``workspace`` index), and on every later call returns the stored key
#      WITHOUT a second proxy call. The budget / rpm / tpm / allowed-models come
#      from runtime settings (``load_key_budget``) — config-driven, never
#      hardcoded. ``get_tenant_key`` reads the key back for spend attribution on
#      the tenant's proxy calls.
#
#   2. SPEND INGESTION (``ingest_tenant_spend``) — read the tenant key's proxy
#      spend (GET /spend/logs?api_key=<key>) and feed it into the EXISTING credit
#      ledger via ``credits.service.debit``. This entity does NOT own a ledger; it
#      plugs into BC-1's. Each spend row is debited EXACTLY ONCE: the debit is
#      keyed ``litellm:{request_id}`` against BC-1's unique
#      ``(workspace, idempotency_key)`` index (the real guard), and the
#      ``last_spend_ingest_ts`` high-water mark bounds the read so a re-sweep
#      doesn't re-read settled rows. ``allow_negative=True`` + a DISTINCT cause
#      (``litellm_spend``) — proxy compute already happened, so it bills fully,
#      and the distinct cause keeps these movements separable from BC-3's
#      ``compute_spend`` rows on the dashboard.
#
# DOUBLE-BILL BOUNDARY (read this before enabling ingestion): the proxy's
# /spend/logs includes EVERY call routed through the proxy — including the text
# chat runs BC-3 metering already bills per ``ChatRunDoc`` (keyed ``run:{run_id}``,
# cause ``compute_spend``). Running BOTH unconditionally would double-bill text
# chat. So spend ingestion is GATED OFF by default behind
# ``settings.litellm_spend_ingest_enabled`` (POCKETPAW_LITELLM_SPEND_INGEST,
# default False). It is the future single-source-of-truth path — bill ALL compute
# from proxy spend and retire per-run metering — but flipping that seam (and the
# row-level dedup against BC-3, e.g. skipping rows whose metadata carries a
# ``run_id`` already billed) is a deliberate product decision, NOT a default. The
# PROVISIONING half is always-on and unconditional; only the ingestion half is
# gated. Today's live, attributed path is media (it tags ``user=workspace_id`` so
# the proxy logs spend per tenant) once provisioning hands media the tenant key.
#
# Rule 6 — validate at entry (a workspace id is required). Rule 7 — every read is
# tenant-filtered on ``workspace``. Rule 10 — only ``CloudError`` subclasses
# propagate to HTTP; a proxy admin failure raises ``LiteLLMAdminError`` (a plain
# exception) which a system-job caller logs + retries, never a bare HTTPException.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): new entity.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.catalog.admin_client import LiteLLMAdminClient, LiteLLMAdminError
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.llm_provisioning.domain import (
    KeyBudget,
    ProvisionResult,
    SpendCredits,
    SpendIngestResult,
)
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey

logger = logging.getLogger(__name__)

# Business cause stamped on the ledger movement for proxy-attributed spend. KEPT
# DISTINCT from BC-3 metering's ``compute_spend`` so a dashboard / audit can tell
# proxy-gateway spend apart from per-run metered spend (and so the two paths can
# coexist without their idempotency namespaces colliding).
_LITELLM_SPEND_CAUSE = "litellm_spend"

# The key-alias prefix the proxy stamps on a tenant's virtual key, for operator
# legibility in the proxy admin UI + spend logs.
_KEY_ALIAS_PREFIX = "ws-"


# ---------------------------------------------------------------------------
# Config — budgets + the spend rate card from runtime settings (NEVER hardcoded).
# ---------------------------------------------------------------------------


def load_key_budget() -> KeyBudget:
    """Build the per-tenant key budget from runtime settings.

    Reads (all with sane defaults so a fresh deploy provisions a sensible key):
      * POCKETPAW_TENANT_MAX_BUDGET_USD  — USD ceiling per ``budget_duration``.
      * POCKETPAW_TENANT_BUDGET_DURATION — reset window (LiteLLM duration string).
      * POCKETPAW_TENANT_RPM_LIMIT       — requests-per-minute cap (0 == unset).
      * POCKETPAW_TENANT_TPM_LIMIT       — tokens-per-minute cap (0 == unset).

    The allowed-models allowlist defaults to empty (all models the proxy serves) —
    a deployment that wants to restrict a tenant can layer that on later without
    changing the mint path. Lazy import so the entity has no import-time dependency
    on the settings singleton.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    rpm = int(settings.tenant_rpm_limit)
    tpm = int(settings.tenant_tpm_limit)
    budget = float(settings.tenant_max_budget_usd)
    return KeyBudget(
        max_budget_usd=budget if budget > 0 else None,
        budget_duration=settings.tenant_budget_duration or None,
        rpm_limit=rpm if rpm > 0 else None,
        tpm_limit=tpm if tpm > 0 else None,
        models=[],
    )


def load_spend_credits() -> SpendCredits:
    """Build the proxy-spend rate card from runtime settings.

    Reuses the SAME ``billing_markup`` / ``credit_usd`` settings the BC-3 meter
    uses (``metering.service.load_rate_card``) so a dollar of compute bills the
    same credits regardless of which path attributes it. Lazy import for the same
    reason as above.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    return SpendCredits(
        markup=float(settings.billing_markup), credit_usd=float(settings.credit_usd)
    )


def spend_ingest_enabled() -> bool:
    """Whether the proxy-spend -> credits sweep is enabled for this deployment.

    Default FALSE — see the DOUBLE-BILL BOUNDARY note in the module header.
    Provisioning is unaffected by this flag (always on); only ingestion is gated.
    """
    from pocketpaw.config import get_settings

    return bool(get_settings().litellm_spend_ingest_enabled)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _require_workspace(workspace: str) -> None:
    if not workspace:
        raise ValidationError("llm_provisioning.invalid_workspace", "workspace is required")


def _num(value: Any) -> float:
    """Coerce a proxy spend value to a float, tolerating None / strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    """Coerce a token count to a non-negative int, tolerating None / strings."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


# ---------------------------------------------------------------------------
# Provisioning — idempotently ensure a tenant has a budgeted virtual key.
# ---------------------------------------------------------------------------


async def get_tenant_key(workspace: str) -> str | None:
    """Return the workspace's LiteLLM virtual key, or None when not yet
    provisioned. Tenant-filtered read (Rule 7). The proxy-calling paths (media /
    the harness) use this to attribute spend + enforce the tenant budget."""
    _require_workspace(workspace)
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    return doc.litellm_key if doc is not None else None


async def ensure_tenant_key(
    workspace: str,
    *,
    budget: KeyBudget | None = None,
    admin_client: LiteLLMAdminClient | None = None,
) -> ProvisionResult:
    """Ensure a LiteLLM virtual key exists for ``workspace``. Idempotent.

    First call: mint a key via the proxy admin API (POST /key/generate) carrying
    the budget / rpm / tpm / allowed-models from ``budget`` (default:
    ``load_key_budget()``) and ``metadata={"workspace_id": workspace}`` so the
    proxy attributes the key to the tenant, then persist the workspace -> key
    mapping. Returns ``ProvisionResult(..., created=True)``.

    Every later call: the mapping already exists, so return the stored key with
    ``created=False`` and NO proxy call (the idempotency guarantee — a workspace
    never gets two proxy keys, even under a concurrent double-provision: the
    unique ``workspace`` index makes the second insert collide and we re-read the
    winner).

    ``admin_client`` is injectable for tests (an ``httpx.MockTransport``-backed
    client); production builds one bound to the deployment master key.
    """
    _require_workspace(workspace)

    # Fast path — already provisioned. No proxy call.
    existing = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    if existing is not None and existing.litellm_key:
        return ProvisionResult(
            workspace_id=workspace, litellm_key=existing.litellm_key, created=False
        )

    card = budget if budget is not None else load_key_budget()
    client = admin_client if admin_client is not None else LiteLLMAdminClient()
    alias = f"{_KEY_ALIAS_PREFIX}{workspace}"

    # Mint the key on the proxy. A failure here raises LiteLLMAdminError — we do
    # NOT persist a half-row, so a retry re-attempts the mint cleanly.
    body = await client.generate_key(
        key_alias=alias,
        max_budget=card.max_budget_usd,
        budget_duration=card.budget_duration,
        rpm_limit=card.rpm_limit,
        tpm_limit=card.tpm_limit,
        models=card.models or None,
        metadata={"workspace_id": workspace},
    )
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise LiteLLMAdminError("LiteLLM /key/generate returned no key")

    # Persist the mapping. Upsert-on-insert with the unique ``workspace`` index as
    # the concurrency guard: if a racing call minted + inserted first, our insert
    # collides — we swallow it and re-read the winner so we never store two rows
    # (the loser's freshly-minted proxy key is orphaned but harmless: unused, and
    # the proxy budget bounds its blast radius; revoking orphans is a noted
    # follow-up).
    doc = LiteLLMTenantKey(
        workspace=workspace,
        litellm_key=key,
        key_alias=alias,
        max_budget_usd=card.max_budget_usd,
        budget_duration=card.budget_duration,
        rpm_limit=card.rpm_limit,
        tpm_limit=card.tpm_limit,
        models=list(card.models),
    )
    try:
        await doc.insert()
    except Exception as exc:  # noqa: BLE001 — Beanie raises DuplicateKeyError here
        winner = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
        if winner is not None and winner.litellm_key:
            logger.info(
                "llm_provisioning.ensure_tenant_key: workspace=%s lost mint race; "
                "using existing key (orphaned a fresh proxy key)",
                workspace,
            )
            return ProvisionResult(
                workspace_id=workspace, litellm_key=winner.litellm_key, created=False
            )
        # Not a duplicate-key collision — re-raise so the caller sees the real error.
        raise LiteLLMAdminError(f"persisting tenant key failed: {exc}") from exc

    logger.info(
        "llm_provisioning.ensure_tenant_key: workspace=%s provisioned a LiteLLM key "
        "(alias=%s, budget=%s/%s, rpm=%s, tpm=%s)",
        workspace,
        alias,
        card.max_budget_usd,
        card.budget_duration,
        card.rpm_limit,
        card.tpm_limit,
    )
    return ProvisionResult(workspace_id=workspace, litellm_key=key, created=True)


# ---------------------------------------------------------------------------
# Spend ingestion — read the tenant key's proxy spend -> the EXISTING ledger.
# ---------------------------------------------------------------------------


def _row_id(row: dict[str, Any]) -> str | None:
    """The stable per-row id used as the ledger idempotency key. LiteLLM spend
    rows carry ``request_id`` (the canonical id); fall back to ``id``. None when
    neither is present (such a row can't be deduped safely, so it is skipped)."""
    rid = row.get("request_id") or row.get("id")
    return str(rid) if rid else None


def _row_start_time(row: dict[str, Any]) -> str | None:
    """The row's ISO ``startTime`` (the high-water-mark field). None when absent."""
    ts = row.get("startTime") or row.get("startTimeStamp")
    return str(ts) if ts else None


def _cached_tokens(row: dict[str, Any]) -> int:
    """The cached-input token count a spend row reports (the prompt-cache savings
    signal). LiteLLM nests it under ``prompt_tokens_details.cached_tokens``; some
    rows expose a flat ``cache_read_input_tokens``. Best-effort across shapes."""
    details = row.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _int(details.get("cached_tokens"))
        if cached:
            return cached
    return _int(row.get("cache_read_input_tokens") or row.get("cached_tokens"))


async def ingest_tenant_spend(
    workspace: str,
    *,
    spend_card: SpendCredits | None = None,
    admin_client: LiteLLMAdminClient | None = None,
) -> SpendIngestResult:
    """Read ``workspace``'s LiteLLM proxy spend and debit it to the EXISTING credit
    ledger, exactly once per spend row.

    Reads GET /spend/logs?api_key=<tenant key>, keeps only rows NEWER than the
    stored ``last_spend_ingest_ts`` high-water mark, converts each row's USD
    ``spend`` to integer credits via the rate card, and debits the wallet with
    ``credits.service.debit(..., cause="litellm_spend",
    idempotency_key="litellm:{request_id}", allow_negative=True)``. BC-1's unique
    ``(workspace, idempotency_key)`` index makes a re-ingested row a ledger no-op
    (the real exactly-once guard); the high-water mark merely bounds the read.

    Advances ``last_spend_ingest_ts`` to the newest row's ``startTime`` after the
    sweep. Returns a ``SpendIngestResult`` (rows read / billed, credits + USD +
    cached-token totals, resulting balance).

    NOTE: this is the GATED half (see the module header's double-bill boundary).
    The caller decides whether to run it — this function does NOT check the flag,
    so it stays reusable by an explicit admin trigger even when the periodic sweep
    is off. A workspace with no provisioned key returns a zero result (nothing to
    ingest) rather than raising.
    """
    _require_workspace(workspace)

    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    if doc is None or not doc.litellm_key:
        # Not provisioned — nothing to ingest. Return the current balance so the
        # caller has a consistent shape.
        return SpendIngestResult(
            workspace_id=workspace,
            rows_read=0,
            rows_billed=0,
            credits_debited=0,
            cost_usd=0.0,
            cached_tokens=0,
            balance_after=await credits_service.balance(workspace),
        )

    card = spend_card if spend_card is not None else load_spend_credits()
    client = admin_client if admin_client is not None else LiteLLMAdminClient()

    rows = await client.spend_logs(api_key=doc.litellm_key)

    high_water = doc.last_spend_ingest_ts
    newest_ts = high_water

    rows_read = 0
    rows_billed = 0
    credits_debited = 0
    cost_usd_total = 0.0
    cached_total = 0

    # Oldest first so the high-water mark advances monotonically.
    for row in sorted(rows, key=lambda r: _row_start_time(r) or ""):
        start_ts = _row_start_time(row)
        # Skip rows at/older than the high-water mark — already ingested.
        if high_water is not None and start_ts is not None and start_ts <= high_water:
            continue

        rows_read += 1
        if newest_ts is None or (start_ts is not None and start_ts > newest_ts):
            newest_ts = start_ts

        cost_usd = _num(row.get("spend"))
        cached_total += _cached_tokens(row)
        cost_usd_total += cost_usd

        credits = card.to_credits(cost_usd)
        if credits <= 0:
            continue  # sub-credit / zero-cost row — nothing to debit

        rid = _row_id(row)
        if rid is None:
            # No stable id — we can't dedup it, so skip rather than risk a
            # double-debit on the next sweep.
            logger.warning(
                "llm_provisioning.ingest_tenant_spend: workspace=%s skipping a spend "
                "row with no request_id (cost_usd=%.6f)",
                workspace,
                cost_usd,
            )
            continue

        ledger_key = f"litellm:{rid}"
        # Skip a row already recorded (the high-water mark normally prevents this,
        # but a reset / a row predating the mark could re-surface). The debit would
        # no-op on the unique index anyway — this keeps ``rows_billed`` honest and
        # avoids the wasted insert-then-rollback. NOT the exactly-once guard (that
        # is BC-1's unique index); just accurate bookkeeping.
        if await credits_service.is_recorded(workspace, ledger_key):
            continue

        balance_after = await credits_service.debit(
            workspace=workspace,
            amount=credits,
            cause=_LITELLM_SPEND_CAUSE,
            idempotency_key=ledger_key,
            ref={
                "request_id": rid,
                "cost_usd": cost_usd,
                "model": row.get("model"),
                "cached_tokens": _cached_tokens(row),
                "source": "litellm_spend_log",
            },
            allow_negative=True,
        )
        credits_debited += credits
        rows_billed += 1
        logger.debug(
            "llm_provisioning.ingest_tenant_spend: workspace=%s billed %d credits "
            "(request_id=%s, cost_usd=%.6f) -> balance=%d",
            workspace,
            credits,
            rid,
            cost_usd,
            balance_after,
        )

    # Advance the high-water mark so a re-sweep doesn't re-read settled rows. Only
    # write when it actually moved (avoid a no-op save).
    if newest_ts is not None and newest_ts != doc.last_spend_ingest_ts:
        doc.last_spend_ingest_ts = newest_ts
        doc.updatedAt = datetime.now(UTC)
        await doc.save()

    balance_after = await credits_service.balance(workspace)
    if rows_billed:
        logger.info(
            "llm_provisioning.ingest_tenant_spend: workspace=%s ingested %d/%d new "
            "spend rows -> %d credits (cost_usd=%.6f, cached_tokens=%d), balance=%d",
            workspace,
            rows_billed,
            rows_read,
            credits_debited,
            cost_usd_total,
            cached_total,
            balance_after,
        )
    return SpendIngestResult(
        workspace_id=workspace,
        rows_read=rows_read,
        rows_billed=rows_billed,
        credits_debited=credits_debited,
        cost_usd=cost_usd_total,
        cached_tokens=cached_total,
        balance_after=balance_after,
    )


__all__ = [
    "ensure_tenant_key",
    "get_tenant_key",
    "ingest_tenant_spend",
    "load_key_budget",
    "load_spend_credits",
    "spend_ingest_enabled",
]
