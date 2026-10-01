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
#
# Updated 2026-10-02 (feat/discover-index, hardening): a Discover row's
# ``public`` now means ``visibility == "public"`` and ``hidden`` rides alongside,
# so Discover keeps a hidden template as a HIDDEN listing (staff can unhide it)
# instead of deleting it; ``iter_public_for_discover`` includes hidden ones.
# ``set_hidden_from_discover`` lets a Discover hide / unhide reach the template,
# so making it private and public again can't launder a hide.
# ``refresh_live_url`` re-reads the source site's URL (Discover's reindex calls
# it: sites emit no rename / unpublish / delete events to listen to).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import SiteTemplateUpdated
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.site_templates.service import _audit, _event_data, _source_live_url


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
    source_pocket_id: str = ""


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
        "public": doc.visibility == "public",
        "hidden": doc.hidden,
    }


async def get_for_discover(template_id: str) -> dict[str, Any] | None:
    """One template's Discover row by id, from any workspace, or ``None`` when it
    does not exist. ``public`` says whether it belongs in the index, ``hidden``
    whether that listing must stay hidden."""
    # admin-cross-tenant: the Discover sync reacts to template events from every
    # workspace; the caller only publishes rows whose ``public`` is True.
    try:
        oid = PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        return None
    doc = await SiteTemplate.find_one(SiteTemplate.id == oid).project(_Row)
    return _discover_row(doc) if doc is not None else None


async def iter_public_for_discover() -> list[dict[str, Any]]:
    """Every public template's Discover row (hidden ones included, so their
    listings stay hidden rather than vanish), from every workspace."""
    # admin-cross-tenant: the Discover backfill indexes public templates, which
    # are readable by every user already (``list_templates`` scope=public).
    # ponytail: one full read; page by _id if public templates reach the thousands.
    docs = await SiteTemplate.find(SiteTemplate.visibility == "public").project(_Row).to_list()
    return [_discover_row(doc) for doc in docs]


async def refresh_live_url(template_id: str) -> str | None:
    """Re-read the template's source site URL (``None`` once the site is no
    longer deployed), store it when it changed, and return it. A missing
    template returns ``None``."""
    # admin-cross-tenant: Discover's reindex refreshes every public template,
    # whatever workspace owns it.
    try:
        oid = PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        return None
    doc = await SiteTemplate.find_one(SiteTemplate.id == oid).project(_Row)
    if doc is None:
        return None
    url = await _source_live_url(doc.workspace, doc.source_pocket_id)
    if url == doc.live_url:
        # no-event: unchanged.
        return url
    await SiteTemplate.get_pymongo_collection().update_one(
        {"_id": oid}, {"$set": {"live_url": url, "updatedAt": datetime.now(UTC)}}
    )
    fresh = await SiteTemplate.get(oid)
    await emit(SiteTemplateUpdated(data=_event_data(fresh, fresh.owner, fresh.workspace)))
    return url


async def set_hidden_from_discover(template_id: str, hidden: bool) -> None:
    """Mirror a Discover hide / unhide onto the template. A hidden template is out
    of the /sites community tab and ``use_template`` for everyone but the owner,
    and a PATCH back to public keeps ``hidden``. Unhiding also clears the
    template's own reports so one more /sites report can't instantly re-hide it.
    A missing template is a no-op."""
    # admin-cross-tenant: Discover moderation acts on any workspace's template.
    try:
        oid = PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        return
    changes: dict[str, Any] = {"hidden": hidden, "updatedAt": datetime.now(UTC)}
    if not hidden:
        changes["reports"] = []
    res = await SiteTemplate.get_pymongo_collection().update_one(
        {"_id": oid, "hidden": {"$ne": hidden}}, {"$set": changes}
    )
    if not res.modified_count:
        # no-event: missing, or already in that state; nothing changed.
        return
    doc = await SiteTemplate.get(oid)
    # Re-enters Discover's listener, which re-syncs the listing idempotently
    # (it already holds the same ``hidden``); no loop back here.
    await emit(SiteTemplateUpdated(data=_event_data(doc, doc.owner, doc.workspace)))
    await _audit(
        doc.workspace,
        "system",
        "site_template.hidden" if hidden else "site_template.unhidden",
        template_id,
        via="discover",
    )


__all__ = [
    "get_for_discover",
    "iter_public_for_discover",
    "refresh_live_url",
    "set_hidden_from_discover",
]
