# Discover — public, cross-tenant reads plus source-sync and moderation writes.
#
# Created 2026-10-01 (feat/discover-index). Listings are public by design, so no
# read here filters by workspace; that is why they live in ``service_admin`` and
# not ``service``. Every function carries ``# admin-cross-tenant: <reason>``.
#
# Invariants a reader must not break:
#   * The public wire is ``_public`` -> ``PublicListingResponse`` (an allow-list).
#     ``workspace``, ``owner``, ``reports``, ``hidden`` and ``source_id`` stop here.
#   * A hidden listing is NotFound to every public read and to use / report.
#   * A source sync (``upsert_from_source``) ``$set``s only the source-owned
#     fields; ``featured``, ``hidden``, ``reports`` and ``remix_count`` are
#     ``$setOnInsert``, so a template re-save never unhides a listing that
#     Discover reports hid, and never resets its counters.
#   * Site templates are read through ``site_templates.service_admin`` only.
#
# Updated 2026-10-01 (feat/discover-index): unhiding a listing clears its
# reports, so one new report can't instantly re-hide it. Hiding keeps them.
#
# Updated 2026-10-02 (feat/discover-index, hardening): a hide / unhide here
# reaches the source item (``sources.hide_at_source``), so the owner can't undo
# a Discover hide by toggling the template private -> public. A public but
# hidden template keeps a HIDDEN listing (``upsert_from_source(hide=True)``)
# instead of losing it, so staff can unhide by listing id; reindex does the same.
# Unhiding moves the reporters into ``dismissed_reporters`` (their later reports
# on that listing are ignored), so the same accounts can't re-hide it at once.
# ``reindex`` refreshes each template's ``live_url`` from its source site first
# (sites emit no rename / unpublish / delete events), so a stale URL heals on
# the next reindex. ``set_featured`` / ``set_hidden`` write audit rows (actor
# "staff") in the listing owner's workspace.

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    DiscoverListingModerated,
    DiscoverListingRemoved,
    DiscoverListingUpserted,
)
from pocketpaw_ee.cloud.discover.domain import DiscoverListingView
from pocketpaw_ee.cloud.discover.dto import (
    ListPublicListingsRequest,
    PublicListingPage,
    PublicListingResponse,
    UpsertListingRequest,
)
from pocketpaw_ee.cloud.discover.sources import get_source, hide_at_source
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.site_templates import service_admin as site_templates_admin

SITE_TEMPLATE = "site_template"

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _view(doc: DiscoverListing) -> DiscoverListingView:
    return DiscoverListingView(
        workspace_id=doc.workspace,
        owner=doc.owner,
        id=str(doc.id),
        source=doc.source,
        source_id=doc.source_id,
        kind=doc.kind,
        title=doc.title,
        description=doc.description,
        audiences=tuple(doc.audiences),
        preview_image_url=doc.preview_image_url,
        live_url=doc.live_url,
        featured=doc.featured,
        hidden=doc.hidden,
        remix_count=doc.remix_count,
        created_at=doc.createdAt,
    )


def _public(doc: DiscoverListing) -> dict:
    """The allow-listed public wire dict: never workspace, owner, reports,
    hidden or source_id."""
    view = asdict(_view(doc))
    allowed = PublicListingResponse.model_fields.keys()
    return PublicListingResponse.model_validate({key: view[key] for key in allowed}).model_dump(
        mode="json"
    )


