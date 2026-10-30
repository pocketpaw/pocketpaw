# ee/cloud/activity/__init__.py
# Created: 2026-05-13 (feat/mission-control-facade) — per-workspace in-memory
# activity buffer feeding Mission Control's live ticker. The durable record
# of every agent decision still lives in Pawprints (Instinct audit); this
# module is the ephemeral feed.
"""Activity buffer package.

Only ``buffer`` matters for now — it owns the per-workspace ring buffer and
the bus subscription that fills it. ``register_activity_listeners`` is
wired from ``ee.cloud.__init__.mount_cloud`` after ``init_realtime``.
"""

from pocketpaw_ee.cloud.activity.buffer import (
    ActivityEvent,
    get_buffer,
    register_activity_listeners,
)

__all__ = ["ActivityEvent", "get_buffer", "register_activity_listeners"]
