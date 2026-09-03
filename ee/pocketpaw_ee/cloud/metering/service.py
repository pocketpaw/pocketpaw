# ee/pocketpaw_ee/cloud/metering/service.py — the compute-cost metering business
# logic (BC-3, the Meter + Price primitives). Reads a finished ``ChatRunDoc``'s
# token-usage, resolves its real USD compute cost, converts that to integer
# credits via the rate card, and debits the workspace wallet EXACTLY ONCE.
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule, mirroring
# ``credits.service`` / ``billing.service``). Public API:
#   * ``resolve_cost(usage, at=)`` — the Meter: a run's ``usage`` dict ->
#                                ``ComputeCost`` (USD). Reported ``total_cost_usd``
#                                wins when > 0; else the dated price lookup; else
#                                0.0. ``at`` is the run's OWN moment and is
#                                required — see "PRICES ARE DATED" below.
#   * ``load_rate_card()``     — build the ``RateCard`` (the Price primitive) from
#                                runtime settings (POCKETPAW_BILLING_MARKUP /
#                                POCKETPAW_CREDIT_USD).
#   * ``bill_run(run_doc)``    — compute credits and debit the wallet once, keyed
#                                on ``run:{run_id}`` (BC-1's idempotency guard),
#                                then mark the run billed via
#                                ``chat.runs.service.mark_billed`` (the doc's
#                                owner). Returns a ``BillResult``.
#
# COST AUTHORITY: the production backend runs keyless / OAuth, so it frequently
# emits ``total_cost_usd = None`` or ``0`` even though tokens were consumed. The
# AUTHORITATIVE fallback is ``src/pocketpaw/usage_tracker.price_run`` — the same
# ladder the runtime usage tracker bills against. We therefore only trust a
# reported cost when it is strictly positive; otherwise we re-derive it.
#
# PRICES ARE DATED, AND THIS METER RUNS LATE. ``resolve_cost`` takes ``at``, the
# run's own moment, and it is a required argument rather than a defaulted one on
# purpose. Billing happens on the sweeper AFTER the run, capped at 200 runs a
# tick, so a backlog can span hours or days; ``claude-sonnet-5`` was $2.00/MTok
# through 2026-08-31 and $3.00 from 2026-09-01. Pricing at ``now()`` would bill
# every backlogged run at today's rate and look entirely plausible doing it. It
# is the same argument the sweeper's ``_emit_run_completed`` already makes for
# the ledger's ``ts``: the run's moment, never the sweep's.
#
# TOKEN SHAPE: producers do not agree on whether ``usage["input_tokens"]``
# includes the cached portion, so ``_prompt_tokens`` decides from the payload
# rather than guessing — see its docstring. Getting this wrong is not academic;
# handing the price library cache buckets larger than their own total is a hard
# error, and handing it a pre-subtracted remainder undercounts the bill.
#
# EXACTLY-ONCE: the debit's ``idempotency_key=f"run:{run_id}"`` + BC-1's unique
# ``(workspace, idempotency_key)`` index means a re-bill (sweeper double-run,
# crash between the debit and the ``billed`` flag write, two workers) is a no-op
# at the ledger. The ``billed`` flag is the cheap filter that keeps each sweep
# bounded; the ledger key is the real guard. ``allow_negative=True`` — a completed
# run is always billed (the compute already happened), so an overage drives the
# balance legitimately negative rather than being blocked here. Hard-blocking is a
# separate concern at run START, not at bill time.
#
# Rule 10 — only ``CloudError`` subclasses propagate (none raised on the happy
# path; an unknown-model run bills 0, it does not error). Rule 6 — the meter
# tolerates a missing / malformed usage dict (treats it as no usage).
#
# ENTITY BOUNDARY (EE Rule 2): metering READS ``run_doc.usage`` (a value read is
# fine), but the ``billed`` flag WRITE belongs to the chat.runs entity (the sole
# owner of ``ChatRunDoc``). ``bill_run`` calls ``chat.runs.service.mark_billed``
# rather than mutating + saving the foreign document itself.
#
# Created 2026-06-24 (integration/billing-credits, BC-3): new entity.
# Updated 2026-09-02 (feat/bill-on-completion): added ``bill_run_now``, called by
#   both run executors the moment ``execute_run`` returns. Nothing charged at
#   completion before, so the wallet, the allowance meter and the activity list
#   were all a sweep interval stale after every run — a customer who checked
#   immediately saw the state from before their own message. The sweeper is
#   unchanged and is still the backstop: ``bill_run`` is idempotent on
#   ``run:{run_id}``, so a run billed at completion is a no-op on the tick, and a
#   run this misses is picked up by it. Gated OFF in the WU-F ``live`` mode for
#   the same reason the sweep is — exactly one meter charges.
# Updated 2026-06-24 (B3 review fix): ``bill_run`` no longer does
# ``run_doc.billed=True; run_doc.save()`` directly (a foreign cross-entity write).
# It now delegates the flag write to ``chat.runs.service.mark_billed(run_id)``.
# Updated 2026-07-11 (feat/llm-cost-attribution): ``bill_run`` now records the run's
# ``total_tokens`` on the debit ``ref`` (alongside cost/source/model). The token
# volume is a REAL per-run figure the backend reports onto ``ChatRunDoc.usage`` in
# every metering mode (it is NOT the blocked LiteLLM SpendLogs path), so persisting
# it lets the ledger-sourced usage graph surface real token volume instead of a
# hardcoded 0 — the follow-up the usage-graph module header named. Purely additive
# ledger metadata: no change to the debited amount, the idempotency key, or the
# exactly-once guard; legacy entries with no ``ref.total_tokens`` simply read 0.
# Updated 2026-09-02 (fix/metering-dated-pricing): ``resolve_cost`` gained the
# required ``at`` keyword and now calls ``usage_tracker.price_run``, which prices
# cache writes, applies the >200k long-context tier and honours effective dates.
# An unpriced run is its own ``CostSource`` and logs at WARNING instead of DEBUG.

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from pocketpaw_ee.cloud.chat.runs import service as chat_runs_service
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.credits.domain import micro_to_credits
from pocketpaw_ee.cloud.metering.domain import ComputeCost, RateCard
from pocketpaw_ee.cloud.metering.dto import BillResult
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)

