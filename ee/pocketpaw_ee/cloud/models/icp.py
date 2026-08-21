# ee/pocketpaw_ee/cloud/models/icp.py — the ICP record for the /growth
# discovery engine: a STANDING description of who a workspace wants, plus the
# cadence the discovery cron runs it on. Workspace-scoped. Only
# ``ee.cloud.growth.service`` may import this doc class (import-linter "Growth"
# contract).
#
# Created 2026-07-29 (feat/growth-discovery): first slice of the discovery
# engine — the ICP store, its CRUD, and the research seam that reads it.
#
# NO unique index. Unlike ``Prospect`` (where ``domain`` is a real dedupe
# identity), two ICPs in one workspace may legitimately share a name: an agency
# running "dental clinics" for two different clients holds two of them, scoped
# by ``project_id``. The (workspace, status, cadence) index is what the
# discovery cron actually queries — the due-ICP scan — so that is the one that
# earns its keep.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class Icp(TimestampedDocument):
    """One Ideal Customer Profile in a workspace."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    # Free text on purpose — this is what the research READS. See the ``Icp``
    # value object in ``growth/domain.py`` for why it is not a filter tree.
    criteria: str
    # The client this ICP prospects for (``cloud/projects``), or None on a
    # workspace that doesn't use projects. Validated against the workspace by
    # the service before it is written.
    project_id: str | None = None
    # Hard constraints the research applies rather than weighs.
    geography: str = ""
    exclusions: str = ""
    cadence: str = "off"  # off | daily | weekly — off is the default
    max_per_run: int = 10
    status: str = "active"  # active | paused
    # When the discovery cron last ran this ICP. Read by the operator surface
    # ("has this thing actually been running?"); the sweep's due-check keys on
    # the cadence + the tick, not on this, so a missed tick is a missed tick
    # rather than a backlog that fires all at once on recovery.
    last_run_at: datetime | None = None

    class Settings:
        name = "growth_icps"
        indexes = [
            # The list view: one workspace's ICPs, newest first.
            [("workspace", 1), ("createdAt", -1)],
            # The discovery cron's due scan — every ACTIVE ICP with a
            # scheduled cadence, across tenants (the one deliberate global
            # read, justified at the service seam).
            [("status", 1), ("cadence", 1)],
        ]
