# ee/pocketpaw_ee/cloud/models/site_dashboard.py — a Paw Site's AI dashboards and their
# revision history. Written and read ONLY through ``pocketpaw_ee.sites.dashboard_service``.
#
# A dashboard is NOT a pocket: pocket share links, the pocket specialist and pocket
# sources would each open a path around the Data dashboard's D1 guards, so every widget
# query is run by the dashboard service through ``site_data.dashboard_sql``.
#
#   * ``widgets`` are stored as plain dicts in the wire shape of
#     ``site_data.dashboard_dto.DashboardWidget`` minus ``spec`` (compiled on read).
#   * ``version`` is the optimistic-concurrency counter: every save is a compare-and-set
#     on it (``If-Match``), so two owners editing at once get a 409, never a lost edit.
#   * ``schema_fingerprint`` / ``known_tables`` are the D1 schema as of the last save,
#     which the health check compares against to report drift and new tables.
#   * ``SiteDashboardRevision`` holds the state BEFORE each save (the last 20 per
#     dashboard), so any save can be undone by restoring the version before it.
from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


class SiteDashboard(Document):
    """One AI dashboard over a site's D1 database."""

    workspace_id: str
    site_pocket_id: str
    name: str
    version: int = 1
    widgets: list[dict[str, Any]] = Field(default_factory=list)
    schema_fingerprint: str = ""
    known_tables: list[str] = Field(default_factory=list)
    generated: dict[str, Any] | None = None
    reason: str = "create"  # why the current version was saved (edit, restore:<v> …)
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime

    class Settings:
        name = "site_dashboards"
        indexes = [
            IndexModel([("workspace_id", 1), ("site_pocket_id", 1)], name="site_dashboard_site"),
        ]


class SiteDashboardRevision(Document):
    """A dashboard as it was before one save."""

    workspace_id: str
    dashboard_id: str
    version: int
    name: str
    widgets: list[dict[str, Any]] = Field(default_factory=list)
    actor: str
    reason: str
    at: datetime

    class Settings:
        name = "site_dashboard_revisions"
        indexes = [
            IndexModel(
                [("dashboard_id", 1), ("version", -1)], unique=True, name="uq_dashboard_revision"
            ),
        ]


__all__ = ["SiteDashboard", "SiteDashboardRevision"]
