# ee/pocketpaw_ee/cloud/launch_controls/service.py: read and write the singleton
# ``launch_controls`` row.
#
# * ``get_launch_controls()`` is what every guard calls. It serves a per-process
#   snapshot for ``CACHE_TTL_SECONDS`` (30 s), so a flip made in Mongo or through
#   the platform endpoint reaches the web process and the arq worker within 30 s
#   with no restart. A read failure returns the defaults with ``read_failed=True``,
#   logged at ERROR: the pause flags then fail open (the per-workspace credit gate
#   still holds) and the daily ceiling, which reads the flag, fails closed. The
#   failed snapshot is cached like any other, so an outage logs once per TTL.
# * ``update_launch_controls()`` is the operator write. It upserts the row and
#   drops this process's snapshot, so the writer sees its own flip at once.
#
# The clock is the module attribute ``_clock`` so tests can move time.

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud.launch_controls.domain import LaunchControls
from pocketpaw_ee.cloud.models.launch_controls import LAUNCH_CONTROLS_KEY, LaunchControlsDoc

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0

_clock = time.monotonic
_cache: tuple[float, LaunchControls] | None = None


def invalidate_cache() -> None:
    global _cache
    _cache = None


def _to_domain(doc: LaunchControlsDoc | None) -> LaunchControls:
    if doc is None:
        return LaunchControls()
    return LaunchControls(
        signups_paused=doc.signups_paused,
        free_usage_paused=doc.free_usage_paused,
        daily_free_ceiling_credits=doc.daily_free_ceiling_credits,
    )


async def _read() -> LaunchControls:
    try:
        # global-read: the platform-wide singleton, not tenant data.
        doc = await LaunchControlsDoc.find_one(LaunchControlsDoc.key == LAUNCH_CONTROLS_KEY)
    except Exception:
        logger.error(
            "launch_controls read failed; pause flags fail open, daily ceiling fails closed",
            exc_info=True,
        )
        return LaunchControls(read_failed=True)
    return _to_domain(doc)


async def get_launch_controls() -> LaunchControls:
    """The current launch controls, at most ``CACHE_TTL_SECONDS`` old."""
    global _cache
    now = _clock()
    if _cache is not None and now - _cache[0] < CACHE_TTL_SECONDS:
        return _cache[1]
    controls = await _read()
    _cache = (now, controls)
    return controls


async def update_launch_controls(changes: dict[str, Any], *, operator_id: str) -> LaunchControls:
    """Apply ``changes`` (field name to value) to the singleton row, creating it
    if absent, and return the result. Callers validate the values."""
    now = datetime.now(UTC)
    # global-read: the platform-wide singleton, not tenant data.
    doc = await LaunchControlsDoc.find_one(LaunchControlsDoc.key == LAUNCH_CONTROLS_KEY)
    if doc is None:
        doc = LaunchControlsDoc(**changes, updated_at=now, updated_by=operator_id)
        await doc.insert()
    else:
        for name, value in changes.items():
            setattr(doc, name, value)
        doc.updated_at = now
        doc.updated_by = operator_id
        await doc.save()
    invalidate_cache()
    # no-event: platform switch, nothing downstream subscribes; the write is audited
    # by the platform route and other processes pick it up on their cache TTL.
    return _to_domain(doc)


__all__ = [
    "CACHE_TTL_SECONDS",
    "get_launch_controls",
    "invalidate_cache",
    "update_launch_controls",
]
