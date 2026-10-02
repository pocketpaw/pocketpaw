# SiteTemplate Beanie document — a user-saved Paw Sites template.
#
# A template is a FROZEN SNAPSHOT of a site pocket's authored content, never a
# pointer to the live site: editing or deleting the source pocket leaves the
# template as it was saved, and deleting the template leaves every pocket made
# from it untouched. ``snapshot`` holds exactly the pockets service's
# ``SITE_SNAPSHOT_FIELDS`` plus the source's raw ``source_gated`` stamp.
#
# ``snapshot`` carries the site's source and rippleSpec, so it never leaves the
# service: responses and events carry the metadata only.
#
# ``visibility``: "private" (owner only), "workspace" (every member of
# ``workspace``) or "public" (every user in every workspace). ``reports`` holds at
# most the service's ``MAX_REPORTS`` ``{user, reason, at}`` entries, one per
# reporting user;
# ``hidden`` flips on at the report threshold and takes a public template out of
# everyone's reach but the owner's. Who-may-see lives in the service, not here.
#
# ``preview_image_url`` is ``None`` or a public https URL on the Sites public
# asset rail under ``sites-assets/{workspace}/template-{id}/`` (a copy of the
# source site's screenshot), never an auth-gated ``/api/v1/uploads/...`` URL:
# public templates are shown to every workspace.
#
# ``kind`` and ``audiences`` are source-owned Discover fields the owner sets;
# ``live_url`` is the source site's deployed URL, stamped by the service on save
# and update (``None`` when the site is not deployed). Defaults keep old rows valid.
#
# Only ``ee.cloud.site_templates.service`` and ``service_admin`` import this doc
# (import-linter "SiteTemplates" contract).
#
# Updated 2026-10-01 (feat/discover-index): added ``kind``, ``audiences`` and
# ``live_url`` for the Discover index.

from __future__ import annotations

from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteTemplate(TimestampedDocument):
    """One saved site template, scoped to a workspace and owned by one user."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    visibility: Literal["private", "workspace", "public"] = "private"
    hidden: bool = False
    reports: list[dict[str, Any]] = Field(default_factory=list)
    version: int = 1
    source_pocket_id: str
    engine: str | None = None
    pattern: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)
    preview_image_url: str | None = None
    kind: Literal["site", "tool", "game"] = "site"
    audiences: list[Literal["shop", "design", "everyone", "fun"]] = Field(default_factory=list)
    live_url: str | None = None

    class Settings(TimestampedDocument.Settings):
        name = "site_templates"
