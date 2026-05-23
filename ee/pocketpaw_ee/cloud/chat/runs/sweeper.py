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

When the run's Redis stream is still alive, the sweeper appends an
``interrupted`` terminal event so any live SSE subscriber finalises
immediately instead of waiting for the heartbeat timeout.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)


async def sweep_stale_runs(
    *,
    older_than_minutes: int | None = None,
    older_than_seconds: int | None = None,
) -> int:
    """Mark queued/running runs older than the cutoff as ``interrupted``.

    Pass either ``older_than_minutes`` or ``older_than_seconds``; if both are
    omitted, defaults to 10 minutes. Returns the number of docs updated.
    """
    if older_than_seconds is not None:
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    else:
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes or 10)
    stale = await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
        ChatRunDoc.createdAt < cutoff,
    ).to_list()
    if not stale:
        return 0
    # Resolve the transport once per sweep; if Redis is unavailable we still
    # mark interruptions in Mongo, we just can't wake live SSE subscribers.
    try:
        transport = get_stream_transport()
    except Exception:
        logger.warning(
            "sweep_stale_runs: stream transport unavailable, interrupt events will not be appended",
            exc_info=True,
        )
        transport = None
    now = datetime.now(UTC)
    for doc in stale:
        doc.status = "interrupted"  # type: ignore[assignment]
        doc.ended_at = now
        await doc.save()
        if transport is not None:
            try:
                if await transport.stream_exists(doc.run_id):
                    await transport.append_event(doc.run_id, "interrupted", {"run_id": doc.run_id})
            except Exception:
                logger.exception(
                    "sweep_stale_runs: transport append failed for run %s",
                    doc.run_id,
                )
    logger.info("sweep_stale_runs: marked %d runs as interrupted", len(stale))
    return len(stale)
