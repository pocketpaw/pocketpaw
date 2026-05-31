# _action_sweeper.py — Abandon-path sweeper for parked Instinct Actions.
# Created: 2026-05-26 (RFC 09 Slice 4 — feat/rfc-09-slice-4-reconciler)
#
# Purpose
# -------
# RFC 09 Producer 4 (e) — the abandon path. The Instinct store has no
# TTL or expire column on ``instinct_actions``; a parked write whose
# approver never acts sits in ``pending`` forever, leaving the matching
# Decision-Graph chain open indefinitely. This sweeper closes the gap:
# periodically scans for parked Actions older than a configurable TTL
# and:
#
#   1. Emits ``decision.completed(passed=False, action_outcome=
#      "abandoned", reason="parked_ttl_expired_<N>d")`` via the
#      canonical ``record_decision_completed`` helper — closes the
#      Decision-Graph chain so ``DecisionGraph.find`` no longer surfaces
#      the chain as in-flight.
#   2. Marks the Instinct Action as ``ActionStatus.EXPIRED`` so the UI
#      no longer offers the approver a stale Approve / Reject button.
#
# Captain decision (RFC 09 § Slice 4 (a) open question): the sweeper
# does BOTH — close the chain AND flip the Action — because the two
# states are naturally paired (an action nobody can approve any more
# should not look pending in the UI either). The decision is documented
# here so a future captain can flip it back to chain-only by removing
# the ``ActionStatus.EXPIRED`` write block.
#
# Pattern reference
# -----------------
# Modelled after ``ee/pocketpaw_ee/cloud/chat/runs/sweeper.py`` (the
# stale-run sweeper) — same shape: periodic asyncio task that wakes,
# queries for stale rows, mutates state, logs the count. The
# Decision-Graph sweeper differs in two ways:
#
#   * The store backing parked Actions is SQLite (``InstinctStore``),
#     not MongoDB (``ChatRunDoc``). The query uses ``aiosqlite``
#     directly because the store has no ``find_older_than`` helper.
#   * Emit-then-mutate ordering matters here — the chain close must
#     land before the Action is flipped to ``expired``, otherwise an
#     observer reading the Action state could see it expired while the
#     chain still reads as in-flight.
#
# Idempotency
# -----------
# Re-sweeping the same Action twice would emit ``decision.completed``
# twice. The projection's ``apply()`` is idempotent on the chain
# correlation_id (the second close is a no-op once the chain is closed),
# but the journal would carry the duplicate row. To avoid the duplicate:
# the sweeper filters on ``status = 'pending'`` so an already-expired
# Action is skipped. The Action state flip is what makes idempotency
# work — once an Action is ``expired``, it never re-enters the sweeper's
# candidate set.
#
# TTL
# ---
# Default 30 days, env-tunable via
# ``POCKETPAW_DECISIONS_ABANDON_TTL_DAYS``. The RFC notes 30 days is
# "significantly longer than the 24h pending-chain anomaly threshold"
# so the sweeper only acts on truly abandoned actions — a 24h-stale
# chain is an operational warning (surface via the reconciler's
# pending-chains gauge), not a sweepable terminal.
#
# Tests
# -----
# See ``tests/ee/test_action_sweeper.py`` for the contract pins:
# TTL-based fire, correct chain close payload, idempotency, no-sweep
# when nothing is over TTL.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import aiosqlite
from fastapi import FastAPI
from soul_protocol.spec.journal import Actor

logger = logging.getLogger(__name__)

_TASK_KEY = "_decisions_action_sweeper_task"
_DEFAULT_TTL_DAYS = 30
_DEFAULT_INTERVAL_SECONDS = 3600  # one pass per hour — TTL is in days
_SWEEP_BATCH_LIMIT = 200


def _ttl_days() -> int:
    """Read the abandon-path TTL from env, falling back to 30 days."""
    raw = os.environ.get("POCKETPAW_DECISIONS_ABANDON_TTL_DAYS", "").strip()
    if not raw:
        return _DEFAULT_TTL_DAYS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_DECISIONS_ABANDON_TTL_DAYS=%r is not an int — falling back to %d days",
            raw,
            _DEFAULT_TTL_DAYS,
        )
        return _DEFAULT_TTL_DAYS
    if value < 1:
        logger.warning(
            "POCKETPAW_DECISIONS_ABANDON_TTL_DAYS=%d is < 1 — clamped to 1",
            value,
        )
        return 1
    return value


