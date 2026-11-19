# ee/pocketpaw_ee/cloud/models/prospect.py — outbound-engine prospect record
# for the /growth surface. Workspace-scoped; the company website ``domain`` is
# the dedupe key, enforced by a UNIQUE (workspace, domain) index so one tenant
# can never hold two rows for the same company while two tenants can each hold
# their own. Only ``ee.cloud.growth.service`` may import this doc class
# (import-linter "Growth" contract).
#
# ``name`` and ``company`` default to ``""`` (NOT YET KNOWN: a prospect may be
# just a domain until research fills the rest in). ``project_id`` is the client
# container (``cloud/projects``) and ``profile_id`` the brand (a Growth › Social
# ``SocialProfile``) the row belongs to (null only on legacy rows); both are
# indexed with the workspace in front. Provenance for a row nobody typed: ``icp_id`` (the
# standing profile that found it) and ``source_urls`` (the pages the research
# read). ``research`` holds the structured profile from the last research run
# (validated by ``growth.dto.ProspectResearch``) and ``researched_at`` when it
# ran. ``whatsapp_opt_out_at`` is set on a WhatsApp STOP and cleared by START;
# while it is set, no other reply turns ``opted_in`` back on.

from __future__ import annotations

from datetime import datetime

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
    # clay | directory | discovery | manual (validated at the DTO boundary)
    source: str
    # The client this prospect belongs to (``cloud/projects``), or None on a
    # workspace that doesn't use projects. Validated against the workspace by
    # the service before it is written.
    project_id: str | None = None
    # The brand (Growth › Social ``SocialProfile`` id) this prospect is worked
    # under.
    # Writes always set it (``growth.service.resolve_brand``); the type stays
    # optional so legacy null rows still load. The backfill script fixes those.
    profile_id: str | None = None
    tier: str = "unqualified"  # a | b | c | unqualified
    research_brief: str = ""
    emails: list[str] = Field(default_factory=list)
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    # When the prospect replied STOP (or another opt-out keyword) on WhatsApp.
    whatsapp_opt_out_at: datetime | None = None
    status: str = "new"  # new | qualified | drafted | in_sequence | replied | dead
    # The standing ICP that discovered this row, when discovery did. None on a
    # typed or imported prospect.
    icp_id: str | None = None
    # The pages the research read to produce this row — the audit trail for a
    # prospect nobody typed. Empty on a manually created one.
    source_urls: list[str] = Field(default_factory=list)
    research: dict | None = None
    researched_at: datetime | None = None

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
            # The discovery monthly ceiling: count this workspace's rows with
            # ``source="discovery"`` created since the period start. Runs on
            # every discovery run, before anything is filed.
            [("workspace", 1), ("source", 1), ("createdAt", -1)],
            # Brand-scoped list: the outreach views filter to one brand.
            [("workspace", 1), ("profile_id", 1)],
        ]
