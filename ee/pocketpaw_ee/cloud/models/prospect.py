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
# Updated 2026-07-29 (feat/growth-discovery): provenance for a row nobody typed
# — ``icp_id`` (the standing profile that found it) and ``source_urls`` (the
# pages the research actually read). Both empty on a manually created or
# imported prospect. Plus a (workspace, source, createdAt) index: the monthly
# discovery ceiling counts this workspace's discovered rows in the current
# period, and that count runs on every discovery run.
# Updated 2026-08-06 (feat/coupling-lead-to-prospect, T-7): ``lead_id`` — the
# site-form submission that created this row, when one did. The inbound funnel's
# provenance field, and the pointer /growth follows back to what the visitor
# actually typed (the prospect row deliberately carries none of it). No index:
# nothing queries by it, the link is followed in one direction only.

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
    # clay | directory | discovery | manual (validated at the DTO boundary)
    source: str
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
    # The standing ICP that discovered this row, when discovery did. None on a
    # typed or imported prospect.
    icp_id: str | None = None
    # The pages the research read to produce this row — the audit trail for a
    # prospect nobody typed. Empty on a manually created one.
    source_urls: list[str] = Field(default_factory=list)
    # The captured Lead that created this row (T-7). None on every prospect
    # that did not arrive through a site form. Set once, at creation.
    lead_id: str | None = None

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
        ]
