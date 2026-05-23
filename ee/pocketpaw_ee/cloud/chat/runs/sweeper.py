"""Stale-run sweeper.

If the backend process dies mid-run, the executor's asyncio task is gone but
Mongo still says ``running``. The sweeper marks anything that's been sitting
in queued/running past the threshold as ``interrupted`` so the client can
render a retry affordance instead of subscribing to a stream nobody is
writing to.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

logger = logging.getLogger(__name__)


async def sweep_stale_runs(*, older_than_minutes: int = 10) -> int:
    """Mark queued/running runs older than ``older_than_minutes`` as ``interrupted``.

    Returns the number of docs updated.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    stale = await ChatRunDoc.find(
        {"status": {"$in": ["queued", "running"]}},
        ChatRunDoc.createdAt < cutoff,
    ).to_list()
    if not stale:
        return 0
    now = datetime.now(UTC)
    for doc in stale:
        doc.status = "interrupted"  # type: ignore[assignment]
        doc.ended_at = now
        await doc.save()
    logger.info("sweep_stale_runs: marked %d runs as interrupted", len(stale))
    return len(stale)
