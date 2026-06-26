# ee/pocketpaw_ee/cloud/llm_provisioning/cutover_sweeper.py — the durable
# per-tenant LiteLLM billing-cutover sweep (WU-F).
#
# One system job that iterates every PROVISIONED tenant and runs the spend logic
# for the current cutover mode (POCKETPAW_LITELLM_SPEND_MODE, honouring the legacy
# INGEST bool via ``service.spend_mode()``):
#
#   * ``off``    — no-op. BC-3 per-run metering bills as today; nothing to sweep.
#   * ``shadow`` — read-only compare. For each tenant, ``reconcile_tenant_spend``
#                  reads proxy spend + the BC-3 ``compute_spend`` ledger over a
#                  lookback window and records a reconciliation row. DEBITS NOTHING.
#   * ``live``   — LiteLLM is the sole meter. For each tenant,
#                  ``ingest_tenant_spend`` debits the proxy spend to the credit
#                  ledger (the BC-3 sweep is gated OFF in ``metering.sweeper`` when
#                  the mode is live, so exactly one meter charges).
#
# Runs on the SAME schedule as the BC-3 metering sweep — the in-process 5-minute
# heartbeat (``ee.extensions._sweeper_loop``) and the Tier 2 worker boot
# (``chat.runs.worker._startup``). Mirrors that sweep's shape: tenant-agnostic
# iteration, every spend op workspace-scoped, idempotent + safe to run repeatedly.
#
# IDEMPOTENT BY CONSTRUCTION:
#   * live   — ``ingest_tenant_spend`` is exactly-once per spend row (the
#              ``litellm:{request_id}`` ledger key + the high-water mark), so a
#              re-run bills nothing twice.
#   * shadow — ``reconcile_tenant_spend`` writes an append-only audit row and moves
#              no money, so a re-run only records another (consistent) compare. The
#              window is a fixed lookback ending "now"; overlapping windows across
#              ticks are harmless because shadow never debits.
# A per-tenant failure (proxy blip, one bad key) is logged and skipped so one
# tenant never wedges the whole sweep — the same isolation the BC-3 sweep uses.
#
# Created 2026-06-26 (feat/litellm-billing-cutover, WU-F): new entity.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

logger = logging.getLogger(__name__)

# Shadow-compare lookback. Each shadow tick reconciles the trailing window ending
# "now". Generous enough to overlap the sweep cadence (so no spend falls between
# ticks) — overlap is safe because shadow debits nothing. Live mode ignores this
# (its ingest is high-water-bounded, not window-bounded).
_SHADOW_WINDOW = timedelta(hours=24)


async def run_cutover_sweep(*, mode: str | None = None) -> dict[str, int]:
    """Run the LiteLLM billing-cutover spend logic for every provisioned tenant.

    Resolves the cutover ``mode`` (defaulting to the deployment's
    ``service.spend_mode()``) and dispatches per tenant:
      * ``off``    — returns immediately, sweeps nothing.
      * ``shadow`` — ``reconcile_tenant_spend`` per tenant (read-only compare).
      * ``live``   — ``ingest_tenant_spend`` per tenant (debit proxy spend).

    Returns a small summary dict ``{"tenants", "processed", "failed", "gaps",
    "credits"}`` for the caller's log line (``gaps`` only meaningful in shadow,
    ``credits`` only in live). Idempotent + safe to run repeatedly. A per-tenant
    error is logged and skipped — never raised — so one tenant can't wedge the
    sweep (it is retried next tick).
    """
    resolved = mode if mode is not None else provisioning_service.spend_mode()
    summary = {"tenants": 0, "processed": 0, "failed": 0, "gaps": 0, "credits": 0}

    if resolved == "off":
        # Nothing to do — BC-3 bills as today.
        return summary

    workspaces = await provisioning_service.list_provisioned_workspaces()
    summary["tenants"] = len(workspaces)
    if not workspaces:
        return summary

    if resolved == "shadow":
        until = datetime.now(UTC)
        since = until - _SHADOW_WINDOW
        for workspace in workspaces:
            try:
                rec = await provisioning_service.reconcile_tenant_spend(
                    workspace, since=since, until=until
                )
                summary["processed"] += 1
                if rec.coverage_gap:
                    summary["gaps"] += 1
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "run_cutover_sweep[shadow]: reconcile failed for workspace=%s "
                    "— will retry next tick",
                    workspace,
                )
        logger.info(
            "run_cutover_sweep[shadow]: reconciled %d/%d tenants over [%s,%s), "
            "%d coverage gap(s), %d failed — NO debits",
            summary["processed"],
            summary["tenants"],
            since.isoformat(),
            until.isoformat(),
            summary["gaps"],
            summary["failed"],
        )
        return summary

    if resolved == "live":
        for workspace in workspaces:
            try:
                result = await provisioning_service.ingest_tenant_spend(workspace)
                summary["processed"] += 1
                summary["credits"] += result.credits_debited
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "run_cutover_sweep[live]: spend ingest failed for workspace=%s "
                    "— will retry next tick",
                    workspace,
                )
        logger.info(
            "run_cutover_sweep[live]: ingested spend for %d/%d tenants -> %d credits, "
            "%d failed (LiteLLM is the sole meter; BC-3 gated off)",
            summary["processed"],
            summary["tenants"],
            summary["credits"],
            summary["failed"],
        )
        return summary

    # An unrecognised mode (mis-set env) — log loud and do nothing rather than
    # guess. ``effective_spend_mode`` only ever returns off|shadow|live, so this is
    # defensive against a hand-rolled caller passing a bad value.
    logger.warning(
        "run_cutover_sweep: unrecognised billing mode %r — sweeping nothing", resolved
    )
    return summary


__all__ = ["run_cutover_sweep"]
