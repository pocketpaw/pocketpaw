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
#
# Updated 2026-10-02 (feat/discover-index, hardening): the auto-hide at
# ``HIDE_THRESHOLD`` also hides the source item (``sources.hide_at_source``), so
# toggling the template private -> public can't bring the listing back. A
# report from a ``dismissed_reporters`` user (staff unhid over their reports)
# is a no-op. ``use_listing`` writes a ``discover.listing_used`` audit row in the
# caller's workspace.
#
# Updated 2026-10-02 (feat/discover-index, review): no direct listing reads or
# writes here; the remix ``$inc``, report ``$push``, report count and auto-hide
# go through named ``service_admin`` functions (each marked cross-tenant), and
# audit rows through the public ``service_admin.record_audit``. The owner using
# their own listing is allowed and audited but doesn't count as a remix.

from __future__ import annotations

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    DiscoverListingReported,
    DiscoverListingUsed,
)
from pocketpaw_ee.cloud.discover import service_admin
from pocketpaw_ee.cloud.discover.dto import ReportListingRequest, UseListingResponse
from pocketpaw_ee.cloud.discover.sources import get_source, hide_at_source

#: Distinct reporters that hide a listing.
HIDE_THRESHOLD = 3
#: Most reports one listing stores; later reports are accepted and dropped.
MAX_REPORTS = 20


async def use_listing(
    workspace_id: str, user_id: str, listing_id: str, name: str | None = None
) -> dict:
    """Make the caller their own copy of a listing's item, in the caller's
    workspace, through the listing's source. NotFound for a missing or hidden
    listing (or an item the source no longer lets the caller see)."""
    doc = await service_admin.public_doc(listing_id)
    result = await get_source(doc.source).use(workspace_id, user_id, doc.source_id, name)
    if user_id != doc.owner:  # the owner's own use isn't a remix
        await service_admin.increment_remix(listing_id)
    await service_admin.record_audit(
        workspace_id, user_id, "discover.listing_used", listing_id, source=doc.source
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

    if not await service_admin.push_report(
        listing_id, user_id, body.reason, max_reports=MAX_REPORTS
    ):
        # no-event: a repeat report, a dismissed reporter's, or one past
        # MAX_REPORTS changes nothing.
        return {"id": listing_id, "reported": True}

    await service_admin.record_audit(workspace_id, user_id, "discover.listing_reported", listing_id)
    hidden = False
    reports = await service_admin.count_reports(listing_id)
    if reports >= HIDE_THRESHOLD:
        hidden = await service_admin.hide_listing(listing_id)
        if hidden:
            await hide_at_source(doc.source, doc.source_id, True)
            # Actor "system": the reporters are other tenants' users, and their
            # ids must not land in the owner's audit log.
            await service_admin.record_audit(
                doc.workspace,
                "system",
                "discover.listing_hidden",
                listing_id,
                reports=str(reports),
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
