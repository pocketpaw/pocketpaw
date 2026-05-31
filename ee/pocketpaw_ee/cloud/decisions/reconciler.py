# reconciler.py — 60s journal-cursor reconciler for the Decision projection.
# Created: 2026-05-26 (RFC 09 Slice 4 — feat/rfc-09-slice-4-reconciler)
#
# Purpose
# -------
# RFC 09 §"Architecture — three layers in concert" — Layer 3 of the
# fan-out story. Producers in Slices 2 and 3 co-locate ``journal.append
# + projection.apply`` so the hot path delivers chain-forming events
# inline. The reconciler is the safety net for everything the hot path
# missed:
#
#   * A producer that called ``journal.append`` but crashed before
#     ``projection.apply`` (rare — see RFC 09 § Captain Decision 11).
#   * Events written by a tool that does not know about the projection
#     (e.g. the soul CLI writing direct journal entries from a script).
#   * Partial-crash recovery — a process boot whose journal has events
#     past the projection's persisted cursor.
#
# The reconciler polls ``journal.replay_from(cursor)`` every 60s (env-
# tunable). For each new entry it calls ``projection.apply(entry)``;
# the projection itself is idempotent on ``seq`` and on chain
# correlation_id so re-applying an entry that already landed via the
# hot path is a no-op. Cursor advances on every successful apply so
# the next tick starts where the last one stopped.
#
# Pattern reference
# -----------------
# Modelled after:
#   * ``ee/pocketpaw_ee/cloud/pockets/journal_stream_router.py``
#     (the polling-cursor SSE precedent at 500ms — same shape, slower
#     cadence). The reconciler differs by feeding the projection
#     instead of an SSE stream and by reading the journal via
#     ``journal.replay_from`` (the public API) instead of reaching into
#     the backend's row iterator.
#   * ``ee/pocketpaw_ee/cloud/cycles/scheduler.py`` (the asyncio loop
#     + ``app.state`` task-handle pattern). The reconciler uses the same
#     start/stop wiring so the shutdown hook can cancel cleanly.
#
# Failure isolation
# -----------------
# Per-entry ``projection.apply`` failures are logged as warnings and
# swallowed — the reconciler advances the cursor PAST the failed entry
# only when the entry's seq is known (``entry.seq is not None``).
# A failed apply on a seq-bearing entry still advances the cursor
# because re-applying the same row on the next tick would hit the
# same failure mode; the right recovery is operator intervention, not
# an infinite retry loop. A failed apply on a seq-less entry (older
# soul-protocol wheels) leaves the cursor where it was so the next
# tick re-tries the row.
#
# Mid-tick crash recovery
# ----------------------
# If the process dies between two ``apply`` calls in the same tick,
# the projection's own cursor (persisted by the store after every
# successful apply via ``projection._store.set_cursor``) is the source
# of truth on restart. The reconciler's local ``_cursor`` is rebuilt
# from ``projection.cursor`` on every start so a fresh process resumes
# exactly where the prior process left off.
#
# Observability
# -------------
# Every tick logs a heartbeat at INFO with ``cursor`` / ``applied`` /
# ``errors`` / ``lag_seconds``. The ``GET /api/v1/decisions/_reconciler/
# status`` admin endpoint (see ``decisions.router``) exposes the same
# numbers without the log line so an operator can poll for liveness.
#
# Tests
# -----
# See ``tests/ee/test_decision_reconciler.py`` for the contract pins:
# tick happy path, mid-chain crash + resume, multi-producer cursor
# race, idempotency, failure isolation.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

logger = logging.getLogger(__name__)

_TASK_KEY = "_decisions_reconciler_task"
_DEFAULT_INTERVAL_SECONDS = 60
_ENV_INTERVAL = "POCKETPAW_DECISIONS_RECONCILER_INTERVAL_SECONDS"


