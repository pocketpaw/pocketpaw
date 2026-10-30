# ee/pocketpaw_ee/cloud/models/social_pulse.py — the latest market Pulse for one Growth › Social
# profile: the sources it read (subreddits, X searches), the themes the analyst found, each with
# why it matters to the brand, the words people use, the public posts and comments behind it,
# and post suggestions. One row per (workspace, profile), replaced on every refresh. ``profile``
# is a social profile id, or "icp:<icp_id>" for a hunt's market pulse. ``manual_refreshed_at`` is
# the last time a person refreshed it; the daily sweep only keeps Pulses someone uses fresh.
#
# ``sources`` and ``themes`` are plain dicts shaped and validated by ``growth.social.dto``. Only
# ``ee.cloud.growth.social.service`` may import this doc class (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialPulse(TimestampedDocument):
    """A social profile's latest market research."""

    workspace: str
    profile: str
    sources: list[dict] = Field(default_factory=list)
    themes: list[dict] = Field(default_factory=list)
    refreshed_at: datetime | None = None
    manual_refreshed_at: datetime | None = None

    class Settings:
        name = "growth_social_pulses"
        indexes = [[("workspace", 1), ("profile", 1)]]