def _oid(listing_id: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(listing_id)
    except (InvalidId, TypeError, ValueError):
        raise NotFound("discover_listing", listing_id) from None


async def _any_doc(listing_id: str) -> DiscoverListing:
    doc = await DiscoverListing.get(_oid(listing_id))
    if doc is None:
        raise NotFound("discover_listing", listing_id)
    return doc


async def _audit(workspace_id: str, user_id: str, action: str, target_id: str, **meta: str) -> None:
    from pocketpaw_ee.cloud.audit import service as audit_service

    await audit_service.record(
        workspace_id=workspace_id,
        actor_id=user_id,
        action=action,
        target_type="discover_listing",
        target_id=target_id,
        metadata=meta,
    )


def _ref(doc: DiscoverListing) -> dict[str, Any]:
    return {"listing_id": str(doc.id), "source": doc.source, "source_id": doc.source_id}


def _site_template_fields(row: dict[str, Any]) -> dict[str, Any]:
    """A site template's Discover row as listing fields (``name`` -> ``title``)."""
    return {
        "workspace": row["workspace"],
        "owner": row["owner"],
        "kind": row["kind"],
        "title": row["name"],
        "description": row["description"],
        "audiences": row["audiences"],
        "preview_image_url": row["preview_image_url"],
        "live_url": row["live_url"],
    }


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------


async def public_doc(listing_id: str) -> DiscoverListing:
    """The listing doc, or NotFound when it does not exist or is hidden. Used by
    ``service.use_listing`` / ``report_listing``."""
    # admin-cross-tenant: listings are public by design; any signed-in user in
    # any workspace may use or report an unhidden one.
    doc = await DiscoverListing.get(_oid(listing_id))
    if doc is None or doc.hidden:
        raise NotFound("discover_listing", listing_id)
    return doc


async def list_public(body: ListPublicListingsRequest | dict | None = None) -> dict:
    """A page of unhidden listings, newest first, filtered by ``source``,
    ``kind``, ``audience``, ``q`` (title or description, case-insensitive) and
    ``featured``."""
    # admin-cross-tenant: the public index spans every workspace by design.
    body = ListPublicListingsRequest.model_validate(body or {})
    query: dict[str, Any] = {"hidden": {"$ne": True}}
    if body.source:
        query["source"] = body.source
    if body.kind:
        query["kind"] = body.kind
    if body.audience:
        query["audiences"] = body.audience
    if body.featured is not None:
        query["featured"] = body.featured
    if body.q:
        pattern = {"$regex": re.escape(body.q), "$options": "i"}
        query["$or"] = [{"title": pattern}, {"description": pattern}]
    if body.cursor:
        try:
            query["_id"] = {"$lt": PydanticObjectId(body.cursor)}
        except (InvalidId, TypeError, ValueError):
            raise ValidationError("discover.bad_cursor", "Invalid cursor") from None

    rows = await DiscoverListing.find(query).sort([("_id", -1)]).limit(body.limit + 1).to_list()
    next_cursor = str(rows[body.limit - 1].id) if len(rows) > body.limit else None
    items = [_public(row) for row in rows[: body.limit]]
    return PublicListingPage(items=items, next_cursor=next_cursor).model_dump(mode="json")


async def get_public(listing_id: str) -> dict:
    """One unhidden listing's public card, else NotFound."""
    # admin-cross-tenant: a public listing is readable by anyone.
    return _public(await public_doc(listing_id))


# ---------------------------------------------------------------------------
# Source sync
# ---------------------------------------------------------------------------


async def upsert_from_source(
    source: str, source_id: str, fields: UpsertListingRequest | dict, *, hide: bool = False
) -> str:
    """Create or refresh the listing for ``(source, source_id)`` with the
    source-owned ``fields``; returns its id. Discover-owned state (featured,
    hidden, reports, remix_count) is set on insert only, except that ``hide``
    (the source item itself is hidden) forces ``hidden`` on. A sync never
    unhides."""
    # admin-cross-tenant: a source sync writes the one listing keyed by
    # (source, source_id), whatever workspace owns the source item.
    body = UpsertListingRequest.model_validate(fields)
    if body.kind not in get_source(source).kinds:
        raise ValidationError("discover.bad_kind", f"{source} listings cannot be {body.kind!r}")
    now = datetime.now(UTC)
    key = {"source": source, "source_id": source_id}
    set_fields: dict[str, Any] = {**body.model_dump(), "updatedAt": now}
    on_insert: dict[str, Any] = {
        **key,
        "featured": False,
        "hidden": False,
        "reports": [],
        "dismissed_reporters": [],
        "remix_count": 0,
        "createdAt": now,
    }
    if hide:
        # Mongo refuses one path in both $set and $setOnInsert.
        del on_insert["hidden"]
        set_fields["hidden"] = True
    await DiscoverListing.get_pymongo_collection().update_one(
        key, {"$set": set_fields, "$setOnInsert": on_insert}, upsert=True
    )
    doc = await DiscoverListing.find_one(key)
    await emit(DiscoverListingUpserted(data=_ref(doc)))
    return str(doc.id)


async def remove_from_source(source: str, source_id: str) -> bool:
    """Delete the listing for ``(source, source_id)``; ``False`` when none."""
    # admin-cross-tenant: the source item is gone or no longer public, whatever
    # workspace owned it.
    doc = await DiscoverListing.find_one({"source": source, "source_id": source_id})
    if doc is None:
        # no-event: nothing was listed, nothing changed.
        return False
    await doc.delete()
    await emit(DiscoverListingRemoved(data=_ref(doc)))
    return True


async def sync_site_template(template_id: str) -> None:
    """List the template when it is public (a hidden one as a hidden listing,
    so staff can unhide it); otherwise (private, workspace, deleted) remove its
    listing."""
    # admin-cross-tenant: reacts to template events from every workspace.
    row = await site_templates_admin.get_for_discover(template_id)
    if row is not None and row["public"]:
        await upsert_from_source(
            SITE_TEMPLATE, template_id, _site_template_fields(row), hide=row["hidden"]
        )
    else:
        await remove_from_source(SITE_TEMPLATE, template_id)


async def reindex(source: str) -> dict:
    """Idempotent backfill: upsert every public item of ``source`` (a hidden
    one as a hidden listing, ``live_url`` re-read from the source site) and
    remove listings whose item is gone or no longer public. Only
    ``site_template`` is supported."""
    # admin-cross-tenant: rebuilds the public index across every workspace.
    if source != SITE_TEMPLATE:
        raise ValidationError("discover.reindex_unsupported", f"Cannot reindex {source!r}")
    rows = await site_templates_admin.iter_public_for_discover()
    keep = {row["id"] for row in rows}
    for row in rows:
        # ponytail: one site lookup per public template; batch by pocket id if
        # public templates reach the thousands.
        row["live_url"] = await site_templates_admin.refresh_live_url(row["id"])
        await upsert_from_source(
            SITE_TEMPLATE, row["id"], _site_template_fields(row), hide=row["hidden"]
        )
    stale = await DiscoverListing.find(
        {"source": SITE_TEMPLATE, "source_id": {"$nin": sorted(keep)}}
    ).to_list()
    for doc in stale:
        await remove_from_source(SITE_TEMPLATE, doc.source_id)
    return {"source": source, "upserted": len(keep), "removed": len(stale)}


# ---------------------------------------------------------------------------
# Moderation (platform routes)
# ---------------------------------------------------------------------------


async def _moderate(doc: DiscoverListing, fields: dict[str, Any]) -> dict:
    listing_id = str(doc.id)
    await doc.set({**fields, "updatedAt": datetime.now(UTC)})
    await emit(
        DiscoverListingModerated(data={**_ref(doc), "featured": doc.featured, "hidden": doc.hidden})
    )
    return {"id": listing_id, "featured": doc.featured, "hidden": doc.hidden}


async def set_featured(listing_id: str, featured: bool) -> dict:
    """Feature or unfeature a listing (hidden ones included)."""
    # admin-cross-tenant: platform moderation acts on any workspace's listing.
    doc = await _any_doc(listing_id)
    result = await _moderate(doc, {"featured": featured})
    action = "discover.listing_featured" if featured else "discover.listing_unfeatured"
    await _audit(doc.workspace, "staff", action, listing_id, featured=str(featured))
    return result


async def set_hidden(listing_id: str, hidden: bool) -> dict:
    """Hide or unhide a listing, and its source item (``hide_at_source``), so
    the owner can't re-list a hidden item by re-publishing it. Unhiding clears
    the reports so the next single report can't re-hide it, and moves their
    authors to ``dismissed_reporters`` so they can't re-hide it either (see
    ``service.report_listing``); hiding keeps them. The listing is written first,
    so the source's own re-sync event finds it already in the new state."""
    # admin-cross-tenant: platform moderation acts on any workspace's listing.
    doc = await _any_doc(listing_id)
    if hidden:
        fields: dict[str, Any] = {"hidden": True}
    else:
        dismissed = set(doc.dismissed_reporters) | {r["user"] for r in doc.reports}
        fields = {"hidden": False, "reports": [], "dismissed_reporters": sorted(dismissed)}
    result = await _moderate(doc, fields)
    await hide_at_source(doc.source, doc.source_id, hidden)
    action = "discover.listing_hidden" if hidden else "discover.listing_unhidden"
    await _audit(doc.workspace, "staff", action, listing_id, hidden=str(hidden))
    return result


__all__ = [
    "SITE_TEMPLATE",
    "get_public",
    "list_public",
    "public_doc",
    "reindex",
    "remove_from_source",
    "set_featured",
    "set_hidden",
    "sync_site_template",
    "upsert_from_source",
]