def _iter_entries_with_seq(journal: Any, since_seq: int) -> list[tuple[Any, int]]:
    """Walk the journal backend's row iterator and emit
    ``(EventEntry, seq)`` pairs whose seq is strictly greater than
    ``since_seq``.

    Reaches into ``journal._backend`` for the same reason
    ``journal_stream_router._drain_since`` does — the public
    ``Journal.replay_from`` API flattens seq away, and the installed
    soul-protocol wheel (0.3.1) does not populate ``EventEntry.seq``
    on the entry it returns. Keeping the seq cursor hand-in-hand with
    the row iterator is the only way today to advance the projection
    cursor across ticks.

    Returns a list (not a generator) so the caller can wrap the whole
    walk in one try/except without losing partial work — the journal
    backend's cursor is closed after iteration.
    """
    backend = journal._backend  # noqa: SLF001 — see docstring rationale
    tail = backend.last_entry()
    if tail is None:
        return []
    _, tail_seq = tail
    if tail_seq < since_seq:
        return []
    pairs: list[tuple[Any, int]] = []
    # ``since_seq=0`` on a brand-new cursor needs to include seq=0
    # because the SQLite backend assigns seq starting at 0. The
    # ``> local_cursor`` skip in the caller handles dedup on warm
    # restarts where the cursor already advanced past seq=0.
    start = max(0, since_seq)
    rows = backend._conn.execute(  # noqa: SLF001
        "SELECT * FROM events WHERE seq >= ? ORDER BY seq ASC", (start,)
    )
    for row in rows:
        entry, seq = backend._row_to_entry(row)  # noqa: SLF001
        pairs.append((entry, seq))
    return pairs


def _interval_seconds() -> int:
    """Read the reconciler tick interval from env, default 60s."""
    raw = os.environ.get(_ENV_INTERVAL, "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int — falling back to %d seconds",
            _ENV_INTERVAL,
            raw,
            _DEFAULT_INTERVAL_SECONDS,
        )
        return _DEFAULT_INTERVAL_SECONDS
    return max(1, value)


