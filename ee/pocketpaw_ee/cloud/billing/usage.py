# ee/pocketpaw_ee/cloud/billing/usage.py — the per-workspace USAGE-graph read.
#
# Module-level ``async def`` API (NOT a class, per the EE cloud rule, mirroring the
# rest of billing / credits / metering). One job: build the daily usage graph the
# frontend renders — daily usage BROKEN DOWN BY MODEL over a date range — from the
# workspace's LiteLLM proxy usage.
#
# THE SEAM (read before changing the conversion — money-adjacent):
#   * MAPPING: a workspace maps to its LiteLLM VIRTUAL KEY (NOT a user_id). The
#     provisioning entity owns that mapping (one ``LiteLLMTenantKey`` row per
#     workspace, the proxy key minted with metadata={workspace_id}); we resolve it
#     through ``llm_provisioning.service.get_tenant_key`` rather than touching the
#     doc (entity isolation — only that entity reads its key doc). The proxy's
#     /user/daily/activity route accepts an ``api_key`` filter, so we scope usage to
#     exactly that tenant's key. The admin client calls with the deployment MASTER
#     key (the proxy treats it as admin view), so the api_key filter is honoured.
#   * CONVERSION: spend USD -> integer credits via the SAME rate card the meter +
#     spend-ingest use — ``SpendCredits.to_credits(cost_usd) =
#     round(cost_usd * markup / credit_usd)`` (markup = billing_markup, default 2.5;
#     credit_usd default 0.01). We deliberately reuse that conversion (via
#     ``llm_provisioning.service.load_spend_credits``) rather than a flat
#     ``round(usd * 100)`` so a dollar of usage SHOWN here equals a dollar of usage
#     BILLED by the meter — the usage graph and the wallet never disagree. (A flat
#     *100 would ignore the markup and under-report vs. what was charged.)
#   * READ-ONLY: this is a pure read. It NEVER debits, NEVER touches the credit
#     ledger or the spend high-water mark, NEVER calls the cutover/metering path.
#     It only converts numbers for display.
#
# EMPTY / NO-KEY CASE: a brand-new workspace with no provisioned key returns an
# empty contract (no models, no buckets, total 0) at HTTP 200 — NOT an error, and
# WITHOUT a proxy call (there is no key to scope). Same for a provisioned workspace
# that simply had no usage in the window (the proxy returns no rows).
#
# Rule 6 — validate at entry (a workspace id is required). Rule 7 — the read is
# tenant-scoped (the key filter IS the tenant scope). Rule 10 — only CloudError
# subclasses propagate to HTTP; a proxy read failure raises ``LiteLLMAdminError``
# (a plain exception) which the route surfaces as a 502 via the cloud error path.
#
# Created 2026-06-29 (feat/billing-usage-endpoint): new module — the GET
# /billing/usage transform (LiteLLM /user/daily/activity -> WorkspaceUsageResponse).

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing.dto import (
    UsageBucket,
    UsageModelStats,
    WorkspaceUsageResponse,
)
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service
from pocketpaw_ee.cloud.llm_provisioning.domain import SpendCredits

logger = logging.getLogger(__name__)

# Default window when the caller omits start/end: the trailing 30 days INCLUSIVE of
# today (today minus 29 days .. today). Inclusive so a 30-day request shows 30 day
# columns, not 31.
_DEFAULT_WINDOW_DAYS = 30


class _DailyActivityClient(Protocol):
    """The slice of LiteLLMAdminClient this module needs. A Protocol so tests can
    inject a duck-typed fake (no HTTP) and we never construct an httpx client in a
    unit test."""

    async def user_daily_activity(
        self, *, start_date: str, end_date: str, api_key: str, page_size: int = ...
    ) -> list[dict[str, Any]]: ...


def _num(value: Any) -> float:
    """Coerce a proxy spend value to a float, tolerating None / strings (the proxy
    occasionally serialises numbers as strings)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    """Coerce a token / request count to a non-negative int, tolerating None /
    strings. A negative / unparseable value clamps to 0."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _default_range() -> tuple[str, str]:
    """The default (start_date, end_date) — the trailing 30 days inclusive of today,
    as ``YYYY-MM-DD`` strings in UTC."""
    today = datetime.now(UTC).date()
    start = today - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)
    return start.isoformat(), today.isoformat()


