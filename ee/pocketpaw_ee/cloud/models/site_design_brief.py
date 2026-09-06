# ee/pocketpaw_ee/cloud/models/site_design_brief.py — a captured design brief for
# the site REGENERATION import path (rebuild), workspace-scoped.
#
# Created 2026-09-04 (IR-2a, feat/sites-import-design-brief).
#
# WHY THIS IS ITS OWN COLLECTION AND NOT A FIELD ON A POCKET OR A SITE: rebuild
# mints neither. The /sites surface routes a run to REFINE whenever a pocket_id
# rides in its meta, so a create flow must run with no pocket and let the agent
# mint one through its own create tool. That leaves the brief with nothing to hang
# on between the capture finishing and the generation starting, which is exactly
# the window this document covers. The copy (mirror) path is unaffected — it still
# mints an html pocket up front and stamps its report on the Site doc.
#
# The brief payload is stored as a plain dict rather than a typed sub-document on
# purpose: ``sites.design_brief`` owns the shape and versions it, and reads go
# through ``design_brief.load_brief`` so a version this build cannot read fails
# loudly instead of being silently defaulted into something plausible.
"""The captured design brief a rebuild-mode import generates from."""

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteDesignBrief(TimestampedDocument):
    """One captured source page, on its way to becoming a native site.

    ``status`` is the only thing a reader needs to tell the three states apart
    that otherwise look identical from outside: the capture has not run yet, it
    ran and failed, or it ran and there is a brief to generate from. ``error``
    carries a SAFE message only (the same rule the import report follows) because
    every site viewer in the workspace can read it.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    source_url: str
    status: str = "queued"  # queued | capturing | ready | failed
    error: str = ""
    # The DesignBrief, dumped in json mode. Empty until the capture succeeds.
    brief: dict[str, Any] = Field(default_factory=dict)
    # Set once a generation run has been started from this brief, so a retry can
    # tell "captured, never used" from "already generating". Empty otherwise.
    consumed_by_run: str = ""

    class Settings:
        name = "site_design_briefs"
        indexes = [
            # The workspace's own briefs, newest first — the only read shape the
            # import panel needs.
            [("workspace", 1), ("createdAt", -1)],
        ]