# Business cause stamped on the ledger movement (matches the credits tests'
# convention and the dashboard's spend filter).
_COMPUTE_SPEND_CAUSE = "compute_spend"


def _int(value: Any) -> int:
    """Coerce a usage count to a non-negative int, tolerating None / strings."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _total_tokens(usage: dict[str, Any] | None) -> int:
    """Total tokens a run consumed, from its ``usage`` dict.

    This is the REAL per-run token volume the backend reports onto
    ``ChatRunDoc.usage`` (via the ``token_usage`` event) in EVERY metering mode —
    NOT the blocked LiteLLM SpendLogs path. Prefers an explicit ``total_tokens``
    the backend supplied; otherwise sums the components (input + output + cached —
    cached_input are real tokens, so they count, matching the runtime
    usage_tracker's own total). Returns 0 for empty / malformed usage.
    """
    usage = usage or {}
    explicit = _int(usage.get("total_tokens"))
    if explicit > 0:
        return explicit
    return (
        _int(usage.get("input_tokens"))
        + _int(usage.get("output_tokens"))
        + _int(usage.get("cached_input_tokens"))
    )


# ---------------------------------------------------------------------------
# The Meter — resolve a run's real USD compute cost from its usage dict.
# ---------------------------------------------------------------------------


def _prompt_tokens(usage: dict[str, Any]) -> tuple[int, int, int]:
    """``(inclusive prompt tokens, cache reads, cache writes)`` from a usage dict.

    The backends do NOT agree on what ``input_tokens`` means and there is no flag
    saying which convention a given run used, so this reads it off the payload's
    own shape instead of guessing:

      * A payload carrying ``cache_read_tokens`` / ``cache_write_tokens`` is
        Anthropic-shaped (``pydantic_ai``, ``claude_sdk``, ``deep_agents`` — the
        three that emit that structured cache telemetry), and there
        ``input_tokens`` is the UNCACHED REMAINDER. The inclusive total is the
        sum of all three. ``cached_input_tokens`` is deliberately ignored on this
        branch: ``claude_sdk`` sets it to read PLUS write, so adding it as well
        would count the cache twice.
      * Anything else is OpenAI-shaped (``codex_cli``, ``openai_agents``,
        ``google_adk``, ``copilot_sdk``), where ``input_tokens`` already includes
        the cached subset and ``cached_input_tokens`` is that subset. ``max`` is
        belt: a payload where the subset exceeds its own total is malformed, and
        the total is the thing to widen.

    Cache WRITES are 0 on the second branch because those providers do not bill a
    write separately.
    """
    raw_input = _int(usage.get("input_tokens"))
    if "cache_read_tokens" in usage or "cache_write_tokens" in usage:
        read = _int(usage.get("cache_read_tokens"))
        write = _int(usage.get("cache_write_tokens"))
        return raw_input + read + write, read, write
    read = _int(usage.get("cached_input_tokens"))
    return max(raw_input, read), read, 0


def resolve_cost(usage: dict[str, Any] | None, *, at: datetime | None) -> ComputeCost:
    """Resolve a run's ``usage`` dict to a USD compute cost (the Meter).

    ``at`` is the run's OWN moment (``ended_at``, else ``createdAt``) and is
    required. Prices are effective-dated and this meter runs on a sweeper that
    drains a backlog, so pricing at ``now()`` bills an old run at today's rate.
    ``None`` is allowed but means "the caller genuinely has no moment", and
    ``price_run`` logs that at WARNING before falling back to now — the fallback
    is explicit and audible, not a silent default.

    Resolution order:
      1. ``usage["total_cost_usd"]`` IF present and strictly ``> 0`` (the backend
         reported a real cost) -> source ``"reported"``.
      2. ELSE ``usage_tracker.price_run(...)`` -> source ``"estimated"`` (the
         keyless / OAuth path where the backend reports no cost). This prices
         cache reads and writes separately and applies any long-context tier.
      3. ELSE 0.0. Source ``"unpriced"`` when the run had a model and real tokens
         and nothing could price it — logged at WARNING, because that is a bill
         we are failing to send and it used to be invisible. Source ``"none"``
         when there was simply nothing to bill, still at DEBUG.
    """
    usage = usage or {}
    model = usage.get("model") or None

    reported = usage.get("total_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > 0:
        return ComputeCost(
            cost_usd=float(reported),
            source="reported",
            model=model,
            cost_usd_exact=Decimal(str(reported)),
        )

    # Import lazily so importing the metering entity never drags the runtime
    # usage_tracker into the cloud import graph at module load.
    from pocketpaw.usage_tracker import price_run

    input_tokens, cache_read, cache_write = _prompt_tokens(usage)
    output_tokens = _int(usage.get("output_tokens"))
    billable = bool(input_tokens or output_tokens)

    priced = None
    if model is not None and billable:
        priced = price_run(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            at=at,
        )

    if priced is not None and priced > 0:
        return ComputeCost(
            cost_usd=float(priced),
            source="estimated",
            model=model,
            cost_usd_exact=priced,
        )

    if model is not None and billable:
        # C4 — this used to log at DEBUG next to the empty-usage case, so a model
        # nothing could price billed $0 in silence. It is the loudest thing this
        # function does now, because it is the only outcome that is a mistake.
        logger.warning(
            "metering.resolve_cost: NO PRICE for model %r — billing 0 for a run "
            "that consumed tokens (in=%d out=%d cache_read=%d cache_write=%d, "
            "at=%s). Add a row to usage_tracker._PRICING if genai-prices lacks "
            "the id.",
            model,
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            at.isoformat() if at is not None else "<none>",
        )
        return ComputeCost(cost_usd=0.0, source="unpriced", model=model)

    logger.debug(
        "metering.resolve_cost: no billable cost (model=%r, in=%d, out=%d, cached=%d, "
        "reported=%r) — billing 0",
        model,
        input_tokens,
        output_tokens,
        cache_read,
        reported,
    )
    return ComputeCost(cost_usd=0.0, source="none", model=model)


# ---------------------------------------------------------------------------
# The Price — the rate card from runtime settings.
# ---------------------------------------------------------------------------


def run_moment(run_doc: ChatRunDoc) -> datetime | None:
    """The moment a run actually happened — what its compute must be priced at.

    ``ended_at`` when the run recorded one, else ``createdAt``. Exactly the pair
    ``sweeper._emit_run_completed`` uses for the agent-ledger ``ts``, and for the
    same reason: the sweep drains a backlog FIFO and may run long after an
    outage, so sweep-time is never the right stamp for anything the run owns.
    Price is one of those things — ``claude-sonnet-5`` changed rate on
    2026-09-01, and a run from the 30th billed at sweep time would pay it.

    ``None`` when the doc carries no moment at all; the caller says so out loud
    rather than pretending it is now.
    """
    return getattr(run_doc, "ended_at", None) or getattr(run_doc, "createdAt", None)


def load_rate_card() -> RateCard:
    """Build the rate card (the Price primitive) from runtime settings.

    Reads ``POCKETPAW_BILLING_MARKUP`` (default 2.5) and ``POCKETPAW_CREDIT_USD``
    (default 0.01). Lazy import so the metering entity has no import-time
    dependency on the settings singleton.
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    return RateCard(markup=float(settings.billing_markup), credit_usd=float(settings.credit_usd))


# ---------------------------------------------------------------------------
# bill_run — debit the wallet for one run's compute, exactly once.
# ---------------------------------------------------------------------------


async def bill_run_now(run_id: str) -> BillResult | None:
    """Bill one run the moment it finishes, so the wallet is not a sweep behind.

    Called by the two run executors right after ``execute_run`` returns. Without
    it nothing charges until the next five-minute tick, which means the balance,
    the allowance meter and the activity list are all stale for a whole interval
    after every run — and a customer who checks immediately sees the state from
    before their own message, with nothing on screen saying so.

    **This does not replace the sweeper, and must not.** It is an optimisation on
    top of it. ``bill_run`` is idempotent on ``run:{run_id}``, so a run billed
    here is a no-op when the sweep later reaches it, and a run this misses (a
    crash between the terminal write and this call, an executor path that does
    not reach here, a transient failure below) is still picked up on the next
    tick. That is why every failure here is swallowed: the cost of losing this
    optimisation is five minutes, and the cost of raising is failing a run that
    already succeeded.

    Returns the ``BillResult`` when it billed, or ``None`` when it deliberately
    did nothing — the run is missing, already billed, or LiteLLM is the sole
    meter.
    """
    # Lazy import — same reason the sweeper does it: avoids a
    # metering <-> llm_provisioning module-load cycle.
    from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

    try:
        # WU-F single-meter gate, identical to the sweeper's. In ``live`` LiteLLM
        # is the sole meter and per-run metering must not charge, at completion
        # any more than on a tick.
        if provisioning_service.spend_mode() == "live":
            return None

        doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
        if doc is None:
            logger.debug(
                "metering.bill_run_now: run=%s not found — leaving it to the sweep", run_id
            )
            return None
        if doc.billed:
            return None
        if doc.status not in _TERMINAL_STATES_FOR_IMMEDIATE_BILLING:
            # Not finished (or finished in a state the sweeper owns). The sweep
            # picks it up once it settles.
            return None

        return await bill_run(doc)
    except Exception:
        # Never let billing fail a run that already succeeded. The sweeper is the
        # backstop and this run keeps ``billed=False`` until it runs.
        logger.exception("metering.bill_run_now: run=%s failed — the sweep will retry it", run_id)
        return None


async def bill_run(run_doc: ChatRunDoc, *, rate_card: RateCard | None = None) -> BillResult:
    """Bill ``run_doc``'s compute cost to its workspace wallet, exactly once.

    Resolves the USD cost (the Meter), converts to credits via the rate card (the
    Price), and debits the workspace wallet with ``allow_negative=True`` keyed on
    ``run:{run_id}`` so a re-bill is a ledger no-op. When the cost rounds to 0
    credits (empty usage / unknown model / sub-credit cost) NO debit is written —
    but the run is STILL marked ``billed`` so the sweeper never re-visits it.

    The ``billed`` flag is flipped True AFTER the debit lands, via
    ``chat.runs.service.mark_billed`` (the chat.runs entity owns ChatRunDoc — EE
    Rule 2; metering never writes the foreign doc). A crash between the debit and
    the flag write is harmless: the next sweep re-bills, BC-1's ``run:{run_id}``
    key makes that debit a no-op, and the flag is then set.

    ``rate_card`` may be injected (tests / a future per-workspace card); it
    defaults to the settings-derived card.
    """
    card = rate_card if rate_card is not None else load_rate_card()
    cost = resolve_cost(run_doc.usage, at=run_moment(run_doc))
    # The Decimal when there is one: the price came back exact and rounding it to
    # a float on the way into a rounding function is two roundings for one bill.
    # Micro-credits: a run priced at $0.0015 is 375_000 of them, where rounding to
    # whole credits made it 0 and served the run free. Same rate card, same money,
    # a unit fine enough to express it.
    micro = card.to_micro_credits(
        cost.cost_usd_exact if cost.cost_usd_exact is not None else cost.cost_usd
    )
    # Real per-run token volume (see ``_total_tokens``) — stamped on the debit ref
    # so the ledger-sourced usage graph can surface it. Mode-agnostic; never the
    # blocked LiteLLM path.
    tokens = _total_tokens(run_doc.usage)

    workspace = run_doc.workspace
    run_id = run_doc.run_id

    if micro > 0:
        balance_after = await credits_service.debit(
            workspace=workspace,
            amount_micro=micro,
            cause=_COMPUTE_SPEND_CAUSE,
            idempotency_key=f"run:{run_id}",
            ref={
                "run_id": run_id,
                "cost_usd": cost.cost_usd,
                "cost_source": cost.source,
                "model": cost.model,
                "total_tokens": tokens,
            },
            allow_negative=True,
        )
        debited = True
        logger.info(
            "metering.bill_run: run=%s workspace=%s billed %d micro-credits "
            "(cost_usd=%.6f, source=%s, model=%r) -> balance=%d",
            run_id,
            workspace,
            micro,
            cost.cost_usd,
            cost.source,
            cost.model,
            balance_after,
        )
    else:
        # Nothing to charge — still mark billed so the run isn't re-swept forever.
        balance_after = await credits_service.balance(workspace)
        debited = False
        logger.debug(
            "metering.bill_run: run=%s workspace=%s is genuinely free "
            "(cost_usd=%.6f, source=%s) — marking billed, no debit",
            run_id,
            workspace,
            cost.cost_usd,
            cost.source,
        )

    # Flip the flag last, through the chat.runs entity that OWNS ChatRunDoc (EE
    # Rule 2 — metering never writes a foreign document). Idempotent by
    # construction — a racing flag write that loses is harmless because the ledger
    # key already prevented a double debit.
    await chat_runs_service.mark_billed(run_id)

    return BillResult(
        run_id=run_id,
        workspace_id=workspace,
        cost_usd=cost.cost_usd,
        credits_charged=micro_to_credits(micro),
        micro_charged=micro,
        balance_after=balance_after,
        debited=debited,
        cost_source=cost.source,
        model=cost.model,
    )


# The states a finished run can be in. Mirrors the sweeper's ``_TERMINAL_STATES``
# — kept as its own constant here rather than imported so the metering entity
# does not depend on its own sweeper module.
_TERMINAL_STATES_FOR_IMMEDIATE_BILLING = ("completed", "interrupted", "failed", "cancelled")

__all__ = [
    "bill_run",
    "bill_run_now",
    "load_rate_card",
    "resolve_cost",
    "run_moment",
]