def _interval_seconds() -> int:
    """Read the sweeper interval from env, default 1 hour."""
    raw = os.environ.get("POCKETPAW_DECISIONS_ABANDON_INTERVAL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_DECISIONS_ABANDON_INTERVAL_SECONDS=%r is not an int — "
            "falling back to %d seconds",
            raw,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    return max(1, value)


def _get_instinct_store() -> Any | None:
    """Resolve the singleton InstinctStore, or return ``None`` when
    unavailable (tests, smoke contexts without ee.api wired)."""
    try:
        from pocketpaw_ee.api import get_instinct_store

        return get_instinct_store()
    except Exception:  # noqa: BLE001
        logger.warning("decisions.action_sweeper: InstinctStore unavailable", exc_info=True)
        return None


def _parse_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _parse_blob(parameters_text: Any) -> dict[str, Any] | None:
    """Pull the ``_pocket_write`` blob out of the persisted ``parameters``
    JSON column, returning ``None`` when the row is not a pocket-write
    Action (no blob = no chain to close)."""
    if not parameters_text:
        return None
    if isinstance(parameters_text, dict):
        params = parameters_text
    else:
        try:
            params = json.loads(parameters_text)
        except (TypeError, ValueError):
            return None
    if not isinstance(params, dict):
        return None
    blob = params.get("_pocket_write")
    return blob if isinstance(blob, dict) else None


async def _list_abandoned_actions(
    store: Any, *, cutoff: datetime, limit: int = _SWEEP_BATCH_LIMIT
) -> list[dict[str, Any]]:
    """Return rows for parked Actions whose ``created_at`` is older than
    ``cutoff``. Returns up to ``limit`` rows so a long-outage backlog
    cannot wedge the sweeper.

    ``InstinctStore`` does not expose a public "find pending older than"
    helper, so the sweeper reads via aiosqlite directly. Same pattern
    ``router._persist_edits`` uses for ad-hoc updates.
    """
    # Ensure the schema exists — on a brand-new store the
    # ``instinct_actions`` table is created lazily by the first
    # ``propose`` call. The sweeper must tolerate the empty-store case
    # (test fixtures, fresh deploys) without exploding on the missing
    # table.
    try:
        await store._ensure_schema()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.warning(
            "decisions.action_sweeper: store._ensure_schema failed",
            exc_info=True,
        )
        return []

    rows: list[dict[str, Any]] = []
    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, pocket_id, parameters, created_at
              FROM instinct_actions
             WHERE status = ?
               AND created_at < ?
             ORDER BY created_at ASC
             LIMIT ?
            """,
            ("pending", cutoff.isoformat(), limit),
        ) as cursor:
            async for row in cursor:
                rows.append(dict(row))
    return rows


async def _mark_action_expired(store: Any, action_id: str) -> None:
    """Flip ``instinct_actions.status`` from ``pending`` to ``expired``.

    The Slice 4 brief introduces ``expired`` as a new informal state —
    ``ActionStatus`` does NOT carry it (the enum is closed at
    pending/approved/rejected/executed/failed). Writing the string
    directly avoids a coordinated enum bump; readers that fetch by enum
    will see the row drop out of every named bucket and that's exactly
    what we want — the row is no longer in any actionable state.
    """
    async with aiosqlite.connect(store._db_path) as db:  # noqa: SLF001
        await db.execute(
            "UPDATE instinct_actions SET status = ? WHERE id = ?",
            ("expired", action_id),
        )
        await db.commit()


def _chain_actor_system(*, workspace_id: str, pocket_id: str) -> Actor:
    """Build the Actor recorded on the sweeper-emitted ``decision.completed``.

    ``kind="system"`` keeps the projection's approver count honest —
    the sweeper is not a human and is not the agent that proposed; it's
    the system telling the chain "this never landed."
    """
    return Actor(
        kind="system",
        id="system:decisions_action_sweeper",
        scope_context=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
    )


def _emit_abandoned_chain_close(
    *,
    correlation_id: UUID,
    workspace_id: str,
    pocket_id: str,
    parked_policy_event_id: UUID | None,
    ttl_days: int,
) -> bool:
    """Emit ``decision.completed(passed=False, action_outcome=
    "abandoned")`` for one chain. Returns True on success, False on
    skip / failure. ``causation_id`` chains the close back to the
    parked ``policy.evaluated(passed=False)`` so the projection's edge
    graph carries policy → close as one causal arrow."""
    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    payload: dict[str, Any] = {
        "passed": False,
        "action_outcome": "abandoned",
        "reason": f"parked_ttl_expired_{ttl_days}d",
    }
    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=_chain_actor_system(workspace_id=workspace_id, pocket_id=pocket_id),
            scope=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
            payload=payload,
            causation_id=parked_policy_event_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "decisions.action_sweeper: chain close emit failed for "
            "correlation_id=%s — reconciler will catch up on next tick",
            correlation_id,
            exc_info=True,
        )
        return False
    return True


async def sweep_abandoned_actions(*, ttl_days: int | None = None) -> int:
    """Sweep parked Instinct Actions older than ``ttl_days``.

    For each abandoned Action that carries a parked ``_pocket_write``
    blob: emit ``decision.completed(abandoned)`` to close the chain,
    then mark the Action ``expired``. Returns the count of Actions
    swept. Safe to call multiple times — once an Action is ``expired``
    it falls out of the ``status = 'pending'`` filter so the sweeper
    will not re-process it.
    """
    days = ttl_days if ttl_days is not None else _ttl_days()
    cutoff = datetime.now(UTC) - timedelta(days=days)

    store = _get_instinct_store()
    if store is None:
        return 0

    rows = await _list_abandoned_actions(store, cutoff=cutoff)
    if not rows:
        return 0

    swept = 0
    for row in rows:
        action_id = row["id"]
        blob = _parse_blob(row.get("parameters"))
        if blob is None:
            # Non-pocket-write parked Action — no chain to close.
            # Still flip to ``expired`` so the UI stops offering an
            # approval button on a stale row, but skip the chain emit.
            try:
                await _mark_action_expired(store, action_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "decisions.action_sweeper: failed to mark non-pocket-write Action %s expired",
                    action_id,
                    exc_info=True,
                )
            continue

        correlation_id = _parse_uuid(blob.get("correlation_id"))
        workspace_id = str(blob.get("workspace_id") or "")
        pocket_id = str(row.get("pocket_id") or "")
        parked_policy_event_id = _parse_uuid(blob.get("parked_policy_event_id"))

        if correlation_id is not None:
            _emit_abandoned_chain_close(
                correlation_id=correlation_id,
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                parked_policy_event_id=parked_policy_event_id,
                ttl_days=days,
            )
        # Flip the Action even if the chain emit failed — the journal
        # row is the source of truth and the Slice 4 reconciler will
        # catch any missed apply. Leaving the Action pending after a
        # failed emit would put the row back into the next sweep batch
        # and produce duplicate journal rows on retry.
        try:
            await _mark_action_expired(store, action_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "decisions.action_sweeper: failed to mark Action %s expired",
                action_id,
                exc_info=True,
            )
            continue

        swept += 1

    logger.info(
        "decisions.action_sweeper: swept %d abandoned actions (ttl_days=%d)",
        swept,
        days,
    )
    return swept


async def _run_sweeper_loop() -> None:
    """The background loop body. Runs forever, sleeping between passes.

    Per-pass exceptions are caught and logged so one bad pass cannot
    take down the loop for everyone else. ``CancelledError`` propagates
    so the shutdown hook can cancel-and-await cleanly.
    """
    interval = _interval_seconds()
    logger.info(
        "decisions.action_sweeper: in-process loop started (interval=%ds, ttl=%dd)",
        interval,
        _ttl_days(),
    )
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("decisions.action_sweeper: loop cancelled — exiting")
            raise

        try:
            await sweep_abandoned_actions()
        except Exception:  # noqa: BLE001
            logger.exception("decisions.action_sweeper: pass failed")


async def start_action_sweeper(app: FastAPI) -> None:
    """Start the abandon-path sweeper. Idempotent — calling start twice
    is a no-op (the second call sees the existing task in app state and
    bails). Mirrors ``cycles.scheduler.start_in_process_scheduler``.
    """
    existing = getattr(app.state, _TASK_KEY, None)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_run_sweeper_loop(), name="decisions-action-sweeper")
    setattr(app.state, _TASK_KEY, task)


async def stop_action_sweeper(app: FastAPI) -> None:
    """Cancel + await the loop. Safe to call multiple times."""
    task = getattr(app.state, _TASK_KEY, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    setattr(app.state, _TASK_KEY, None)


__all__ = [
    "start_action_sweeper",
    "stop_action_sweeper",
    "sweep_abandoned_actions",
]
