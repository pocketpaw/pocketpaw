# ee/pocketpaw_ee/cloud/metering/service.py — the compute-cost metering business
# logic (BC-3, the Meter + Price primitives). Reads a finished ``ChatRunDoc``'s
# token-usage, resolves its real USD compute cost, converts that to integer
# credits via the rate card, and debits the workspace wallet EXACTLY ONCE.
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule, mirroring
# ``credits.service`` / ``billing.service``). Public API:
#   * ``resolve_cost(usage)``  — the Meter: a run's ``usage`` dict -> ``ComputeCost``
#                                (USD). Reported ``total_cost_usd`` wins when > 0;
#                                else the pricing-table estimate; else 0.0.
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
# AUTHORITATIVE fallback is ``src/pocketpaw/usage_tracker._estimate_cost`` over
# the ``_PRICING`` table — the same table the runtime usage tracker bills against.
# We therefore only trust a reported cost when it is strictly positive; otherwise
# we re-derive from token counts.
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
# Updated 2026-06-24 (B3 review fix): ``bill_run`` no longer does
# ``run_doc.billed=True; run_doc.save()`` directly (a foreign cross-entity write).
# It now delegates the flag write to ``chat.runs.service.mark_billed(run_id)``.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.chat.runs import service as chat_runs_service
from pocketpaw_ee.cloud.credits import service as credits_service
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


# ---------------------------------------------------------------------------
# The Meter — resolve a run's real USD compute cost from its usage dict.
# ---------------------------------------------------------------------------


def resolve_cost(usage: dict[str, Any] | None) -> ComputeCost:
    """Resolve a run's ``usage`` dict to a USD compute cost (the Meter).

    Resolution order:
      1. ``usage["total_cost_usd"]`` IF present and strictly ``> 0`` (the backend
         reported a real cost) -> source ``"reported"``.
      2. ELSE ``usage_tracker._estimate_cost(model, input, output, cached)`` over
         the authoritative ``_PRICING`` table -> source ``"estimated"`` (this is
         the keyless / OAuth path where the backend reports no cost).
      3. ELSE ``0.0`` -> source ``"none"`` (unknown model or empty usage); logged
         at DEBUG so an operator can spot an unpriced model without log noise.
    """
    usage = usage or {}
    model = usage.get("model") or None

    reported = usage.get("total_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > 0:
        return ComputeCost(cost_usd=float(reported), source="reported", model=model)

    # Fallback to the authoritative pricing table. Import lazily so importing the
    # metering entity never drags the runtime usage_tracker into the cloud import
    # graph at module load.
    from pocketpaw.usage_tracker import _estimate_cost

    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cached_input_tokens = _int(usage.get("cached_input_tokens"))

    estimated = None
    if model is not None and (input_tokens or output_tokens or cached_input_tokens):
        estimated = _estimate_cost(model, input_tokens, output_tokens, cached_input_tokens)

    if estimated is not None and estimated > 0:
        return ComputeCost(cost_usd=float(estimated), source="estimated", model=model)

    logger.debug(
        "metering.resolve_cost: no billable cost (model=%r, in=%d, out=%d, cached=%d, "
        "reported=%r) — billing 0",
        model,
        input_tokens,
        output_tokens,
        cached_input_tokens,
        reported,
    )
    return ComputeCost(cost_usd=0.0, source="none", model=model)


# ---------------------------------------------------------------------------
# The Price — the rate card from runtime settings.
# ---------------------------------------------------------------------------


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
    cost = resolve_cost(run_doc.usage)
    credits = card.to_credits(cost.cost_usd)

    workspace = run_doc.workspace
    run_id = run_doc.run_id

    if credits > 0:
        balance_after = await credits_service.debit(
            workspace=workspace,
            amount=credits,
            cause=_COMPUTE_SPEND_CAUSE,
            idempotency_key=f"run:{run_id}",
            ref={
                "run_id": run_id,
                "cost_usd": cost.cost_usd,
                "cost_source": cost.source,
                "model": cost.model,
            },
            allow_negative=True,
        )
        debited = True
        logger.info(
            "metering.bill_run: run=%s workspace=%s billed %d credits "
            "(cost_usd=%.6f, source=%s, model=%r) -> balance=%d",
            run_id,
            workspace,
            credits,
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
            "metering.bill_run: run=%s workspace=%s cost rounds to 0 credits "
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
        credits_charged=credits,
        balance_after=balance_after,
        debited=debited,
        cost_source=cost.source,
        model=cost.model,
    )


__all__ = [
    "bill_run",
    "load_rate_card",
    "resolve_cost",
]
