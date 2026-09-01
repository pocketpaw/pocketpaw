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
# THE BC-3 OFF-SWITCH (WU-F): this sweep is the ONLY place BC-3 actually charges —
# ``bill_run`` is never called inline in the chat run lifecycle, only from here (the
# ee/extensions heartbeat + the Tier 2 worker boot). When the billing cutover is in
# ``live`` mode (POCKETPAW_LITELLM_SPEND_MODE=live) LiteLLM is the sole meter, so
# this sweep MUST NOT run — otherwise the same usage is billed twice (once by the
# proxy-spend sweep, once here). ``sweep_unbilled_runs`` therefore checks the mode
# and no-ops in ``live``. In ``off`` and ``shadow`` it bills as today (shadow is a
# read-only compare layered ON TOP of unchanged BC-3 billing). Runs that go unbilled
# while ``live`` is in effect simply stay ``billed=False`` and are billed by LiteLLM
# instead — they are NOT back-billed by BC-3 if the mode is later turned off, which
# is the correct behaviour (the proxy already charged them).
#
# Created 2026-06-24 (integration/billing-credits, BC-3): new entity.
# Updated 2026-06-26 (feat/litellm-billing-cutover, WU-F): gated OFF in ``live`` mode
# — the single-meter guarantee. See "THE BC-3 OFF-SWITCH" above.
# Updated 2026-08-01 (AL-3, agent ledger run counting): this sweep now also emits
#   one ``paw.run.completed`` agent-ledger row per run it bills, via
#   ``_emit_run_completed``. WHY HERE and nowhere else: the value board's run
#   count and the wallet's spend must be derived from the SAME walk over the same
#   docs. A second pass — a separate sweeper, a hook on run completion, a nightly
#   job — is a second meter, and two meters over one quantity eventually disagree
#   (the usage-chart-vs-wallet bug we have already paid for once). Riding this
#   loop makes disagreement structurally impossible: a run is counted if and only
#   if it was billed. COUNT ONLY — the ledger row carries agent, surface and the
#   run id, and never tokens/cost/latency; those stay on the run doc and are read
#   federated by joining on the run id. The emit is fail-soft and post-bill, so a
#   broken ledger can never cost a workspace its billing (the drift that silence
#   buys is what AL-4's reconcile endpoint exists to surface).
# Updated 2026-09-02 (fix/metering-dated-pricing): the loop now reads each
#   ``BillResult`` back and tallies the runs whose cost came out ``unpriced``,
#   emitting ONE warning per tick naming the distinct model ids. An unpriced run
#   bills 0 credits, which writes no ledger row, so the tick is the last place
#   the fact still exists — after this loop it is gone. Same argument as the
#   ``ts`` note above: the sweep is where a run's truth is available, so it is
#   where the sweep has to record it.

from __future__ import annotations

import logging
from typing import Any

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

# The one ChatRunDoc context type that is NOT a member chatting in the app. A
# concierge run is a Paw Bar run, so it shares the surface bucket with AL-2's
# funnel rows and an owner reading "paw_bar" sees the conversation beats and the
# runs that produced them together. Every other context type (dm / group /
# pocket / session) maps to SURFACE_CHAT. The mapping lives HERE rather than in
# the OSS vocabulary module because ``context_type`` is ChatRunDoc's own EE-side
# vocabulary; the OSS ``surface_from_trigger`` maps Instinct's.
_CONCIERGE_CONTEXT_TYPE = "concierge"


