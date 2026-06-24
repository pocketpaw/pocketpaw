# ee/pocketpaw_ee/cloud/metering/sweeper.py — the durable compute-cost metering
# sweep (BC-3, the Meter + Price primitives).
#
# Every TERMINAL chat run (completed AND the non-completed terminal states —
# cancelled / interrupted / failed; a partial run consumed tokens too) that has
# not yet been billed gets its compute cost charged to the workspace wallet
# EXACTLY ONCE. This runs as a system job on the SAME schedule as the stale-run
# sweeper (the in-process 5-minute heartbeat + the Tier 2 worker boot), so a
# crash between a run finishing and its bill landing is recovered on the next
# tick — bill-on-completion that survives process death.
#
# TENANT-AGNOSTIC by design: this is a system job that scans across workspaces,
# but every debit is workspace-scoped because the workspace travels on the run
# doc and ``bill_run`` debits ``run_doc.workspace``. There is no cross-tenant
# read of a wallet — only the run's own workspace is touched.
#
# IDEMPOTENT BY CONSTRUCTION: the query filters on ``billed == False`` (the cheap
# filter) and each ``bill_run`` debit is keyed on ``run:{run_id}`` against BC-1's
# unique ledger index (the real guard). Running the sweep twice — or two workers
# racing it — bills each run once: the second debit collides on the key and is a
# no-op, and the ``billed`` flag short-circuits the run out of later sweeps.
#
# Created 2026-06-24 (integration/billing-credits, BC-3): new entity.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.metering import service as metering_service
from pocketpaw_ee.cloud.metering.domain import RateCard
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)

# Terminal states whose runs are billable. ``completed`` is the happy path; the
# other three are non-completed terminals that still consumed tokens before they
# stopped, so they are billed for whatever the backend reported.
_TERMINAL_STATES = ["completed", "interrupted", "failed", "cancelled"]

# Cap per tick so a long-outage backlog can't wedge the heartbeat (mirrors the
# stale-run sweeper's batch limit). The next tick drains the next batch.
_SWEEP_BATCH_LIMIT = 200


async def sweep_unbilled_runs(
    *,
    batch_limit: int = _SWEEP_BATCH_LIMIT,
    rate_card: RateCard | None = None,
) -> int:
    """Bill every unbilled terminal chat run's compute cost, exactly once.

    Queries up to ``batch_limit`` runs in a terminal state where ``billed`` is
    False, oldest first (so the backlog drains FIFO), and bills each via
    ``metering.service.bill_run``. Returns the number of runs billed (including
    those that billed 0 credits but were marked billed). A per-run failure is
    logged and skipped — it stays unbilled and is retried next tick — so one bad
    run never wedges the whole sweep.

    ``rate_card`` is resolved ONCE for the whole batch (a single settings read)
    and passed into each ``bill_run`` so a 200-run sweep doesn't re-read settings
    200 times. Tests inject a card here to pin the rate.
    """
    unbilled = (
        await ChatRunDoc.find(
            {"status": {"$in": _TERMINAL_STATES}},
            ChatRunDoc.billed == False,  # noqa: E712 — Beanie field equality, not `is`
        )
        .sort("+createdAt")
        .limit(batch_limit)
        .to_list()
    )
    if not unbilled:
        return 0

    card = rate_card if rate_card is not None else metering_service.load_rate_card()

    billed = 0
    for doc in unbilled:
        try:
            await metering_service.bill_run(doc, rate_card=card)
            billed += 1
        except Exception:
            # Leave ``billed`` False so the next tick retries this run; the
            # run:{run_id} key keeps any partial debit from double-applying.
            logger.exception(
                "sweep_unbilled_runs: failed to bill run %s (workspace=%s) — will retry",
                doc.run_id,
                doc.workspace,
            )

    if billed:
        logger.info("sweep_unbilled_runs: billed %d terminal runs", billed)
    return billed
