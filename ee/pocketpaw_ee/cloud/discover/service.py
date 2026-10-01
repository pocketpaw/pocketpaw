# Discover — service (signed-in actions on a listing: use and report).
#
# Created 2026-10-01 (feat/discover-index). Both actions run as the caller in
# the caller's workspace. The listing itself is public, so it is loaded through
# ``service_admin.public_doc`` (the visible cross-tenant read; hidden is NotFound)
# and never with a workspace filter here.
#
# Invariants a reader must not break:
#   * ``use_listing`` delegates to the listing's source (``sources.get_source``),
#     which applies its own visibility, plan and cap checks, and bumps
#     ``remix_count`` with one atomic ``$inc`` only after the source succeeded.
#   * Reports: one per user (the conditional ``$push``), at most ``MAX_REPORTS``
#     stored, ``HIDE_THRESHOLD`` distinct reporters set ``hidden``. The owner
#     cannot report their own listing. Mirrors ``site_templates.report_template``.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    DiscoverListingReported,
    DiscoverListingUsed,
)
from pocketpaw_ee.cloud.discover import service_admin
from pocketpaw_ee.cloud.discover.dto import ReportListingRequest, UseListingResponse
from pocketpaw_ee.cloud.discover.sources import get_source
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing

#: Distinct reporters that hide a listing.
HIDE_THRESHOLD = 3
#: Most reports one listing stores; later reports are accepted and dropped.
MAX_REPORTS = 20


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


async def use_listing(
    workspace_id: str, user_id: str, listing_id: str, name: str | None = None
) -> dict:
    """Make the caller their own copy of a listing's item, in the caller's
    workspace, through the listing's source. NotFound for a missing or hidden
    listing (or an item the source no longer lets the caller see)."""
    doc = await service_admin.public_doc(listing_id)
    result = await get_source(doc.source).use(workspace_id, user_id, doc.source_id, name)
    await DiscoverListing.get_pymongo_collection().update_one(
        {"_id": doc.id}, {"$inc": {"remix_count": 1}}
    )
    await emit(
        DiscoverListingUsed(
            data={
                "listing_id": listing_id,
                "source": doc.source,
                "source_id": doc.source_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "result": result,
            }
        )
    )
    return UseListingResponse(source=doc.source, result=result).model_dump(mode="json")


async def report_listing(
    workspace_id: str, user_id: str, listing_id: str, body: ReportListingRequest | dict
) -> dict:
    """Report an unhidden listing. One report per user (a repeat is a no-op);
    the owner cannot report their own (Forbidden ``discover.own_listing``).
    ``HIDE_THRESHOLD`` distinct reporters hide it from every public read."""
    body = ReportListingRequest.model_validate(body)
    doc = await service_admin.public_doc(listing_id)
    if doc.owner == user_id:
        raise Forbidden("discover.own_listing", "You can't report your own listing")

    collection = DiscoverListing.get_pymongo_collection()
    report = {"user": user_id, "reason": body.reason, "at": datetime.now(UTC)}
    pushed = await collection.update_one(
        {
            "_id": doc.id,
            "reports.user": {"$ne": user_id},
            f"reports.{MAX_REPORTS - 1}": {"$exists": False},
        },
        {"$push": {"reports": report}},
    )
    if not pushed.modified_count:
        # no-event: a repeat report (or one past MAX_REPORTS) changes nothing.
        return {"id": listing_id, "reported": True}

    await _audit(workspace_id, user_id, "discover.listing_reported", listing_id)
    hidden = False
    fresh = await DiscoverListing.get(doc.id)
    if fresh is not None and len(fresh.reports) >= HIDE_THRESHOLD:
        hid = await collection.update_one(
            {"_id": doc.id, "hidden": {"$ne": True}}, {"$set": {"hidden": True}}
        )
        hidden = bool(hid.modified_count)
        if hidden:
            # Actor "system": the reporters are other tenants' users, and their
            # ids must not land in the owner's audit log.
            await _audit(
                fresh.workspace,
                "system",
                "discover.listing_hidden",
                listing_id,
                reports=str(len(fresh.reports)),
            )
    await emit(
        DiscoverListingReported(
            data={
                "listing_id": listing_id,
                "source": doc.source,
                "source_id": doc.source_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "hidden": hidden,
            }
        )
    )
    return {"id": listing_id, "reported": True}


__all__ = ["HIDE_THRESHOLD", "MAX_REPORTS", "report_listing", "use_listing"]
