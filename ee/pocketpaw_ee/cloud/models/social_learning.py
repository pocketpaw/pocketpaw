# ee/pocketpaw_ee/cloud/models/social_learning.py — one Growth › Social learning:
# a short rule about what works for a profile's posts (at most 300 characters)
# with its evidence (at most 1000), proposed by a closed experiment or by the
# weekly-review model call.
#
# Invariant: only ``status == "approved"`` rows ever reach the ideas prompt.
# Status moves proposed -> approved, proposed -> retired or approved -> retired,
# and never back; ``approved_at`` / ``retired_at`` stamp those moves.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialLearning(TimestampedDocument):
    """A rule the founder may approve for the drafting prompt."""

    workspace: str
    profile: str
    text: str
    source_experiment: str = ""
    evidence: str = ""
    status: str = "proposed"
    approved_at: datetime | None = None
    retired_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_learnings"
        indexes = [IndexModel([("workspace", 1), ("profile", 1), ("status", 1)])]
