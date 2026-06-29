# ee/pocketpaw_ee/cloud/billing/usage.py — the per-workspace USAGE-graph read.
#
# Module-level ``async def`` API (NOT a class, per the EE cloud rule, mirroring the
# rest of billing / credits / metering). One job: build the daily usage graph the
# frontend renders — daily usage BROKEN DOWN BY MODEL over a date range — from the
# workspace's CREDIT LEDGER (the wallet's own meter).
#
# THE SOURCE (read before changing — this is the whole point of the module):
#   * The graph is sourced from the CREDIT LEDGER, not the LiteLLM proxy. The
#     ledger is the UNIVERSAL meter: every finished run's compute cost is debited
#     to it as a negative movement with the run's model on ``ref.model``. In the
#     DEFAULT metering mode (POCKETPAW_LITELLM_SPEND_MODE=off) the meter
#     (``metering/service.py:bill_run``) debits the ledger DIRECTLY under
#     ``cause="compute_spend"`` and NOTHING flows through the proxy; after an
#     off->live cutover the spend-ingest (``llm_provisioning:ingest_tenant_spend``)
#     debits under ``cause="litellm_spend"``. Only ONE cause is ever active for a
#     given run, so reading BOTH is safe (no double-count) and the chart matches
#     the wallet IN EVERY MODE — by construction, since it is literally the
#     wallet's own decomposition. (The prior version read the proxy's
#     /user/daily/activity, which a Free/keyless workspace never populates — so a
#     workspace deep in the negative showed "No usage to chart yet". This is the
#     fix for that bug.)
#   * NO CONVERSION HERE. Ledger credits are already markup-applied at debit time
#     (the meter / spend-ingest convert USD->credits when they write the
#     movement), so this read does NO ``to_credits`` and touches NO rate card — it
#     just surfaces the integers the wallet already holds. That is what makes the
#     graph and the wallet agree exactly.
#   * ENTITY ISOLATION. Billing must NOT query ``CreditLedgerEntry`` directly — the
#     credits entity owns reads of its own ledger doc. The (day, model) breakdown
#     comes through ``credits.service.spend_by_model`` (a sibling of the existing
#     ``sum_debits_by_cause``), the same boundary the cutover shadow-compare uses.
#   * READ-ONLY: a pure read. It NEVER debits, NEVER touches the ledger or the
#     balance, NEVER calls the cutover/metering path. It only reads + folds.
#
# TOKENS=0 (a deliberate limitation): the ledger ``ref`` does not carry a token
# count, so per-model ``tokens`` is reported as 0. The credit + request figures are
# accurate; a follow-up can add ``total_tokens`` to the debit ``ref`` and surface
# it here. Do NOT try to recover tokens from elsewhere — they aren't on the wallet.
#
# UNKNOWN MODEL: a real charged debit whose ``ref.model`` is absent is bucketed
# under the model id ``"unknown"`` so its credits STILL count toward the day + the
# grand total — the chart must reconcile with the wallet, and dropping unattributed
# spend would make it under-report vs. what was charged.
#
# EMPTY CASE: a workspace with no spend in the window (a brand-new workspace, or a
# provisioned one that simply had no usage) yields an empty contract — no models,
# no buckets, total 0 — at HTTP 200, NOT an error. There is no proxy / key
# dependency anymore: the empty case is simply "no ledger rows in the window".
#
# Rule 6 — validate at entry (a workspace id is required). Rule 7 — the read is
# tenant-scoped (``spend_by_model`` filters on ``workspace``). The DTOs
# (``WorkspaceUsageResponse`` / ``UsageBucket`` / ``UsageModelStats``) are
# unchanged so the response contract stays byte-identical and the frontend is
# untouched.
#
# Created 2026-06-29 (feat/billing-usage-endpoint): new module — the GET
# /billing/usage transform (LiteLLM /user/daily/activity -> WorkspaceUsageResponse).
# Changed 2026-06-29 (fix/billing-usage-ledger-source): RE-SOURCED the graph from
# the credit ledger instead of the LiteLLM proxy daily-activity. The chart showed
# "No usage to chart yet" for any workspace without a LiteLLM virtual key even
# though the wallet held real ``compute_spend`` — the chart was wired to the one
# meter (the proxy) that is empty in the default off-mode. Now reads the wallet's
# own ledger via ``credits.service.spend_by_model`` (mode-agnostic across
# compute_spend / litellm_spend), so the chart matches the wallet by construction.
# Removed the proxy plumbing (get_tenant_key, the _DailyActivityClient Protocol,
# the LiteLLMAdminClient import, the spend_card / rate-card conversion, the
# record-folding helpers); kept the date-range validator + clamp and the response
# contract intact. tokens=0 is now intentional (the ledger ref carries no token
# count).

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing.dto import (
    UsageBucket,
    UsageModelStats,
    WorkspaceUsageResponse,
)
from pocketpaw_ee.cloud.credits import service as credits_service

logger = logging.getLogger(__name__)

# Default window when the caller omits start/end: the trailing 30 days INCLUSIVE of
# today (today minus 29 days .. today). Inclusive so a 30-day request shows 30 day
# columns, not 31.
_DEFAULT_WINDOW_DAYS = 30

