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
# ``visibility`` is "private" only for now (owner-only); the field exists so a
# workspace-shared visibility can be added without a migration.
#
# Only ``ee.cloud.site_templates.service`` imports this doc (import-linter
# "SiteTemplates" contract).

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
    visibility: Literal["private"] = "private"
    version: int = 1
    source_pocket_id: str
    engine: str | None = None
    pattern: str | None = None
    snapshot: dict[str, Any] = Field(default_factory=dict)

    class Settings(TimestampedDocument.Settings):
        name = "site_templates"