async def _emit_run_completed(doc: Any) -> bool:
    """Count one billed terminal run in the agent ledger. NEVER raises.

    Emitter #5 of the agent-ledger design, and the only one that does not sit on
    a human's hot path — but the fail-soft contract is if anything stricter here:
    this runs inside the billing sweep, and BOOKKEEPING MUST NEVER COST A
    WORKSPACE ITS BILLING. Every failure mode is swallowed and logged at debug: a
    workspace the store factory refuses (``WorkspaceScopeRequired`` is real in
    cloud mode for a run with an empty workspace), a locked database, a duck-typed
    doc from a test. Same posture as ``instinct/store.py::_emit_ledger`` and
    ``paw_bar/ledger.py``.

    Four choices, each of which fails silently if it is wrong:

    * ``ref`` IS THE RUN ID. ``UNIQUE(kind, ref)`` therefore makes a re-sweep of
      the same run a no-op at the database, not at the caller — so the flag-loss
      path (``billed`` reset by a crash between debit and save, which the metering
      tests exercise deliberately) re-bills nothing and re-counts nothing.

    * WORKSPACE ROUTING USES ``doc.workspace`` — the SAME value this sweep already
      routes its own work by (``bill_run`` debits ``run_doc.workspace``). It is
      not re-derived from anything else in the row. Feeding the factory a value
      that is not a real workspace token makes ``_safe_workspace_dir`` raise
      INSIDE the guard above, and the row then vanishes with no trace at all —
      the exact silent under-count that was a review blocker on AL-1. The in-row
      ``workspace_id`` column takes the same value, so the ledger's tenancy filter
      and the wallet's debit agree by construction.

    * ``ts`` IS THE RUN'S OWN MOMENT (``ended_at``, else ``createdAt``), never the
      sweep's. The sweep drains a backlog FIFO and may run long after an outage;
      stamping sweep-time would pile a week of runs into one window and make every
      windowed count — including AL-4's reconcile — wrong in a way that looks like
      a traffic spike.

    * COUNT ONLY. No tokens, no cost, no latency, no status. All of it is already
      on the run doc, and the ref IS the run id, so a consumer that wants any of
      it joins on the key rather than reading a second copy that can drift from
      the first. That second copy is the two-meters bug in miniature and the row
      model has no field for it on purpose.

    Returns True when a NEW row landed; False on a replay OR on a swallowed
    failure. The sweep ignores the answer — it is here for tests and for a future
    caller that wants to distinguish the two.
    """
    try:
        from pocketpaw.agent_ledger.models import (
            ATTR_AGENT_ID,
            KIND_RUN_COMPLETED,
            SURFACE_CHAT,
            SURFACE_PAW_BAR,
            LedgerActor,
            LedgerRow,
        )
        from pocketpaw.stores import get_agent_ledger_store

        workspace = str(getattr(doc, "workspace", "") or "")
        agent_id = str(getattr(doc, "agent_id", "") or "")
        context_type = str(getattr(doc, "context_type", "") or "")
        # ``LedgerRow.ts`` is a string; the store's ``_normalize_ts`` coerces
        # whatever we hand it to aware-UTC ISO (a naive stamp — which is what
        # Mongo hands back — is read as UTC, which is what Mongo stores). Only
        # fall through to the model's "now" default when the doc carries no
        # moment at all.
        moment = getattr(doc, "ended_at", None) or getattr(doc, "createdAt", None)
        fields: dict[str, Any] = {"ts": moment.isoformat()} if moment is not None else {}

        row = LedgerRow(
            agent_id=agent_id,
            workspace_id=workspace,
            surface=(SURFACE_PAW_BAR if context_type == _CONCIERGE_CONTEXT_TYPE else SURFACE_CHAT),
            kind=KIND_RUN_COMPLETED,
            ref=str(getattr(doc, "run_id", "") or ""),
            # The sweep counted it, not a person — a run the agent did on its own
            # authority is still machinery from the board's point of view.
            actor=LedgerActor.SYSTEM.value,
            # The column is the key; the attribute is the copy, mirroring both
            # other emitters. An unattributed run omits it rather than carrying an
            # empty string that reads like a real id.
            attrs={ATTR_AGENT_ID: agent_id} if agent_id else {},
            **fields,
        )
        store = get_agent_ledger_store(workspace_id=workspace or None)
        return bool(await store.append(row))
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a billing sweep
        logger.debug(
            "agent-ledger run count skipped for run %s",
            getattr(doc, "run_id", "<unknown>"),
            exc_info=True,
        )
        return False


async def sweep_unbilled_runs(
    *,
    batch_limit: int = _SWEEP_BATCH_LIMIT,
    rate_card: RateCard | None = None,
    mode: str | None = None,
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

    WU-F single-meter gate: when the billing-cutover ``mode`` is ``live`` LiteLLM
    is the sole meter, so this BC-3 sweep MUST NOT charge — it returns 0 WITHOUT
    touching any wallet or flipping any ``billed`` flag (the off-switch that keeps
    exactly one meter charging; see the module header). ``off`` and ``shadow`` bill
    as today. ``mode`` defaults to the resolved deployment mode
    (``llm_provisioning.service.spend_mode()``, which honours the legacy bool);
    tests pass it explicitly so they don't depend on ambient settings.
    """
    if mode is None:
        # Lazy import — avoids a metering<->llm_provisioning module-load cycle and
        # mirrors the lazy settings reads elsewhere in the metering entity.
        from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

        mode = provisioning_service.spend_mode()
    if mode == "live":
        logger.debug(
            "sweep_unbilled_runs: billing mode is 'live' — LiteLLM is the sole "
            "meter, BC-3 per-run metering is gated OFF (no debit, no flag write)"
        )
        return 0

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
    unpriced: list[str] = []
    for doc in unbilled:
        try:
            result = await metering_service.bill_run(doc, rate_card=card)
        except Exception:
            # Leave ``billed`` False so the next tick retries this run; the
            # run:{run_id} key keeps any partial debit from double-applying.
            logger.exception(
                "sweep_unbilled_runs: failed to bill run %s (workspace=%s) — will retry",
                doc.run_id,
                doc.workspace,
            )
        else:
            billed += 1
            # AL-3 — count the run in the agent ledger, on the ELSE branch so it
            # runs only for a run that was actually billed and so its own
            # failures cannot land in the handler above. Inside that ``except``
            # a bookkeeping error would be logged as "failed to bill … will
            # retry" — a false statement about a run that WAS billed — and would
            # skip the ``billed`` increment, making the sweep under-report
            # itself. The emitter is fail-soft in its own right (see
            # ``_emit_run_completed``), so this line cannot raise; the ``else``
            # is the structural belt to that braces.
            await _emit_run_completed(doc)
            # C4 — an unpriced run bills 0 and marks itself billed, so it leaves
            # no ledger row and nothing downstream can count it. The tick's own
            # tally is the only place it can be counted, which is why it is
            # counted here rather than in a metrics system this codebase does not
            # have. One line per tick, not per run: a backlog on one bad model id
            # should read as one problem, not two hundred.
            if result.cost_source == "unpriced" and result.model:
                unpriced.append(result.model)

    if unpriced:
        logger.warning(
            "sweep_unbilled_runs: %d of %d runs billed $0 because nothing could "
            "price them — models: %s",
            len(unpriced),
            billed,
            ", ".join(sorted(set(unpriced))),
        )
    if billed:
        logger.info("sweep_unbilled_runs: billed %d terminal runs", billed)
    return billed
