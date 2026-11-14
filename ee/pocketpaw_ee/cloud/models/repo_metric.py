# ee/pocketpaw_ee/cloud/models/repo_metric.py — our own daily copy of one Growth ›
# Social product repo's GitHub numbers. GitHub keeps repo traffic for 14 days only,
# so the growth worker's 06:30 UTC sweep stores one row per (workspace, repo, day),
# unique. ``day`` is a ``YYYY-MM-DD`` string (UTC): the complete day before the
# sweep ran. ``views`` / ``uniques`` / ``clones`` / ``clone_uniques`` are that day's
# traffic; ``stars`` / ``forks`` are read live at ``captured_at``. ``referrers`` is
# GitHub's rolling 14-day top list as of capture (``{referrer, count, uniques}``),
# not a per-day figure. ``traffic_available`` is false when the token is missing or
# lacks push access; the traffic fields are then 0.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class (import-linter
# "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class RepoMetric(TimestampedDocument):
    """One product repo's stars, forks and traffic for one UTC day."""

    workspace: str
    repo: str
    day: str
    stars: int = 0
    forks: int = 0
    views: int = 0
    uniques: int = 0
    clones: int = 0
    clone_uniques: int = 0
    referrers: list[dict] = Field(default_factory=list)
    traffic_available: bool = False
    captured_at: datetime

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_repo_metrics"
        indexes = [IndexModel([("workspace", 1), ("repo", 1), ("day", 1)], unique=True)]
