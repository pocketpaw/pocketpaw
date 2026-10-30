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
#     the wallet IN EVERY MODE — provided nothing rounds on the way out, which is
#     the whole of the MICRO note below. (The prior version read the proxy's
#     /user/daily/activity, which a Free/keyless workspace never populates — so a
#     workspace deep in the negative showed "No usage to chart yet". This is the
#     fix for that bug.)
#   * NO CONVERSION HERE. Ledger credits are already markup-applied at debit time
#     (the meter / spend-ingest convert USD->credits when they write the
#     movement), so this read does NO ``to_credits`` and touches NO rate card — it
#     just surfaces the integers the wallet already holds. That is what makes the
#     graph and the wallet agree exactly.
#   * MICRO IS THE UNIT OF THE FOLD. The wallet stores micro-credits (1_000_000
#     micro == 1 credit == $0.01) and a chat run costs about 375_000 of them, so a
#     (day, model) group of ordinary light usage is worth 0 whole credits. This
#     module therefore folds ``row.credits_micro`` and converts to whole credits
#     exactly ONCE per figure, at the end. Truncating per group and summing the
#     results compounds the shortfall over every model and every day instead of
#     cancelling — that is how a day the wallet was charged 1.5 credits for
#     rendered as a row of zeros. The whole-credit fields stay on the wire for the
#     existing clients; ``*_micro`` is what anything reasoning about money reads.
#   * ENTITY ISOLATION. Billing must NOT query ``CreditLedgerEntry`` directly — the
#     credits entity owns reads of its own ledger doc. The (day, model) breakdown
#     comes through ``credits.service.spend_by_model`` (a sibling of the existing
#     ``sum_debits_by_cause``), the same boundary the cutover shadow-compare uses.
#   * READ-ONLY: a pure read. It NEVER debits, NEVER touches the ledger or the
#     balance, NEVER calls the cutover/metering path. It only reads + folds.
#
# TOKENS: the metering path (``metering.service.bill_run``) now stamps the run's
# real ``total_tokens`` onto the debit ``ref``, and ``spend_by_model`` sums it per
# (day, model), so per-model ``tokens`` reflects real volume — no longer a
# hardcoded 0. It is sourced from the wallet's own ledger like credits + requests
# (NOT the blocked LiteLLM path). A debit written before the ref carried tokens
# contributes 0, so historical days may under-report tokens while credits stay
# exact; volume is accurate for every run billed after the change.
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
# Changed 2026-07-11 (feat/llm-cost-attribution): per-model ``tokens`` now carries
# REAL volume — the metering path stamps ``total_tokens`` on the debit ref and
# ``spend_by_model`` sums it, so this surfaces ``row.tokens`` instead of the former
# hardcoded 0. Sourced from the wallet's own ledger (NOT the blocked LiteLLM path);
# legacy debits without the ref field contribute 0. The response contract is
# unchanged (the ``tokens`` field already existed) — only its value became real.
# Changed 2026-09-11 (fix/billing-usage-chart-micro): the fold now runs in
# MICRO-CREDITS. ``spend_by_model`` truncated each (day, model) group to whole
# credits before it reached here, and this module summed the truncations, so light
# usage charted as zeros and the error compounded across groups rather than
# cancelling. Per-model, per-day and grand totals now carry a ``*_micro`` figure
# and every whole-credit figure is derived from its micro total with a single
# ``micro_to_credits`` at the end. Additive on the wire — the pre-existing fields
# keep their names, so the frontend is not broken by this, only unblocked.

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
from pocketpaw_ee.cloud.credits.domain import micro_to_credits

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
    with a ``{credits, credits_micro, tokens, requests}`` block per model. The
    figures are the integers the wallet already holds (markup applied at debit time
    — NO conversion here), so the graph and the wallet agree in every metering mode
    (the ledger reads both ``compute_spend`` and ``litellm_spend``). Returns a
    ``WorkspaceUsageResponse``.

    The fold is in MICRO-CREDITS and each whole-credit figure is truncated from its
    own micro total exactly once. A chat run is about 375_000 micro (0 whole
    credits), so folding the whole-credit field instead would lose a day of light
    usage entirely and compound the loss across models and days.

    ``start_date`` / ``end_date`` are ``YYYY-MM-DD``; when BOTH are omitted the
    window defaults to the trailing 30 days. ``spend_reader`` is injectable for
    pure-unit tests (defaults to ``credits.service.spend_by_model``).

    ``tokens`` per model is the real volume the ledger now carries (the metering
    path stamps ``total_tokens`` on each debit ref; a legacy debit without it reads
    0). ``credits_micro`` is micro-credits (1_000_000 == 1 credit == $0.01 — NOT
    USD) and ``credits`` is that truncated for display; a caller rendering money
    reads the micro field. A workspace with no spend in the window returns an empty
    contract (no models, no buckets, totals 0) at HTTP 200 — never an error.
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
    #
    # The accumulator is MICRO-CREDITS. Accumulating ``row.credits`` would add up
    # figures that were each truncated to a unit coarser than the thing they charge
    # for, and the residues never cancel — they are all thrown away in the same
    # direction. Whole credits are derived once, below, from the micro totals.
    buckets: dict[str, dict[str, tuple[int, int, int]]] = {}
    models_seen: set[str] = set()
    for row in rows:
        models_seen.add(row.model)
        per_model = buckets.setdefault(row.day, {})
        # ``tokens`` is the real per-(day, model) volume the ledger now carries
        # (summed from each debit's ``ref.total_tokens`` in ``spend_by_model``); a
        # legacy row without it reads 0. Micro + requests come off the ledger too.
        micro, tokens, requests = per_model.get(row.model, (0, 0, 0))
        per_model[row.model] = (
            micro + row.credits_micro,
            tokens + row.tokens,
            requests + row.requests,
        )

    # Assemble the response: buckets OLDEST-FIRST, each with its exact total and the
    # whole-credit figure derived from it; the grand total over every bucket; the
    # sorted distinct model list (a stable legend). The "unknown" bucket (debits
    # with no ``ref.model``) is kept so the total reconciles with the wallet.
    out_buckets: list[UsageBucket] = []
    total_micro = 0
    for day in sorted(buckets):
        stats_by_model = {
            model: UsageModelStats(
                credits=micro_to_credits(micro),
                credits_micro=micro,
                tokens=tokens,
                requests=requests,
            )
            for model, (micro, tokens, requests) in buckets[day].items()
        }
        day_micro = sum(stats.credits_micro for stats in stats_by_model.values())
        total_micro += day_micro
        out_buckets.append(
            UsageBucket(
                date=day,
                by_model=stats_by_model,
                # Truncate the DAY's micro total, not the sum of per-model
                # truncations: three models at 750_000 micro is two credits charged
                # and would otherwise report 0.
                total_credits=micro_to_credits(day_micro),
                total_credits_micro=day_micro,
            )
        )

    return WorkspaceUsageResponse(
        start_date=resolved_start,
        end_date=resolved_end,
        models=sorted(models_seen),
        buckets=out_buckets,
        # Same rule one level up: truncate the grand micro total, never sum the
        # per-day whole-credit figures.
        total_credits=micro_to_credits(total_micro),
        total_credits_micro=total_micro,
    )


__all__ = ["get_workspace_usage"]