# A caller-supplied window must be YYYY-MM-DD and span at most a year. The format
# check fails fast with a clean 400 rather than letting a malformed date through;
# the span clamp bounds the per-request fan-out. Both apply ONLY to an explicit
# range — the default window is always sane.
_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_WINDOW_DAYS = 366


def _resolve_explicit_range(start_date: str, end_date: str) -> tuple[str, str]:
    """Validate a caller-supplied ``[start_date, end_date]`` and clamp its span.

    Both bounds must be ``YYYY-MM-DD`` calendar dates with start on or before end;
    a malformed or inverted range raises ``ValidationError`` (a clean 400). A span
    over ``_MAX_WINDOW_DAYS`` is clamped to the most recent that many days ending at
    ``end_date`` (the response stamps the resolved window, so the axis reflects it).
    """
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if not _YMD.match(value):
            raise ValidationError("billing.invalid_date", f"{label} must be YYYY-MM-DD")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValidationError(
            "billing.invalid_date", "start_date / end_date must be a valid calendar date"
        ) from exc
    if start > end:
        raise ValidationError("billing.invalid_range", "start_date must be on or before end_date")
    if (end - start).days > _MAX_WINDOW_DAYS - 1:
        start = end - timedelta(days=_MAX_WINDOW_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _default_range() -> tuple[str, str]:
    """The default (start_date, end_date) — the trailing 30 days inclusive of today,
    as ``YYYY-MM-DD`` strings in UTC."""
    today = datetime.now(UTC).date()
    start = today - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)
    return start.isoformat(), today.isoformat()


async def get_workspace_usage(
    workspace_id: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    spend_reader=None,
) -> WorkspaceUsageResponse:
    """Build the per-workspace daily usage graph over ``[start_date, end_date]``.

    Reads the workspace's spend straight from its CREDIT LEDGER (via
    ``credits.service.spend_by_model``) and folds it into a ``UsageBucket`` per day
    with a ``{credits, tokens, requests}`` block per model. Credits are the
    integers the wallet already holds (markup applied at debit time — NO conversion
    here), so the graph and the wallet agree by construction in every metering mode
    (the ledger reads both ``compute_spend`` and ``litellm_spend``). Returns a
    ``WorkspaceUsageResponse``.

    ``start_date`` / ``end_date`` are ``YYYY-MM-DD``; when BOTH are omitted the
    window defaults to the trailing 30 days. ``spend_reader`` is injectable for
    pure-unit tests (defaults to ``credits.service.spend_by_model``).

    ``tokens`` is reported as 0 per model — the ledger ``ref`` does not carry a
    token count (the credit + request figures are accurate). A workspace with no
    spend in the window returns an empty contract (no models, no buckets, total 0)
    at HTTP 200 — never an error.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("billing.invalid_workspace", "workspace_id is required")

    if start_date and end_date:
        # Validate the format + clamp the span.
        resolved_start, resolved_end = _resolve_explicit_range(start_date, end_date)
    else:
        # Any partial range (only one bound given) falls back to the full default
        # window — we never source a half range.
        resolved_start, resolved_end = _default_range()

    # Build the UTC datetime window the ledger read expects: ``since`` is the start
    # day at 00:00 UTC (inclusive); ``until`` is the day AFTER end_date at 00:00 UTC
    # (exclusive) so the whole of end_date is included by day. This matches
    # ``spend_by_model``'s inclusive-since / exclusive-until ``createdAt`` filter.
    start_day = date.fromisoformat(resolved_start)
    end_day = date.fromisoformat(resolved_end)
    since = datetime.combine(start_day, time.min, tzinfo=UTC)
    until = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)

    reader = spend_reader if spend_reader is not None else credits_service.spend_by_model
    rows = await reader(workspace_id, since=since, until=until)

    # Fold the (day, model) rows into per-day buckets. ``spend_by_model`` already
    # aggregates one row per (day, model), so we assign directly; a duplicate
    # (day, model) would still accumulate defensively.
    buckets: dict[str, dict[str, UsageModelStats]] = {}
    models_seen: set[str] = set()
    for row in rows:
        models_seen.add(row.model)
        per_model = buckets.setdefault(row.day, {})
        existing = per_model.get(row.model)
        # ``tokens=0`` intentionally — the ledger ref carries no token count
        # (see the module header). Credits + requests come straight off the ledger.
        if existing is None:
            per_model[row.model] = UsageModelStats(
                credits=row.credits, tokens=0, requests=row.requests
            )
        else:
            per_model[row.model] = UsageModelStats(
                credits=existing.credits + row.credits,
                tokens=0,
                requests=existing.requests + row.requests,
            )

    # Assemble the response: buckets OLDEST-FIRST, each with its credit total; the
    # grand total over every bucket; the sorted distinct model list (a stable
    # legend). The "unknown" bucket (debits with no ``ref.model``) is kept so the
    # total reconciles with the wallet.
    out_buckets: list[UsageBucket] = []
    total_credits = 0
    for day in sorted(buckets):
        per_model = buckets[day]
        day_credits = sum(stats.credits for stats in per_model.values())
        total_credits += day_credits
        out_buckets.append(UsageBucket(date=day, by_model=per_model, total_credits=day_credits))

    return WorkspaceUsageResponse(
        start_date=resolved_start,
        end_date=resolved_end,
        models=sorted(models_seen),
        buckets=out_buckets,
        total_credits=total_credits,
    )


__all__ = ["get_workspace_usage"]
