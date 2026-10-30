# AiVisibilitySite Beanie document — a Staff site's AI visibility settings and the
# state of its latest requested check (one row per site).
#
# ``questions`` are the owner's (PUT /sites/{id}/ai-visibility/questions). ``status``
# is the latest requested check: none (never requested) | pending (queued) | running
# | done | failed. ``requested_at`` is stamped when a check is QUEUED, so the 24-hour
# owner limit and the monthly sweep both see a check still in flight and never queue
# a second. ``last_check_id`` points at the newest finished AiVisibilityCheck.
#
# Only ``ee.cloud.ai_visibility.service`` / ``service_admin`` import this doc
# (import-linter "AiVisibility" contract).

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class AiVisibilitySite(TimestampedDocument):
    """Per-site AI visibility settings and latest check state."""

    workspace: str
    site_id: str
    questions: list[str] = Field(default_factory=list)
    status: str = "none"
    requested_at: datetime | None = None
    ran_at: datetime | None = None
    last_check_id: str | None = None
    error: str = ""

    class Settings(TimestampedDocument.Settings):
        name = "ai_visibility_sites"
        indexes = [
            IndexModel([("site_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("site_id", 1)]),
            IndexModel([("requested_at", 1)]),
        ]
