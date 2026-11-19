# ee/pocketpaw_ee/cloud/models/growth_blueprint_unit.py — one version of one unit
# of a Growth brand's Blueprint. A unit is ``brand``, ``strategy``, ``plan`` or
# ``play:<id>`` (a play id from ``growth.social.play_catalog``); each unit runs its
# own version chain (draft, approved, superseded), so every part approves on its
# own. Drafting agents read the approved units stitched into one Blueprint.
#
# Invariants (held by ``growth.social.blueprint``, not by an index): at most one
# ``approved`` and one ``draft`` per (workspace, profile_id, unit); approving
# marks the draft approved before superseding the rest, so a crash leaves two
# approved, never zero, and reads take the highest version. The unique
# (workspace, profile_id, unit, version) index makes two writers racing for the
# same next version fail instead of forking history. ``rev`` is bumped by every
# write to the row; a draft edit is conditional on the rev it was read at.
#
# ``body`` is a plain dict shaped and validated by
# ``growth.social.blueprint_units_dto`` (one body model per unit kind). Numbers in
# a body (weights, cadences, budgets, baselines, targets) are owner-entered or
# carried from the legacy Blueprint; a model never writes one.
#
# Only ``ee.cloud.growth.social.blueprint`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class GrowthBlueprintUnit(TimestampedDocument):
    """One version of one Blueprint unit of a brand profile."""

    workspace: str
    profile_id: str
    unit: str
    version: int
    status: str = "draft"
    rev: int = 0
    body: dict = Field(default_factory=dict)
    created_by: str = ""
    approved_by: str | None = None
    approved_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "growth_blueprint_units"
        indexes = [
            IndexModel([("workspace", 1), ("profile_id", 1), ("status", 1)]),
            IndexModel(
                [("workspace", 1), ("profile_id", 1), ("unit", 1), ("version", 1)], unique=True
            ),
        ]
