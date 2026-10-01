# Site templates — cross-tenant reads for the Discover index.
#
# Created 2026-10-01 (feat/discover-index): the Discover index (``cloud.discover``)
# mirrors every public, unhidden template as one listing. It reads templates
# through these two functions, never through the SiteTemplate model, and these
# are the only template reads that skip the workspace filter. They return the
# metadata Discover needs (never the snapshot) with the REAL ``workspace`` and
# ``owner``: Discover stores both and keeps them off its public wire.
#
# Every function carries ``# admin-cross-tenant: <reason>`` (adoption-plan rule).

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.site_template import SiteTemplate


class _Row(BaseModel):
    """Projection: the fields Discover needs, never the (up to 2 MB) snapshot."""

    id: PydanticObjectId = Field(alias="_id")
    workspace: str
    owner: str
    name: str
    description: str = ""
    visibility: str = "private"
    hidden: bool = False
    kind: str = "site"
    audiences: list[str] = Field(default_factory=list)
    preview_image_url: str | None = None
    live_url: str | None = None


def _discover_row(doc: _Row) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "workspace": doc.workspace,
        "owner": doc.owner,
        "name": doc.name,
        "description": doc.description,
        "kind": doc.kind,
        "audiences": list(doc.audiences),
        "preview_image_url": doc.preview_image_url,
        "live_url": doc.live_url,
        "public": doc.visibility == "public" and not doc.hidden,
    }


async def get_for_discover(template_id: str) -> dict[str, Any] | None:
    """One template's Discover row by id, from any workspace, or ``None`` when it
    does not exist. ``public`` says whether it belongs in the index."""
    # admin-cross-tenant: the Discover sync reacts to template events from every
    # workspace; the caller only publishes rows whose ``public`` is True.
    try:
        oid = PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        return None
    doc = await SiteTemplate.find_one(SiteTemplate.id == oid).project(_Row)
    return _discover_row(doc) if doc is not None else None


async def iter_public_for_discover() -> list[dict[str, Any]]:
    """Every public, unhidden template's Discover row, from every workspace."""
    # admin-cross-tenant: the Discover backfill indexes public templates, which
    # are readable by every user already (``list_templates`` scope=public).
    # ponytail: one full read; page by _id if public templates reach the thousands.
    docs = (
        await SiteTemplate.find(SiteTemplate.visibility == "public", {"hidden": {"$ne": True}})
        .project(_Row)
        .to_list()
    )
    return [_discover_row(doc) for doc in docs]


__all__ = ["get_for_discover", "iter_public_for_discover"]
