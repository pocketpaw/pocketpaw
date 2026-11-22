# ee/pocketpaw_ee/cloud/models/social_task.py — one Growth › Social "By hand"
# task: a recurring to-do the founder does themselves for one brand (e.g. "2
# Reddit comments" daily, "Fill tracking" weekly), scoped to a workspace and one
# social profile. ``repeat`` is "daily" or "weekly"; ``weekday`` (0 = Monday,
# Python ``date.weekday()``) is set only for weekly tasks. ``done_on`` holds the
# UTC dates it was ticked, oldest first, trimmed to the newest 60 by the service.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import date

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialTask(TimestampedDocument):
    """One recurring by-hand task on a brand."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    profile: str
    title: str
    link: str | None = None
    repeat: str = "daily"
    weekday: int | None = None
    done_on: list[date] = Field(default_factory=list)

    class Settings:
        name = "growth_social_tasks"
        indexes = [
            [("workspace", 1), ("profile", 1), ("createdAt", 1)],
        ]
