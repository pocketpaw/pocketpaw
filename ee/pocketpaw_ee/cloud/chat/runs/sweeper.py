"""Stale-run sweeper.

Updated: 2026-09-27 (fix/chat-run-heartbeat) — a ``running`` run is now judged
by its liveness stamp, ``coalesce(last_heartbeat_at, started_at, createdAt)``,
not by ``createdAt``. The worker refreshes ``last_heartbeat_at`` on a timer while
it drives a run, so a run is only stale once its worker has stopped beating.
Before this, any run older than the cutoff was interrupted however healthy it
was: the browser got a terminal ``interrupted`` frame at 10-15 minutes, stopped
listening, and a refresh showed only the user's message. A ``queued`` run is
still judged by ``createdAt`` (nobody has picked it up, so nothing can beat).

The write is now a conditional atomic update that re-checks the stale predicate,
instead of a full-document ``save()`` of a doc read moments earlier. The old
save could land after the worker had completed the run and overwrite
``completed`` (and its ``assistant_message_id``) with ``interrupted``. The
``interrupted`` stream frame is only appended for runs this sweep actually moved.

If the backend process dies mid-run, the executor's asyncio task is gone but
Mongo still says ``running``. The sweeper marks anything that has stopped making
progress past the threshold as ``interrupted`` so the client can render a retry
affordance instead of subscribing to a stream nobody is writing to.

Two cadences share this:
- The web process's periodic sweep (every 5 minutes, 10-minute cutoff) catches
  runs abandoned by a web-process restart or a dead worker.
- The Tier 2 worker's boot sweep (5-second cutoff) catches runs orphaned by
  the previous worker that just crashed.

If the run's stream buffer is still live, the sweeper appends an
``interrupted`` terminal event so any live SSE subscriber finalises
immediately instead of waiting for its own stream timeout.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

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


async def sweep_stale_runs(
    *,
    older_than_minutes: int | None = None,
    older_than_seconds: int | None = None,
) -> int:
    """Mark queued/running runs that stopped progressing as ``interrupted``.

    Queued runs are stale when created before the cutoff; running runs when
    their last heartbeat (else ``started_at``, else ``createdAt``) is before it.
    Pass exactly one of ``older_than_minutes`` or ``older_than_seconds`` (the
    other must be ``None``). Both ``None`` defaults to 10 minutes; passing
    both raises ``ValueError`` so the caller's intent stays unambiguous.
    Returns the number of docs this sweep actually moved to ``interrupted``.
    """
    if older_than_minutes is not None and older_than_seconds is not None:
        raise ValueError(
            "sweep_stale_runs: pass exactly one of older_than_minutes / older_than_seconds"
        )
    if older_than_seconds is not None:
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    elif older_than_minutes is not None:
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    else:
        cutoff = datetime.now(UTC) - timedelta(minutes=_DEFAULT_OLDER_THAN_MINUTES)

    stale_filter = _stale_filter(cutoff)
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
        result = await collection.update_one(
            {"run_id": doc.run_id, **stale_filter},
            {"$set": {"status": "interrupted", "ended_at": now}},
        )
        if result.modified_count != 1:
            continue
        swept += 1
        if transport is not None:
            try:
                if await transport.stream_exists(doc.run_id):
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


def _stale_filter(cutoff: datetime) -> dict:
    """Mongo filter for "queued too long, or running with no recent heartbeat".

    ``coalesce(last_heartbeat_at, started_at, createdAt) < cutoff`` spelled as
    three ``$or`` arms, because the fallbacks only apply when the newer stamp is
    absent: a running run with a FRESH heartbeat and an ancient ``createdAt`` is
    alive. ``{"field": None}`` matches both a missing field and an explicit null,
    which covers docs written before these fields existed.
    """
    return {
        "$or": [
            {"status": "queued", "createdAt": {"$lt": cutoff}},
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
