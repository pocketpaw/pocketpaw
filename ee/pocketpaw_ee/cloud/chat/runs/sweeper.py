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
immediately instead of waiting for the heartbeat timeout. The append step
is silently skipped when ``POCKETPAW_REDIS_URL`` is unset (Tier 0
deployments) so no warning spam appears on every tick.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)

_DEFAULT_OLDER_THAN_MINUTES = 10
# Bound the lifetime of any stream the sweeper might resurrect via the
# append step (stream_exists/append_event race window) so a TTL-evicted key
# can't be brought back from the dead to live forever.
_STREAM_TTL_AFTER_INTERRUPT = 3600


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

    stale = await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
        ChatRunDoc.createdAt < cutoff,
    ).to_list()
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
    """Return the stream transport when Redis is configured, else ``None``.

    A Tier 0 deployment (EE installed but ``POCKETPAW_REDIS_URL`` unset) used
    to log a WARNING + traceback every 5 minutes from the heartbeat sweeper.
    Short-circuiting on the env var keeps those deployments quiet — the
    Mongo-only sweep still works.
    """
    if not os.environ.get("POCKETPAW_REDIS_URL", "").strip():
        return None
    try:
        return get_stream_transport()
    except Exception:
        # Env is set but the transport refused to construct (e.g. malformed
        # URL). One-off WARNING without traceback — operators get told once
        # per process start, not on every tick.
        logger.warning(
            "sweep_stale_runs: stream transport unavailable; interrupt events will not be appended"
        )
        return None
