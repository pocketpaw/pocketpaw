# ee/pocketpaw_ee/cloud/models/growth_blueprint.py — one version of a Growth
# brand profile's Brand Blueprint: the plan the owner edits as a draft, approves,
# and that drafting agents follow. Each row is one ``version`` of one profile's
# blueprint; ``status`` is draft, approved or superseded.
#
# Invariants (held by ``growth.social.blueprint``, not by an index): at most one
# ``approved`` and at most one ``draft`` per (workspace, profile_id). Approving a
# draft supersedes the previous approved; regenerating supersedes the previous
# draft. The unique (workspace, profile_id, version) index makes two writers
# racing for the same next version fail instead of forking history.
#
# The sections are plain dicts / lists of dicts, shaped and validated by
# ``growth.social.dto`` on the way in and out. ``roadmap`` is
# ``{"phases": [...]}`` with ISO-string dates (BSON has no date type). Numbers in a blueprint
# (pillar weights, channel cadences, goal targets) are owner-entered or copied
# from the profile; a model never writes one.
#
# Only ``ee.cloud.growth.social.blueprint`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class GrowthBlueprint(TimestampedDocument):
    """One version of a brand profile's blueprint."""

    workspace: str
    profile_id: str
    version: int
    status: str = "draft"
    identity: dict = Field(default_factory=dict)
    audience: dict = Field(default_factory=dict)
    voice: dict = Field(default_factory=dict)
    pillars: list[dict] = Field(default_factory=list)
    channels: list[dict] = Field(default_factory=list)
    goals: list[dict] = Field(default_factory=list)
    roadmap: dict = Field(default_factory=lambda: {"phases": []})
    learning_ids: list[str] = Field(default_factory=list)
    created_by: str = ""
    approved_by: str | None = None
    approved_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "growth_blueprints"
        indexes = [
            IndexModel([("workspace", 1), ("profile_id", 1), ("status", 1)]),
            IndexModel([("workspace", 1), ("profile_id", 1), ("version", 1)], unique=True),
        ]
