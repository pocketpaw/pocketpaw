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
#
# Updated 2026-10-02 (feat/discover-index, review): the listing reads and writes
# behind ``service.use_listing`` / ``report_listing`` live here as named
# functions (``increment_remix``, ``push_report``, ``count_reports``,
# ``hide_listing``), so ``service`` touches no listing collection directly.
# ``_audit`` is public as ``record_audit`` (``service`` calls it too).
# ``upsert_from_source`` is one ``find_one_and_update(upsert=True)``; when a
# concurrent sync wins the insert (``DuplicateKeyError`` on the unique
# (source, source_id) index) it retries once as a plain update.
# Updated 2026-10-02 (feat/discover-moderation): staff reads for the platform
# moderation routes. ``list_all`` / ``get_staff`` include hidden listings and
# return ``StaffListingResponse`` (moderation fields, workspace, owner). The
# cursor / ``q`` paging is shared with ``list_public`` (``_page``).

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

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
    ListStaffListingsRequest,
    PublicListingPage,
    PublicListingResponse,
    StaffListingPage,
    StaffListingResponse,
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


def _staff(doc: DiscoverListing) -> dict:
    """The staff wire dict: the listing plus its moderation state."""
    return StaffListingResponse(
        id=str(doc.id),
        source=doc.source,
        source_id=doc.source_id,
        workspace_id=doc.workspace,
        owner=doc.owner,
        kind=doc.kind,
        title=doc.title,
        description=doc.description,
        live_url=doc.live_url,
        featured=doc.featured,
        hidden=doc.hidden,
        report_count=len(doc.reports),
        dismissed_reporter_count=len(doc.dismissed_reporters),
        remix_count=doc.remix_count,
        created_at=doc.createdAt,
    ).model_dump(mode="json")


async def _page(query: dict[str, Any], body: Any) -> tuple[list[DiscoverListing], str | None]:
    """Newest-first page for ``query`` plus ``body``'s ``q`` / ``cursor`` /
    ``limit``; returns the docs and the next cursor."""
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
    return rows[: body.limit], next_cursor


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


async def record_audit(
    workspace_id: str, user_id: str, action: str, target_id: str, **meta: str
) -> None:
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
    rows, next_cursor = await _page(query, body)
    items = [_public(row) for row in rows]
    return PublicListingPage(items=items, next_cursor=next_cursor).model_dump(mode="json")


async def get_public(listing_id: str) -> dict:
    """One unhidden listing's public card, else NotFound."""
    # admin-cross-tenant: a public listing is readable by anyone.
    return _public(await public_doc(listing_id))


# ---------------------------------------------------------------------------
# Use / report writes (called by ``service``)
# ---------------------------------------------------------------------------


async def increment_remix(listing_id: str) -> None:
    """Count one remix with an atomic ``$inc``."""
    # admin-cross-tenant: a user in any workspace remixes another's listing.
    # no-event: the caller emits DiscoverListingUsed.
    await DiscoverListing.get_pymongo_collection().update_one(
        {"_id": _oid(listing_id)}, {"$inc": {"remix_count": 1}}
    )


async def push_report(listing_id: str, user_id: str, reason: str, *, max_reports: int) -> bool:
    """Store ``user_id``'s report unless they already reported, were dismissed,
    or the listing holds ``max_reports``. ``True`` when it was stored."""
    # admin-cross-tenant: a user in any workspace reports another's listing.
    # no-event: the caller emits DiscoverListingReported.
    report = {"user": user_id, "reason": reason, "at": datetime.now(UTC)}
    pushed = await DiscoverListing.get_pymongo_collection().update_one(
        {
            "_id": _oid(listing_id),
            "reports.user": {"$ne": user_id},
            "dismissed_reporters": {"$ne": user_id},
            f"reports.{max_reports - 1}": {"$exists": False},
        },
        {"$push": {"reports": report}},
    )
    return bool(pushed.modified_count)


async def count_reports(listing_id: str) -> int:
    """How many reports the listing holds (0 when it is gone)."""
    # admin-cross-tenant: the report threshold reads another workspace's listing.
    doc = await DiscoverListing.get(_oid(listing_id))
    return len(doc.reports) if doc is not None else 0


