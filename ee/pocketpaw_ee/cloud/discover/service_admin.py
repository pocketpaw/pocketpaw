# Discover — public, cross-tenant reads plus source-sync and moderation writes.
#
# Listings are public by design, so no read here filters by workspace; that is
# why they live in ``service_admin`` and not ``service``. Every function carries
# ``# admin-cross-tenant: <reason>``.
#
# Invariants a reader must not break:
#   * The public wire is ``_public`` -> ``PublicListingResponse`` (an allow-list).
#     ``workspace``, ``owner``, ``reports``, ``hidden`` and ``source_id`` stop here.
#   * A hidden listing is NotFound to every public read and to use / report.
#   * A source sync (``upsert_from_source``) ``$set``s only the source-owned
#     fields; ``featured``, ``hidden``, ``reports``, ``dismissed_reporters`` and
#     ``remix_count`` are ``$setOnInsert``, so a re-save never unhides a listing
#     Discover reports hid and never resets its counters. ``hide=True`` (the
#     source item is hidden) forces ``hidden`` on; a sync never unhides.
#   * ``slug`` is written once and never changes: from the title (or the
#     source's proposed ``slug``), else the ``source_id``, folded by
#     ``sites.slug.normalize`` (the one slug dialect in EE) and made unique
#     across every source with ``-2``, ``-3``... A new row gets it through
#     ``$setOnInsert``; a pre-slug row gets it through a backfill guarded on
#     ``slug: None``, so two syncs of the same row that both read "no slug"
#     cannot re-slug it. The upsert retries when a concurrent sync wins the
#     insert or takes the slug first.
#   * Source items are read through their registered ``DiscoverSource``
#     (``get_public`` / ``iter_public``) only; this module knows no source's
#     field names. ``sync_source`` lists a public item (a hidden one as a hidden
#     listing so staff can unhide it) and removes a private or deleted one;
#     ``reindex`` does the same for every item of a source and heals stale rows.
#   * Moderation: hide / unhide also reach the source item (``hide_at_source``)
#     so the owner can't re-list by re-publishing. Unhiding clears the reports
#     and moves their authors to ``dismissed_reporters``; hiding keeps them.
#     Staff writes record audit rows (actor "staff") in the owner's workspace.
#
# ``service.use_listing`` / ``report_listing`` touch no listing collection
# directly: their reads and writes are the named functions here
# (``public_doc``, ``increment_remix``, ``push_report``, ``count_reports``,
# ``hide_listing``, ``record_audit``).

from __future__ import annotations

import asyncio
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
from pocketpaw_ee.sites.slug import normalize as slugify

SITE_TEMPLATE = "site_template"
STUDIO_TEMPLATE = "studio_template"

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _slug_base(source_id: str, *fields: str | None) -> str:
    """The first of ``fields`` that slugifies to something, else the slugified
    ``source_id``, else the raw ``source_id``: a CJK or Devanagari title must
    not collapse every listing onto one constant."""
    for text in (*fields, source_id):
        if text and (slug := slugify(text)):
            return slug
    return source_id


async def _free_slug(base: str) -> str:
    """``base``, or the first of ``base-2``, ``base-3``... that no listing of
    any source holds."""
    # admin-cross-tenant: slugs are unique across every workspace's listings.
    # ponytail: one lookup per taken candidate; a single regex fetch if titles
    # ever collide hundreds deep.
    collection = DiscoverListing.get_pymongo_collection()
    n = 1
    while True:
        slug = base if n == 1 else f"{base}-{n}"
        if await collection.find_one({"slug": slug}, {"_id": 1}) is None:
            return slug
        n += 1


def _slug_or_id(doc: DiscoverListing) -> str:
    # A pre-slug row serves its id, which the public item route accepts too.
    return doc.slug or str(doc.id)