@dataclass
class ReconcilerStatus:
    """Snapshot of the reconciler's current state. Exposed via the
    admin endpoint so an operator can confirm the loop is alive."""

    cursor: int = 0
    last_tick_ts: datetime | None = None
    last_tick_applied: int = 0
    last_tick_errors: int = 0
    lag_seconds: float | None = None
    total_ticks: int = 0
    total_applied: int = 0
    total_errors: int = 0
    last_error_ts: datetime | None = None
    last_error_message: str | None = None
    started_at: datetime | None = None
    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS

    # Per-tick error-rate window — drop entries older than 1h on every
    # poll so the ``errors_last_hour`` counter is a real rolling window
    # rather than a lifetime sum. Cheap; bounded by tick rate.
    error_history: list[datetime] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Wire shape for the admin endpoint."""
        now = datetime.now(UTC)
        cutoff = now.timestamp() - 3600
        recent_errors = [ts for ts in self.error_history if ts.timestamp() >= cutoff]
        self.error_history = recent_errors
        return {
            "cursor": self.cursor,
            "last_tick_ts": (self.last_tick_ts.isoformat() if self.last_tick_ts else None),
            "last_tick_applied": self.last_tick_applied,
            "last_tick_errors": self.last_tick_errors,
            "lag_seconds": self.lag_seconds,
            "total_ticks": self.total_ticks,
            "total_applied": self.total_applied,
            "total_errors": self.total_errors,
            "errors_last_hour": len(recent_errors),
            "last_error_ts": (self.last_error_ts.isoformat() if self.last_error_ts else None),
            "last_error_message": self.last_error_message,
            "started_at": (self.started_at.isoformat() if self.started_at else None),
            "interval_seconds": self.interval_seconds,
        }


class DecisionReconciler:
    """60s in-process polling reconciler for the Decision projection.

    Use:
        reconciler = DecisionReconciler()
        await reconciler.start()      # spawn the background task
        ...
        await reconciler.stop()       # cancel + await cleanly

    For unit tests:
        reconciler = DecisionReconciler()
        await reconciler.tick()       # one iteration, no background task

    Per-process singleton via ``get_reconciler()``. The cloud bootstrap
    wires a singleton onto the FastAPI ``app.state`` so the admin
    endpoint can read the status without a thread-local.
    """

    def __init__(self, interval_seconds: int | None = None) -> None:
        self._interval = interval_seconds if interval_seconds is not None else _interval_seconds()
        self._status = ReconcilerStatus(interval_seconds=self._interval)
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    @property
    def status(self) -> ReconcilerStatus:
        return self._status

    @property
    def interval_seconds(self) -> int:
        return self._interval

    async def tick(self) -> int:
        """Run one reconciler iteration. Returns the number of events
        applied this tick. Safe to call from tests without ``start()``.
        """
        # Lazy imports so a test that exercises ``tick()`` directly
        # does not have to bootstrap the whole cloud module.
        from pocketpaw.journal_dep import get_journal
        from pocketpaw_ee.cloud.decisions.service import get_decision_graph

        applied = 0
        errors = 0
        last_error_message: str | None = None
        tick_start = datetime.now(UTC)

        try:
            journal = get_journal()
            graph = get_decision_graph()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions.reconciler: failed to resolve journal/graph: %s",
                exc,
                exc_info=True,
            )
            self._record_error(str(exc), tick_start)
            return 0

        projection = graph.projection
        # Local cursor follows projection.cursor — Slice 1b persists the
        # cursor in decisions.db after every successful apply, so a
        # restart resumes exactly where the prior process stopped.
        local_cursor = projection.cursor
        # Use the backend's row iterator directly so we get (entry,
        # seq) tuples even on soul-protocol wheels that don't round-
        # trip ``EventEntry.seq`` via ``replay_from``. This is the
        # same pattern ``journal_stream_router._drain_since`` uses
        # (Slice 4 RFC reference). Without this hop, the reconciler
        # cannot advance the projection's cursor on 0.3.1 because the
        # entries arriving via ``replay_from`` carry no seq.
        try:
            entries_with_seq = _iter_entries_with_seq(journal, local_cursor)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "decisions.reconciler: journal iteration raised: %s",
                exc,
                exc_info=True,
            )
            self._record_error(str(exc), tick_start)
            return 0

        # Walk the (entry, seq) pairs. The local seq is the source of
        # truth for cursor advancement — we stamp it onto the entry's
        # model_copy so the projection's ``if seq > cursor`` guard
        # works even on wheels where ``replay_from`` doesn't round-
        # trip seq.
        #
        # Dedup contract: skip entries whose seq is at or below the
        # last-applied watermark. The initial watermark is ``local
        # _cursor - 1`` so cold-start (cursor=0) still applies seq=0
        # while warm restarts (cursor=N) skip seq<=N. After every
        # successful apply we bump the watermark and force the
        # projection's cursor to ``seq + 1`` so the seq=0 wrinkle in
        # ``apply()``'s ``if seq > cursor`` guard cannot leave the
        # cursor stuck.
        applied_watermark = local_cursor - 1
        for entry, seq in entries_with_seq:
            if seq <= applied_watermark:
                continue
            # Stamp the local seq onto a copy of the entry so the
            # projection's apply() sees it.
            try:
                stamped = entry.model_copy(update={"seq": seq})
            except Exception:  # noqa: BLE001
                stamped = entry

            try:
                projection.apply(stamped)
                applied += 1
                applied_watermark = seq
                # Force the projection's cursor forward to seq+1 so a
                # subsequent tick whose journal_seq=0 entry was just
                # folded does not re-apply it. The projection's own
                # ``if seq > cursor`` guard handles cursor advancement
                # for seq>=1; the +1 stamp here covers the seq=0
                # corner.
                try:
                    projection._cursor = max(projection._cursor, seq + 1)  # noqa: SLF001
                    projection._store.set_cursor(seq + 1)  # noqa: SLF001
                except Exception:  # noqa: BLE001 — cursor persistence is best-effort
                    pass
            except Exception as exc:  # noqa: BLE001
                errors += 1
                last_error_message = str(exc)
                logger.warning(
                    "decisions.reconciler: projection.apply failed for "
                    "entry id=%s action=%s seq=%s: %s",
                    getattr(entry, "id", "?"),
                    getattr(entry, "action", "?"),
                    seq,
                    exc,
                    exc_info=True,
                )
                # Still advance the watermark so the next tick does
                # not loop on the same bad row.
                applied_watermark = seq

        tick_end = datetime.now(UTC)
        # ``lag_seconds`` = wall-clock between this tick and the
        # journal's tail. Falls back to None when the journal is empty.
        lag = self._compute_lag(journal, tick_end)

        self._status.cursor = projection.cursor
        self._status.last_tick_ts = tick_end
        self._status.last_tick_applied = applied
        self._status.last_tick_errors = errors
        self._status.lag_seconds = lag
        self._status.total_ticks += 1
        self._status.total_applied += applied
        self._status.total_errors += errors
        if errors:
            self._status.last_error_ts = tick_end
            self._status.last_error_message = last_error_message

        logger.info(
            "decisions.reconciler: tick cursor=%d applied=%d errors=%d lag_seconds=%s",
            projection.cursor,
            applied,
            errors,
            f"{lag:.2f}" if lag is not None else "?",
        )
        return applied

    def _record_error(self, message: str, ts: datetime) -> None:
        """Track an error from outside the apply loop (e.g. journal
        resolution failure) so the admin endpoint surfaces it."""
        self._status.last_tick_ts = ts
        self._status.last_tick_errors = 1
        self._status.last_error_ts = ts
        self._status.last_error_message = message
        self._status.total_errors += 1
        self._status.error_history.append(ts)
        self._status.total_ticks += 1

    def _compute_lag(self, journal: Any, now: datetime) -> float | None:
        """Wall-clock seconds between ``now`` and the last journal entry."""
        try:
            backend = journal._backend  # noqa: SLF001
            tail = backend.last_entry()
        except Exception:  # noqa: BLE001
            return None
        if tail is None:
            return 0.0
        last_entry, _ = tail
        last_ts = getattr(last_entry, "ts", None)
        if last_ts is None:
            return None
        try:
            return max(0.0, (now - last_ts).total_seconds())
        except Exception:  # noqa: BLE001
            return None

    async def _run_loop(self) -> None:
        """The background loop body. Wakes on a timeout OR the stop
        event — the latter lets ``stop()`` return promptly without
        waiting for the next tick.
        """
        logger.info("decisions.reconciler: loop started (interval=%ds)", self._interval)
        self._status.started_at = datetime.now(UTC)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                # If we got here without TimeoutError, stop was signalled.
                break
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                logger.info("decisions.reconciler: loop cancelled — exiting")
                raise

            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — already logged in tick()
                logger.exception("decisions.reconciler: tick raised")

    async def start(self) -> None:
        """Spawn the background loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="decisions-reconciler")

    async def stop(self) -> None:
        """Cancel + await the loop. Safe to call multiple times."""
        if self._task is None:
            return
        self._stop_event.set()
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None