def _record_date(record: dict[str, Any]) -> str | None:
    """The ``YYYY-MM-DD`` date of a LiteLLM daily record. The proxy serialises the
    ``date`` field as an ISO date string; we keep just the date portion. None when
    a record carries no usable date (skipped rather than bucketed under ''
    )."""
    raw = record.get("date")
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Tolerate a full ISO timestamp by keeping the date head (the proxy emits a bare
    # date today, but a future shape change to a datetime must not mis-bucket).
    return s.split("T", 1)[0]


def _model_breakdown(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ``breakdown.models`` map of a daily record (model id -> {metrics, ...}),
    or an empty dict when absent/malformed. Defensive across shapes so a partial
    proxy row never crashes the transform."""
    breakdown = record.get("breakdown")
    if not isinstance(breakdown, dict):
        return {}
    models = breakdown.get("models")
    if not isinstance(models, dict):
        return {}
    return {str(name): m for name, m in models.items() if isinstance(m, dict)}


async def get_workspace_usage(
    workspace_id: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    spend_card: SpendCredits | None = None,
    daily_activity_client: _DailyActivityClient | None = None,
) -> WorkspaceUsageResponse:
    """Build the per-workspace daily usage graph over ``[start_date, end_date]``.

    Resolves the workspace's LiteLLM virtual key, reads its daily activity from the
    proxy (scoped by that key), and folds each day's per-model breakdown into a
    ``UsageBucket`` with a ``{credits, tokens, requests}`` block per model. Spend USD
    is converted to credits with the SAME rate card the meter uses (so the graph and
    the wallet agree). Returns a ``WorkspaceUsageResponse``.

    ``start_date`` / ``end_date`` are ``YYYY-MM-DD``; when BOTH are omitted the window
    defaults to the trailing 30 days. ``spend_card`` / ``daily_activity_client`` are
    injectable for tests; production resolves the settings-derived rate card and a
    master-key admin client.

    A workspace with NO provisioned key (or no usage in the window) returns an empty
    contract (no models, no buckets, total 0) — never an error, and the no-key case
    makes NO proxy call.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("billing.invalid_workspace", "workspace_id is required")

    if start_date and end_date:
        resolved_start, resolved_end = start_date, end_date
    else:
        # Any partial range (only one bound given) falls back to the full default
        # window — the proxy requires BOTH bounds, so we never send a half range.
        resolved_start, resolved_end = _default_range()

    # MAPPING: workspace -> its LiteLLM virtual key, via the provisioning entity
    # (entity isolation — we don't read the key doc directly). No key == a
    # brand-new workspace with nothing provisioned yet.
    key = await provisioning_service.get_tenant_key(workspace_id)
    if not key:
        # Empty contract, HTTP 200, NO proxy call — there is no key to scope.
        return WorkspaceUsageResponse(
            start_date=resolved_start,
            end_date=resolved_end,
            models=[],
            buckets=[],
            total_credits=0,
        )

    card = spend_card if spend_card is not None else provisioning_service.load_spend_credits()
    client = daily_activity_client
    if client is None:
        # Lazy import — keep this module free of an import-time httpx dependency, and
        # so a unit test that injects a fake never constructs a real client.
        from pocketpaw_ee.catalog.admin_client import LiteLLMAdminClient

        client = LiteLLMAdminClient()

    records = await client.user_daily_activity(
        start_date=resolved_start,
        end_date=resolved_end,
        api_key=key,
    )

    # Fold the daily records into per-day buckets keyed by date. A proxy is expected
    # to return one record per date for a single-entity query, but we accumulate
    # defensively so a duplicated date (or a future per-key split) sums correctly.
    buckets: dict[str, dict[str, UsageModelStats]] = {}
    models_seen: set[str] = set()

    for record in records:
        day = _record_date(record)
        if day is None:
            continue
        per_model = buckets.setdefault(day, {})
        for model_id, model_block in _model_breakdown(record).items():
            metrics = model_block.get("metrics")
            if not isinstance(metrics, dict):
                continue
            credits = card.to_credits(_num(metrics.get("spend")))
            tokens = _int(metrics.get("total_tokens"))
            # The proxy splits requests into successful/failed + a combined
            # api_requests; prefer api_requests (the total attempts), falling back to
            # successful_requests for an older shape that lacked api_requests.
            requests = _int(metrics.get("api_requests")) or _int(
                metrics.get("successful_requests")
            )
            models_seen.add(model_id)
            existing = per_model.get(model_id)
            if existing is None:
                per_model[model_id] = UsageModelStats(
                    credits=credits, tokens=tokens, requests=requests
                )
            else:
                # Accumulate a repeated (date, model) — keeps the transform correct
                # if the proxy ever returns split rows for the same day+model.
                per_model[model_id] = UsageModelStats(
                    credits=existing.credits + credits,
                    tokens=existing.tokens + tokens,
                    requests=existing.requests + requests,
                )

    # Assemble the response: buckets oldest-first, each with its credit total; the
    # grand total over every bucket; the sorted distinct model list (a stable legend).
    out_buckets: list[UsageBucket] = []
    total_credits = 0
    for day in sorted(buckets):
        per_model = buckets[day]
        day_credits = sum(stats.credits for stats in per_model.values())
        total_credits += day_credits
        out_buckets.append(
            UsageBucket(date=day, by_model=per_model, total_credits=day_credits)
        )

    return WorkspaceUsageResponse(
        start_date=resolved_start,
        end_date=resolved_end,
        models=sorted(models_seen),
        buckets=out_buckets,
        total_credits=total_credits,
    )


__all__ = ["get_workspace_usage"]