def _view(doc: DiscoverListing) -> DiscoverListingView:
    return DiscoverListingView(
        workspace_id=doc.workspace,
        owner=doc.owner,
        id=str(doc.id),
        slug=_slug_or_id(doc),
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
        media_kind=doc.media_kind,
        media_url=doc.media_url,
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
        slug=_slug_or_id(doc),
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


_ROW_FLAGS = ("id", "public", "hidden")


def _row_fields(row: dict[str, Any]) -> dict[str, Any]:
    """A source row's listing fields: the row minus ``id`` / ``public`` / ``hidden``."""
    return {k: v for k, v in row.items() if k not in _ROW_FLAGS}


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


async def public_doc_by_id_or_slug(id_or_slug: str) -> DiscoverListing:
    """The unhidden listing whose id is ``id_or_slug``, else the one whose slug
    is (slugs are unique across sources). NotFound when neither exists or it is
    hidden."""
    # admin-cross-tenant: a public listing is readable by anyone.
    try:
        doc = await DiscoverListing.get(PydanticObjectId(id_or_slug))
    except (InvalidId, TypeError, ValueError):
        doc = None
    if doc is None:
        doc = await DiscoverListing.find_one({"slug": id_or_slug, "hidden": {"$ne": True}})
    if doc is None or doc.hidden:
        raise NotFound("discover_listing", id_or_slug)
    return doc


async def get_public(id_or_slug: str) -> dict:
    """One unhidden listing's public card, by id or slug, else NotFound."""
    # admin-cross-tenant: a public listing is readable by anyone.
    return _public(await public_doc_by_id_or_slug(id_or_slug))


async def list_public_for_workspaces(
    workspace_ids: list[str], *, per_workspace: int = 12
) -> dict[str, list[dict]]:
    """The newest ``per_workspace`` unhidden listings of each of ``workspace_ids``
    as public cards, grouped by workspace (a workspace with none is absent). The
    public partner profile shows a partner's listed sites through this; the card
    shape is the same allow-list as the index. Capped per workspace so one
    partner with thousands of listings cannot turn a directory page into a
    multi-megabyte anonymous response."""
    # admin-cross-tenant: public cards only; the workspace ids come from the
    # public partner directory, which lists them by the partner's own choice.
    # ponytail: one capped query per workspace (at most a directory page of
    # them, 50); a $group/$slice aggregation if that ever shows in latency.
    if not workspace_ids:
        return {}

    async def newest(workspace_id: str) -> list[DiscoverListing]:
        return (
            await DiscoverListing.find({"hidden": {"$ne": True}, "workspace": workspace_id})
            .sort([("_id", -1)])
            .limit(per_workspace)
            .to_list()
        )

    pages = await asyncio.gather(*(newest(w) for w in dict.fromkeys(workspace_ids)))
    return {rows[0].workspace: [_public(r) for r in rows] for rows in pages if rows}


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
    key = {"source": source, "source_id": source_id}
    collection = DiscoverListing.get_pymongo_collection()
    for attempt in range(3):
        now = datetime.now(UTC)
        set_fields: dict[str, Any] = {**body.model_dump(exclude={"slug"}), "updatedAt": now}
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
        existing = await collection.find_one(key, {"slug": 1})
        slug = None
        if not (existing or {}).get("slug"):
            # Written once, never re-derived, so a rename keeps the listing's
            # URL. Insert-only here; a pre-slug row is backfilled below.
            slug = await _free_slug(_slug_base(source_id, body.slug, body.title))
            on_insert["slug"] = slug
        try:
            raw = await collection.find_one_and_update(
                key,
                {"$set": set_fields, "$setOnInsert": on_insert},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            if slug and not raw.get("slug"):
                # Guarded on ``slug: None`` (missing or null): a sibling sync of
                # this same row that backfilled first wins, and ours is a no-op.
                await collection.update_one({**key, "slug": None}, {"$set": {"slug": slug}})
            break
        except DuplicateKeyError:
            # A concurrent sync won the (source, source_id) insert, or took our
            # slug, between our reads and our write: re-read and go again.
            if attempt == 2:
                raise
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


async def sync_source(name: str, source_id: str) -> None:
    """List the source item when it is public (a hidden one as a hidden
    listing, so staff can unhide it); otherwise (private, deleted) remove its
    listing."""
    # admin-cross-tenant: reacts to source events from every workspace.
    get_public = get_source(name).get_public
    if get_public is None:
        raise ValidationError("discover.sync_unsupported", f"Cannot sync {name!r}")
    row = await get_public(source_id)
    if row is not None and row["public"]:
        await upsert_from_source(name, source_id, _row_fields(row), hide=row["hidden"])
    else:
        await remove_from_source(name, source_id)


async def reindex(source: str) -> dict:
    """Idempotent backfill: upsert every public item of ``source`` whose listing
    is missing, differs or has no slug yet (a hidden one as a hidden listing) and remove listings
    whose item is gone or no longer public. Returns created / updated /
    unchanged / removed counts. A source that is unknown or has no
    ``iter_public`` can't be reindexed."""
    # admin-cross-tenant: rebuilds the public index across every workspace.
    try:
        iter_public = get_source(source).iter_public
    except NotFound:
        iter_public = None
    if iter_public is None:
        raise ValidationError("discover.reindex_unsupported", f"Cannot reindex {source!r}")
    existing = {
        doc.source_id: doc for doc in await DiscoverListing.find({"source": source}).to_list()
    }
    keep: set[str] = set()
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    async for row in iter_public():
        if not row["public"]:
            continue
        keep.add(row["id"])
        fields = UpsertListingRequest.model_validate(_row_fields(row)).model_dump()
        doc = existing.get(row["id"])
        if doc is None:
            counts["created"] += 1
        elif (
            # A proposed ``slug`` only matters to a row without one (see upsert).
            any(getattr(doc, k) != v for k, v in fields.items() if k != "slug")
            or (row["hidden"] and not doc.hidden)
            or doc.slug is None  # a pre-slug row: backfill it
        ):
            counts["updated"] += 1
        else:
            # no-event: the listing already matches its source.
            counts["unchanged"] += 1
            continue
        await upsert_from_source(source, row["id"], fields, hide=row["hidden"])
    stale = [doc for source_id, doc in existing.items() if source_id not in keep]
    for doc in stale:
        await remove_from_source(source, doc.source_id)
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
    "STUDIO_TEMPLATE",
    "count_reports",
    "get_public",
    "hide_listing",
    "increment_remix",
    "get_staff",
    "list_all",
    "list_public",
    "list_public_for_workspaces",
    "public_doc",
    "public_doc_by_id_or_slug",
    "push_report",
    "record_audit",
    "reindex",
    "remove_from_source",
    "set_featured",
    "set_hidden",
    "slugify",
    "sync_source",
    "upsert_from_source",
]