# ---------------------------------------------------------------------------
# Module-level singleton — mount_cloud installs one of these onto app.state
# ---------------------------------------------------------------------------


_RECONCILER: DecisionReconciler | None = None


def get_reconciler() -> DecisionReconciler:
    """Return the process singleton, lazily constructing one if needed.

    The admin endpoint calls this so an operator querying status
    before ``mount_cloud`` runs (rare — startup race) still gets a
    valid (empty) status payload back.
    """
    global _RECONCILER
    if _RECONCILER is None:
        _RECONCILER = DecisionReconciler()
    return _RECONCILER


def reset_reconciler_for_tests() -> None:
    """Drop the singleton so each test gets a fresh instance."""
    global _RECONCILER
    _RECONCILER = None


async def start_reconciler(app: FastAPI) -> None:
    """Wire the singleton onto ``app.state`` and spawn the loop.
    Mirrors ``cycles.scheduler.start_in_process_scheduler``."""
    reconciler = get_reconciler()
    setattr(app.state, _TASK_KEY, reconciler)
    await reconciler.start()


async def stop_reconciler(app: FastAPI) -> None:
    """Cancel + await the loop attached to ``app.state``."""
    reconciler: DecisionReconciler | None = getattr(app.state, _TASK_KEY, None)
    if reconciler is None:
        return
    await reconciler.stop()
    setattr(app.state, _TASK_KEY, None)


__all__ = [
    "DecisionReconciler",
    "ReconcilerStatus",
    "get_reconciler",
    "reset_reconciler_for_tests",
    "start_reconciler",
    "stop_reconciler",
]
