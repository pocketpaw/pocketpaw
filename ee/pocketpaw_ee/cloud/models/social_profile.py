# ee/pocketpaw_ee/cloud/models/social_profile.py — the Growth › Social company
# profile. A workspace may hold several (one per brand it posts for), each
# holding what the
# setup wizard typed (owner, company, website, the six description fields, the
# business-shape enums) and the latest website analysis.
#
# ``description`` and ``analysis`` are plain dicts, shaped and validated by
# ``growth.social.dto`` on the way in and out. Only ``ee.cloud.growth.social.
# service`` may import this doc class (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialProfile(TimestampedDocument):
    """A workspace's social-content company profile."""

    workspace: str
    owner_name: str = ""
    company_name: str = ""
    website: str | None = None
    description: dict = Field(default_factory=dict)
    team_size: str | None = None
    monthly_revenue: str | None = None
    role: str | None = None
    business_model: str | None = None
    category: str | None = None
    analysis_status: str = "none"
    analysis_error: str | None = None
    analysis: dict | None = None
    analyzed_at: datetime | None = None
    onboarding_completed_at: datetime | None = None
    characters: list[dict] = Field(default_factory=list)

    class Settings:
        name = "growth_social_brands"
        indexes = [[("workspace", 1), ("updatedAt", -1)]]