async def hide_listing(listing_id: str) -> bool:
    """Hide the listing; ``True`` only when this call hid it."""
    # admin-cross-tenant: the report threshold hides another workspace's listing.
    # no-event: the caller emits DiscoverListingReported(hidden=True).
    hid = await DiscoverListing.get_pymongo_collection().update_one(
        {"_id": _oid(listing_id), "hidden": {"$ne": True}}, {"$set": {"hidden": True}}
    )
    return bool(hid.modified_count)


# ---------------------------------------------------------------------------
# Staff reads (platform routes)
# ---------------------------------------------------------------------------


async def list_all(body: ListStaffListingsRequest | dict | None = None) -> dict:
    """A page of every listing, hidden ones included, newest first, with its
    moderation state. Filters: ``source``, ``hidden``, ``featured``, ``q``."""
    # admin-cross-tenant: platform moderation browses every workspace's listings.
    body = ListStaffListingsRequest.model_validate(body or {})
    query: dict[str, Any] = {}
    if body.source:
        query["source"] = body.source
    if body.hidden is not None:
        # ``$ne`` so a doc without the field counts as not hidden, as on the
        # public list.
        query["hidden"] = True if body.hidden else {"$ne": True}
    if body.featured is not None:
        query["featured"] = body.featured
    rows, next_cursor = await _page(query, body)
    page = StaffListingPage(items=[_staff(row) for row in rows], next_cursor=next_cursor)
    return page.model_dump(mode="json")


async def get_staff(listing_id: str) -> dict:
    """One listing's staff view, hidden or not; NotFound when missing."""
    # admin-cross-tenant: platform moderation reads any workspace's listing.
    return _staff(await _any_doc(listing_id))


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
    collection = DiscoverListing.get_pymongo_collection()
    try:
        raw = await collection.find_one_and_update(
            key,
            {"$set": set_fields, "$setOnInsert": on_insert},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        # A concurrent sync inserted it between our match and our insert; it
        # exists now, so update it.
        raw = await collection.find_one_and_update(
            key, {"$set": set_fields}, return_document=ReturnDocument.AFTER
        )
    listing_id = str(raw["_id"])
    await emit(DiscoverListingUpserted(data={"listing_id": listing_id, **key}))
    return listing_id


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
    """Idempotent backfill: upsert every public item of ``source`` whose listing
    is missing or differs (a hidden one as a hidden listing, ``live_url``
    re-read from the source site) and remove listings whose item is gone or no
    longer public. Returns created / updated / unchanged / removed counts. Only
    ``site_template`` is supported."""
    # admin-cross-tenant: rebuilds the public index across every workspace.
    if source != SITE_TEMPLATE:
        raise ValidationError("discover.reindex_unsupported", f"Cannot reindex {source!r}")
    rows = await site_templates_admin.iter_public_for_discover()
    keep = {row["id"] for row in rows}
    existing = {
        doc.source_id: doc
        for doc in await DiscoverListing.find({"source": SITE_TEMPLATE}).to_list()
    }
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        # ponytail: one site lookup per public template; batch by pocket id if
        # public templates reach the thousands.
        row["live_url"] = await site_templates_admin.refresh_live_url(row["id"])
        fields = UpsertListingRequest.model_validate(_site_template_fields(row)).model_dump()
        doc = existing.get(row["id"])
        if doc is None:
            counts["created"] += 1
        elif any(getattr(doc, k) != v for k, v in fields.items()) or (
            row["hidden"] and not doc.hidden
        ):
            counts["updated"] += 1
        else:
            # no-event: the listing already matches its source.
            counts["unchanged"] += 1
            continue
        await upsert_from_source(SITE_TEMPLATE, row["id"], fields, hide=row["hidden"])
    stale = [doc for source_id, doc in existing.items() if source_id not in keep]
    for doc in stale:
        await remove_from_source(SITE_TEMPLATE, doc.source_id)
    return {"source": source, **counts, "removed": len(stale)}


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
    await record_audit(doc.workspace, "staff", action, listing_id, featured=str(featured))
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
    await record_audit(doc.workspace, "staff", action, listing_id, hidden=str(hidden))
    return result


__all__ = [
    "SITE_TEMPLATE",
    "count_reports",
    "get_public",
    "hide_listing",
    "increment_remix",
    "get_staff",
    "list_all",
    "list_public",
    "public_doc",
    "push_report",
    "record_audit",
    "reindex",
    "remove_from_source",
    "set_featured",
    "set_hidden",
    "sync_site_template",
    "upsert_from_source",
]
