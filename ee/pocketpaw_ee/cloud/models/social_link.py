# ee/pocketpaw_ee/cloud/models/social_link.py — a Growth › Social tracked short
# link: the code behind ``/api/v1/go/{code}``, the stored URL it redirects to,
# and its click counts, scoped to a workspace, profile and idea.
#
# Privacy invariant: the doc holds counts only. A click never stores anything
# about the requester (no IP, no user agent, no referrer).
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialLink(TimestampedDocument):
    """A short code that 302s to ``target_url`` and counts human clicks."""

    code: str
    workspace: str
    profile: str = ""
    idea: str
    target_url: str
    clicks: int = 0
    clicks_by_day: dict[str, int] = Field(default_factory=dict)

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_links"
        indexes = [
            IndexModel([("code", 1)], unique=True),
            IndexModel([("workspace", 1), ("idea", 1)]),
        ]
