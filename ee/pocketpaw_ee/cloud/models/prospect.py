# ee/pocketpaw_ee/cloud/models/prospect.py — outbound-engine prospect record
# for the /growth surface (G-1, feat/growth-g1). Workspace-scoped; the company
# website ``domain`` is the dedupe key, enforced by a UNIQUE
# (workspace, domain) index so one tenant can never hold two rows for the same
# company while two tenants can each hold their own. Only
# ``ee.cloud.growth.service`` may import this doc class (import-linter
# "Growth" contract).
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — the prospect
# store. Later slices (ingestion, drafts, gated sends) build on this doc.
# Updated 2026-07-28 (feat/growth-projects): ``name`` and ``company`` default to
# ``""`` — a prospect may be JUST A DOMAIN until research fills the rest in, so
# the stored fields have to be able to say "not yet known". Existing rows all
# carry values, so this is a widening with no migration. Also ``project_id`` —
# the client container (``cloud/projects``) a prospect belongs to — plus a
# (workspace, project_id, createdAt) index, because an agency's default view is
# one client's pipeline rather than the whole workspace's.

from __future__ import annotations

from beanie import Indexed
from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class Prospect(TimestampedDocument):
    """One outbound prospect (a company + who to reach there) in a workspace."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # Both default to "" — NOT YET KNOWN. A pasted list of bare domains is a
    # legitimate import; research fills these in later.
    name: str = ""
    company: str = ""
    # Company website domain, lowercased at the service boundary — the dedupe key.
    domain: str
    source: str  # clay | directory | manual (validated at the DTO boundary)
    # The client this prospect belongs to (``cloud/projects``), or None on a
    # workspace that doesn't use projects. Validated against the workspace by
    # the service before it is written.
    project_id: str | None = None
    tier: str = "unqualified"  # a | b | c | unqualified
    research_brief: str = ""
    emails: list[str] = Field(default_factory=list)
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    status: str = "new"  # new | qualified | drafted | in_sequence | replied | dead

    class Settings:
        name = "growth_prospects"
        indexes = [
            # Dedupe key: one row per company domain per workspace. The leading
            # ``workspace`` keeps the constraint tenant-local — two workspaces
            # can each prospect the same company.
            IndexModel(
                [("workspace", 1), ("domain", 1)],
                unique=True,
                name="uq_workspace_domain",
            ),
            # List cursor: the prospects view pages newest-first per workspace.
            [("workspace", 1), ("createdAt", -1)],
            # Project-scoped list cursor: an agency's default view is ONE
            # client's pipeline, so the project filter belongs in the index
            # rather than as a post-filter over the whole workspace.
            [("workspace", 1), ("project_id", 1), ("createdAt", -1)],
        ]
