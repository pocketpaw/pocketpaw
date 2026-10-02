"""Stale-run sweeper: moves runs that stopped progressing to ``interrupted``.

A ``running`` run is judged by its liveness stamp,
``coalesce(last_heartbeat_at, started_at, createdAt)``; the worker refreshes
``last_heartbeat_at`` on a timer, so a run is only stale once its worker stopped
beating. A ``queued`` run is judged by ``createdAt`` against its own cutoff
(``domain.queued_timeout_minutes``, env-configurable, default 10): nobody has
picked it up, so nothing can beat, and the usual cause is every worker slot
being busy.

Each write is a conditional atomic update that re-checks the stale predicate, so
a run the worker completed, beat, or claimed between the read and the write is
left exactly as the worker wrote it. Only runs this sweep actually moved get a
stream frame:

- a ``running`` run gets an ``interrupted`` frame only if its stream still
  exists, so a live SSE subscriber finalises instead of waiting for its own
  timeout;
- a ``queued`` run always gets one, creating the stream if needed (a run that
  never started may have no stream), with ``reason: "queue_timeout"`` and a
  "system was busy" message, which is also stored on the doc's ``error``. When
  arq later dequeues it, ``mark_running`` refuses and the worker drops it.

Two cadences share this: the web process's periodic sweep (default cutoffs) and
the Tier 2 worker's boot sweep (a short explicit cutoff applied to both arms).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.chat.runs.domain import queued_timeout_minutes
from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)

_DEFAULT_OLDER_THAN_MINUTES = 10
# Bound the lifetime of any stream the sweeper might resurrect via the
# append step (stream_exists/append_event race window) so a TTL-evicted key
# can't be brought back from the dead to live forever.
_STREAM_TTL_AFTER_INTERRUPT = 3600
# Cap per tick so a long-outage backlog can't wedge the periodic sweep.
_SWEEP_BATCH_LIMIT = 200

QUEUE_TIMEOUT_CODE = "run.queue_timeout"
QUEUE_TIMEOUT_MESSAGE = "The system was too busy to start this reply. Please try sending it again."


async def sweep_stale_runs(
    *,
    older_than_minutes: int | None = None,
    older_than_seconds: int | None = None,
) -> int:
    """Mark queued/running runs that stopped progressing as ``interrupted``.

    Queued runs are stale when created before the cutoff; running runs when
    their last heartbeat (else ``started_at``, else ``createdAt``) is before it.
    Pass exactly one of ``older_than_minutes`` or ``older_than_seconds`` (the
    other must be ``None``) to apply one cutoff to both. Both ``None`` uses 10
    minutes for running runs and ``queued_timeout_minutes()`` for queued ones;
    passing both raises ``ValueError`` so the caller's intent stays unambiguous.
    Returns the number of docs this sweep actually moved to ``interrupted``.
    """
    if older_than_minutes is not None and older_than_seconds is not None:
        raise ValueError(
            "sweep_stale_runs: pass exactly one of older_than_minutes / older_than_seconds"
        )
    if older_than_seconds is not None:
        cutoff = queued_cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    elif older_than_minutes is not None:
        cutoff = queued_cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    else:
        cutoff = datetime.now(UTC) - timedelta(minutes=_DEFAULT_OLDER_THAN_MINUTES)
        queued_cutoff = datetime.now(UTC) - timedelta(minutes=queued_timeout_minutes())

    stale_filter = _stale_filter(cutoff, queued_cutoff)
    stale = await ChatRunDoc.find(stale_filter).limit(_SWEEP_BATCH_LIMIT).to_list()
    if not stale:
        return 0

    transport = _resolve_transport()
    collection = ChatRunDoc.get_pymongo_collection()
    now = datetime.now(UTC)
    swept = 0
    for doc in stale:
        # Re-assert the stale predicate IN the write. Between the read above and
        # this line the worker may have completed the run, or beaten, or a
        # queued run may have been claimed; any of those makes the filter miss
        # and the run is left exactly as the worker wrote it.
        was_queued = doc.status == "queued"
        update: dict = {"status": "interrupted", "ended_at": now}
        if was_queued:
            update["error"] = QUEUE_TIMEOUT_MESSAGE
        result = await collection.update_one(
            {"run_id": doc.run_id, **stale_filter},
            {"$set": update},
        )
        if result.modified_count != 1:
            continue
        swept += 1
        if transport is not None:
            try:
                if was_queued:
                    # A run that never started may have no stream, and its
                    # reader blocks on the missing key until its own ~30 minute
                    # cap. Create the stream so the terminal frame lands now.
                    await transport.append_event(
                        doc.run_id,
                        "interrupted",
                        {
                            "run_id": doc.run_id,
                            "reason": "queue_timeout",
                            "code": QUEUE_TIMEOUT_CODE,
                            "message": QUEUE_TIMEOUT_MESSAGE,
                        },
                    )
                    await transport.set_ttl(doc.run_id, _STREAM_TTL_AFTER_INTERRUPT)
                elif await transport.stream_exists(doc.run_id):
                    await transport.append_event(doc.run_id, "interrupted", {"run_id": doc.run_id})
                    # The append above will recreate the key if it was just
                    # TTL-evicted between stream_exists and append_event, so
                    # set a fresh TTL unconditionally to bound the stream's
                    # lifetime in that race.
                    await transport.set_ttl(doc.run_id, _STREAM_TTL_AFTER_INTERRUPT)
            except Exception:
                logger.exception(
                    "sweep_stale_runs: transport append failed for run %s",
                    doc.run_id,
                )
    if swept:
        logger.info("sweep_stale_runs: marked %d runs as interrupted", swept)
    return swept


def _stale_filter(cutoff: datetime, queued_cutoff: datetime | None = None) -> dict:
    """Mongo filter for "queued too long, or running with no recent heartbeat".

    ``coalesce(last_heartbeat_at, started_at, createdAt) < cutoff`` spelled as
    three ``$or`` arms, because the fallbacks only apply when the newer stamp is
    absent: a running run with a FRESH heartbeat and an ancient ``createdAt`` is
    alive. ``{"field": None}`` matches both a missing field and an explicit null,
    which covers docs written before these fields existed. ``queued_cutoff``
    (default ``cutoff``) applies to the queued arm alone.
    """
    return {
        "$or": [
            {"status": "queued", "createdAt": {"$lt": queued_cutoff or cutoff}},
            {"status": "running", "last_heartbeat_at": {"$lt": cutoff}},
            {"status": "running", "last_heartbeat_at": None, "started_at": {"$lt": cutoff}},
            {
                "status": "running",
                "last_heartbeat_at": None,
                "started_at": None,
                "createdAt": {"$lt": cutoff},
            },
        ]
    }


def _resolve_transport():
    """Return the stream transport, or ``None`` if construction fails."""
    try:
        return get_stream_transport()
    except Exception:
        logger.warning("sweep_stale_runs: stream transport unavailable")
        return None
