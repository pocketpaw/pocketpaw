"""Stale-run sweeper.

If the backend process dies mid-run, the executor's asyncio task is gone but
Mongo still says ``running``. The sweeper marks anything that's been sitting
in queued/running past the threshold as ``interrupted`` so the client can
render a retry affordance instead of subscribing to a stream nobody is
writing to.

Two cadences share this:
- The in-process heartbeat (every 5 minutes, 10-minute cutoff) catches runs
  abandoned by a web-process restart.
- The Tier 2 worker's boot sweep (5-second cutoff) catches runs orphaned by
  the previous worker that just crashed.

If the run's stream buffer is still live, the sweeper appends an
``interrupted`` terminal event so any live SSE subscriber finalises
immediately instead of waiting for the heartbeat timeout.
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
# Cap per tick so a long-outage backlog can't wedge the heartbeat.
_SWEEP_BATCH_LIMIT = 200


async def sweep_stale_runs(
    *,
    older_than_minutes: int | None = None,
    older_than_seconds: int | None = None,
) -> int:
    """Mark queued/running runs older than the cutoff as ``interrupted``.

    Pass exactly one of ``older_than_minutes`` or ``older_than_seconds`` (the
    other must be ``None``). Both ``None`` defaults to 10 minutes; passing
    both raises ``ValueError`` so the caller's intent stays unambiguous.
    Returns the number of docs updated.
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

    stale = (
        await ChatRunDoc.find(
            {"status": {"$in": ["queued", "running"]}},
            ChatRunDoc.createdAt < cutoff,
        )
        .limit(_SWEEP_BATCH_LIMIT)
        .to_list()
    )
    if not stale:
        return 0

    transport = _resolve_transport()
    now = datetime.now(UTC)
    for doc in stale:
        doc.status = "interrupted"  # type: ignore[assignment]
        doc.ended_at = now
        await doc.save()
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
    logger.info("sweep_stale_runs: marked %d runs as interrupted", len(stale))
    return len(stale)


def _resolve_transport():
    """Return the stream transport, or ``None`` if construction fails."""
    try:
        return get_stream_transport()
    except Exception:
        logger.warning("sweep_stale_runs: stream transport unavailable")
        return None
