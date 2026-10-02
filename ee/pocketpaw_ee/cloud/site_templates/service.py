# Site templates — service (the sole owner of SiteTemplate Beanie reads and writes).
#
# A template is a frozen snapshot of a site pocket's authored content
# (``pockets.service.SITE_SNAPSHOT_FIELDS`` plus the source's raw ``source_gated``
# stamp). This module never imports the Pocket model: it reads the source through
# ``pockets.service.read_site_snapshot`` (workspace filter, read access, site
# check) and creates pockets through ``pockets.service.copy_site_snapshot``
# (Sites plan gate, pocket cap, draft Site mint) in the CALLER's workspace.
#
# Invariants a reader must not break:
#   * The snapshot never leaves this module. Responses and events are built by
#     ``_meta`` from ``SiteTemplateResponse``, which has no snapshot field, and
#     ``_meta`` nulls ``owner`` for anyone but the owner.
#   * ``_visible_to`` is the one place that decides who may read a template:
#     the owner (in its workspace), any member for "workspace", anyone for a
#     "public" template that reports have not hidden. Everyone else gets
#     NotFound, never Forbidden, so ids are not an existence oracle. Delete and
#     PATCH are owner-only by query (``_owned``).
#   * Making a template public runs ``_check_publishable``: no reference to a
#     workspace's private files (``assets.find_private_asset_refs``) and no
#     source the owner's workspace may not read under SF-2.
#   * Reports: one per user (enforced by the conditional ``$push``), at most
#     ``MAX_REPORTS`` stored; ``HIDE_THRESHOLD`` of them set ``hidden``.
#   * Caps: a snapshot over ``MAX_SNAPSHOT_BYTES`` of JSON is refused
#     (``site_templates.too_large``), and so is a workspace's template number
#     ``MAX_TEMPLATES_PER_WORKSPACE + 1`` (``site_templates.limit``).
#   * ``preview_image_url`` is ``None`` or a public-rail URL that
#     ``_copy_source_preview`` minted under ``sites-assets/{ws}/template-{id}/``,
#     never the source pocket's prefix (a site delete purges that) and never the
#     source site's private ``/api/v1/uploads/...`` URL. The copy is best-effort
#     on save; delete purges the template prefix, also best-effort.
#   * ``live_url`` is the source site's deployed URL (``_source_live_url``),
#     re-read on save and on every metadata update; ``None`` when not deployed.
#
# Updated 2026-10-01 (feat/discover-index): save / PATCH store ``kind`` and
# ``audiences`` and stamp ``live_url``; responses carry all three. The Discover
# index syncs from the existing events; this module never imports discover.

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    SiteTemplateDeleted,
    SiteTemplateSaved,
    SiteTemplateUpdated,
    SiteTemplateUsed,
)
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.site_templates.assets import find_private_asset_refs
from pocketpaw_ee.cloud.site_templates.domain import SiteTemplateMeta
from pocketpaw_ee.cloud.site_templates.dto import (
    ListSiteTemplatesRequest,
    PatchSiteTemplateRequest,
    ReportSiteTemplateRequest,
    SaveSiteTemplateRequest,
    SiteTemplateListResponse,
    SiteTemplateResponse,
    UseSiteTemplateRequest,
    UseSiteTemplateResponse,
)

#: Largest snapshot, as UTF-8 JSON, a template may hold.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
#: Most templates one workspace may hold, across all owners.
MAX_TEMPLATES_PER_WORKSPACE = 50
#: Distinct reporters that hide a public template.
HIDE_THRESHOLD = 3
#: Most reports one template stores; later reports are accepted and dropped.
MAX_REPORTS = 20

logger = logging.getLogger(__name__)

#: The private URL ``sites/screenshot.py`` stores a site screenshot under.
_UPLOAD_URL = re.compile(r"/api/v1/uploads/([A-Za-z0-9_-]+)")

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class _MetaRow(BaseModel):
    """List projection: every metadata field, never the snapshot or reports."""

    id: PydanticObjectId = Field(alias="_id")
    workspace: str
    owner: str
    name: str
    description: str = ""
    visibility: str = "private"
    version: int = 1
    engine: str | None = None
    pattern: str | None = None
    hidden: bool = False
    preview_image_url: str | None = None
    kind: str = "site"
    audiences: list[str] = Field(default_factory=list)
    live_url: str | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None


def _meta(doc: SiteTemplate | _MetaRow, viewer: str) -> dict:
    """The template's wire dict as ``viewer`` sees it: metadata only, never the
    snapshot; ``owner`` and ``hidden`` only for the owner."""
    meta = SiteTemplateMeta(
        workspace_id=doc.workspace,
        owner=doc.owner,
        id=str(doc.id),
        name=doc.name,
        description=doc.description,
        visibility=doc.visibility,
        version=doc.version,
        engine=doc.engine,
        pattern=doc.pattern,
        hidden=doc.hidden,
        preview_image_url=doc.preview_image_url,
        kind=doc.kind,
        audiences=tuple(doc.audiences),
        live_url=doc.live_url,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )
    mine = meta.owner == viewer
    fields = asdict(meta)
    del fields["workspace_id"]
    fields.update(owner=meta.owner if mine else None, is_mine=mine, hidden=meta.hidden and mine)
    return SiteTemplateResponse.model_validate(fields).model_dump(mode="json")


def _event_data(doc: SiteTemplate, recipient: str, workspace_id: str, **extra: str) -> dict:
    """Event payload for its one recipient (``user_id``, read by the audience)."""
    return {**_meta(doc, recipient), "workspace_id": workspace_id, "user_id": recipient, **extra}


def _visible_to(doc: SiteTemplate | _MetaRow, workspace_id: str, user_id: str) -> bool:
    """May ``user_id``, acting in ``workspace_id``, read this template?"""
    if doc.workspace == workspace_id:
        if doc.owner == user_id or doc.visibility == "workspace":
            return True
    return doc.visibility == "public" and not doc.hidden


def _oid(template_id: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        raise NotFound("site_template", template_id) from None


async def _fetch_visible(workspace_id: str, user_id: str, template_id: str) -> SiteTemplate:
    # global-read: a public template is readable from every workspace;
    # ``_visible_to`` applies the workspace rule to everything else.
    doc = await SiteTemplate.get(_oid(template_id))
    if doc is None or not _visible_to(doc, workspace_id, user_id):
        raise NotFound("site_template", template_id)
    return doc


async def _owned(workspace_id: str, user_id: str, template_id: str) -> SiteTemplate:
    doc = await SiteTemplate.find_one(
        SiteTemplate.id == _oid(template_id),
        SiteTemplate.workspace == workspace_id,
        SiteTemplate.owner == user_id,
    )
    if doc is None:
        raise NotFound("site_template", template_id)
    return doc


def _check_size(snapshot: dict) -> None:
    if len(json.dumps(snapshot, default=str).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValidationError(
            "site_templates.too_large",
            f"This site is too large to save as a template (limit {MAX_SNAPSHOT_BYTES} bytes)",
        )


async def _check_publishable(workspace_id: str, snapshot: dict) -> None:
    """Refuse to make ``snapshot`` public if it points at the workspace's own files
    or carries source the workspace may not read (SF-2)."""
    refs = find_private_asset_refs(snapshot)
    if refs:
        raise ValidationError(
            "site_templates.private_assets",
            f"This site references {len(refs)} private workspace file(s), which other "
            "workspaces cannot load. Replace them with public images before sharing it publicly.",
        )
    if not await pockets_service.snapshot_source_visible(
        workspace_id, bool(snapshot.get("source_gated"))
    ):
        raise Forbidden(
            "site_templates.source_not_shareable",
            "This site's source is not available on your plan, so it can't be shared publicly",
        )


async def _source_live_url(workspace_id: str, pocket_id: str) -> str | None:
    """The source pocket's live site URL, or ``None`` when it has no deployed site."""
    # Function-local import: sites.service reads pockets (cycle).
    from pocketpaw_ee.sites import service as sites_service

    site = await sites_service.canonical_site_for_pocket(workspace_id, pocket_id)
    if site is None or not site.deployed:
        return None
    return site.url or None


def _template_assets_id(doc: SiteTemplate) -> str:
    """The ``pocket_id`` slot of the template's own public-rail prefix."""
    return f"template-{doc.id}"


async def _copy_source_preview(doc: SiteTemplate) -> Any | None:
    """Copy the source site's current screenshot onto the public asset rail under
    the template's own prefix. Returns the stored ``PublicAsset``, or ``None``
    (logged) when there is no screenshot, it cannot be read, no public bucket is
    configured, or the bytes are not an accepted image. Never raises."""
    from pocketpaw_ee.cloud.uploads import service as uploads_service
    from pocketpaw_ee.sites import public_assets
    from pocketpaw_ee.sites import service as sites_service

    try:
        store = public_assets.public_asset_store()
        if store is None:
            logger.info("site_templates: no public asset bucket, template %s has no image", doc.id)
            return None
        url = await sites_service.preview_image_for_pocket(doc.workspace, doc.source_pocket_id)
        match = _UPLOAD_URL.fullmatch(url or "")
        if match is None:
            logger.info("site_templates: source of template %s has no screenshot", doc.id)
            return None
        data = await uploads_service.read_bytes_scoped(
            match.group(1), doc.workspace, max_bytes=public_assets.MAX_IMAGE_BYTES
        )
        if not data:
            logger.info("site_templates: screenshot for template %s is unreadable", doc.id)
            return None
        asset = await store.put(
            data,
            filename="preview.png",
            workspace_id=doc.workspace,
            pocket_id=_template_assets_id(doc),
        )
        if asset.kind != "image":
            # A video is valid on the rail but is not a card image.
            await store.delete(
                workspace_id=doc.workspace, pocket_id=_template_assets_id(doc), key=asset.key
            )
            return None
        return asset
    except Exception:  # noqa: BLE001 — a missing picture never costs a save
        logger.warning(
            "site_templates: could not copy the screenshot for %s", doc.id, exc_info=True
        )
        return None


async def _audit(workspace_id: str, user_id: str, action: str, target_id: str, **meta: str) -> None:
    from pocketpaw_ee.cloud.audit import service as audit_service

    await audit_service.record(
        workspace_id=workspace_id,
        actor_id=user_id,
        action=action,
        target_type="site_template",
        target_id=target_id,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def save_template(
    workspace_id: str, user_id: str, body: SaveSiteTemplateRequest | dict
) -> dict:
    """Save the site pocket ``body.pocket_id`` as a template the caller owns.

    Refused, before any write: no Sites on the plan (Forbidden
    ``plan.feature_denied``); a pocket the caller cannot read, in another
    workspace, or not a site (via ``read_site_snapshot``); a snapshot over
    ``MAX_SNAPSHOT_BYTES`` (``site_templates.too_large``); a workspace already
    holding ``MAX_TEMPLATES_PER_WORKSPACE`` templates (``site_templates.limit``);
    and for ``visibility="public"``, ``_check_publishable``.
    """
    body = SaveSiteTemplateRequest.model_validate(body)
    # Function-local import: sites.service reads pockets (cycle).
    from pocketpaw_ee.sites import service as sites_service

    await sites_service.require_sites_plan(workspace_id)
    source = await pockets_service.read_site_snapshot(workspace_id, user_id, body.pocket_id)
    snapshot = {key: source[key] for key in pockets_service.SITE_SNAPSHOT_FIELDS}
    snapshot["source_gated"] = source["source_gated"]

    _check_size(snapshot)
    count = await SiteTemplate.find(SiteTemplate.workspace == workspace_id).count()
    if count >= MAX_TEMPLATES_PER_WORKSPACE:
        raise ValidationError(
            "site_templates.limit",
            f"This workspace already has {MAX_TEMPLATES_PER_WORKSPACE} templates",
        )
    if body.visibility == "public":
        await _check_publishable(workspace_id, snapshot)

    doc = SiteTemplate(
        workspace=workspace_id,
        owner=user_id,
        name=body.name,
        description=body.description,
        visibility=body.visibility,
        version=1,
        source_pocket_id=body.pocket_id,
        engine=snapshot["engine"],
        pattern=snapshot["pattern"],
        snapshot=snapshot,
        kind=body.kind,
        audiences=body.audiences,
        live_url=await _source_live_url(workspace_id, body.pocket_id),
    )
    await doc.insert()
    asset = await _copy_source_preview(doc)
    if asset is not None:
        await doc.set({"preview_image_url": asset.url})
    await emit(SiteTemplateSaved(data=_event_data(doc, user_id, workspace_id)))
    await _audit(
        workspace_id, user_id, "site_template.saved", str(doc.id), source_pocket_id=body.pocket_id
    )
    return _meta(doc, user_id)


async def list_templates(
    workspace_id: str, user_id: str, body: ListSiteTemplatesRequest | dict | None = None
) -> dict:
    """A page of templates, newest first, for ``body.scope``.

    ``mine``: the caller's own in this workspace, any visibility. ``workspace``:
    workspace-visibility templates in this workspace (the caller's included).
    ``public``: public, non-hidden templates from every workspace.
    """
    body = ListSiteTemplatesRequest.model_validate(body or {})
    if body.scope == "mine":
        filters: list[Any] = [SiteTemplate.workspace == workspace_id, SiteTemplate.owner == user_id]
    elif body.scope == "workspace":
        filters = [SiteTemplate.workspace == workspace_id, SiteTemplate.visibility == "workspace"]
    else:
        # global-read: public templates are readable by every user
        filters = [SiteTemplate.visibility == "public", {"hidden": {"$ne": True}}]
    if body.cursor:
        try:
            filters.append({"_id": {"$lt": PydanticObjectId(body.cursor)}})
        except (InvalidId, TypeError, ValueError):
            raise ValidationError("site_templates.bad_cursor", "Invalid cursor") from None

    rows = (
        await SiteTemplate.find(*filters)
        .sort([("_id", -1)])
        .limit(body.limit + 1)
        .project(_MetaRow)
        .to_list()
    )
    next_cursor = str(rows[body.limit - 1].id) if len(rows) > body.limit else None
    templates = [_meta(row, user_id) for row in rows[: body.limit]]
    return SiteTemplateListResponse(templates=templates, next_cursor=next_cursor).model_dump(
        mode="json"
    )


async def get_template(workspace_id: str, user_id: str, template_id: str) -> dict:
    """One template's metadata. NotFound unless ``_visible_to`` the caller."""
    return _meta(await _fetch_visible(workspace_id, user_id, template_id), user_id)


async def update_template(
    workspace_id: str, user_id: str, template_id: str, body: PatchSiteTemplateRequest | dict
) -> dict:
    """Change a template's name, description or visibility (owner only; anyone
    else gets NotFound). Setting ``visibility="public"`` runs the Sites plan
    gate, the size cap and ``_check_publishable`` first. ``version`` is unchanged:
    the snapshot is."""
    body = PatchSiteTemplateRequest.model_validate(body)
    doc = await _owned(workspace_id, user_id, template_id)
    if body.visibility == "public":
        from pocketpaw_ee.sites import service as sites_service

        await sites_service.require_sites_plan(workspace_id)
        _check_size(doc.snapshot)
        await _check_publishable(workspace_id, doc.snapshot)

    changes = body.model_dump(exclude_none=True)
    if changes:
        changes["live_url"] = await _source_live_url(doc.workspace, doc.source_pocket_id)
        changes["updatedAt"] = datetime.now(UTC)
        # ``$set`` of the changed fields only: a whole-doc save could drop a
        # report pushed concurrently.
        await doc.set(changes)
        await emit(SiteTemplateUpdated(data=_event_data(doc, user_id, workspace_id)))
        await _audit(
            workspace_id, user_id, "site_template.updated", template_id, visibility=doc.visibility
        )
    return _meta(doc, user_id)


async def delete_template(workspace_id: str, user_id: str, template_id: str) -> dict:
    """Delete a template the caller owns (anyone else gets NotFound). Pockets
    made from it are untouched: they hold their own copy of the snapshot."""
    doc = await _owned(workspace_id, user_id, template_id)
    data = _event_data(doc, user_id, workspace_id)
    await doc.delete()
    await emit(SiteTemplateDeleted(data=data))
    await _audit(workspace_id, user_id, "site_template.deleted", template_id)
    try:
        from pocketpaw_ee.sites import public_assets

        store = public_assets.public_asset_store()
        if store is not None:
            await store.purge_prefix(
                public_assets.prefix_for(doc.workspace, _template_assets_id(doc))
            )
    except Exception:  # noqa: BLE001 — the row is gone; an orphan image is a cleanup job
        logger.warning("site_templates: could not purge images of %s", template_id, exc_info=True)
    return {"id": template_id, "deleted": True}


async def refresh_preview(workspace_id: str, user_id: str, template_id: str) -> dict:
    """Re-copy the source site's CURRENT screenshot onto the template (owner only;
    anyone else gets NotFound). With no usable source screenshot (the site is
    gone, never captured, or the copy failed) raises ConflictError
    ``site_templates.no_source_preview`` and keeps the stored image; so does a
    source the owner can no longer read. Otherwise the
    new image replaces it and every other object under the template prefix is
    deleted (keys are content-addressed, so an unchanged screenshot keeps its key
    and nothing is deleted)."""
    doc = await _owned(workspace_id, user_id, template_id)
    # Re-check the owner can still read the source site, through the same rule
    # save used: access granted at save time may since have been withdrawn, and a
    # public template would publish whatever the copy picks up.
    try:
        await pockets_service.read_site_snapshot(doc.workspace, user_id, doc.source_pocket_id)
    except (NotFound, Forbidden, ValidationError):
        asset = None
    else:
        asset = await _copy_source_preview(doc)
    if asset is None:
        raise ConflictError(
            "site_templates.no_source_preview",
            "The site this template came from has no screenshot to copy",
        )
    from pocketpaw_ee.sites import public_assets

    try:
        store = public_assets.public_asset_store()
        assets_id = _template_assets_id(doc)
        for old in await store.list(workspace_id=doc.workspace, pocket_id=assets_id):
            if old.key != asset.key:
                await store.delete(workspace_id=doc.workspace, pocket_id=assets_id, key=old.key)
    except Exception:  # noqa: BLE001 — the new image is stored; an old one is a cleanup job
        logger.warning("site_templates: could not prune images of %s", template_id, exc_info=True)
    await doc.set({"preview_image_url": asset.url, "updatedAt": datetime.now(UTC)})
    await emit(SiteTemplateUpdated(data=_event_data(doc, user_id, workspace_id)))
    await _audit(workspace_id, user_id, "site_template.preview_refreshed", template_id)
    return _meta(doc, user_id)


async def use_template(
    workspace_id: str, user_id: str, template_id: str, body: UseSiteTemplateRequest | dict
) -> dict:
    """Start a new private site pocket, owned by the caller, in the CALLER's
    workspace, from a template the caller can see.

    ``copy_site_snapshot`` applies the caller's Sites plan gate (Forbidden) and
    pocket cap (402) before any write, and mints the draft Site. The new pocket
    records ``template_id`` and ``template_version``. The audit row names the
    template's workspace only when it is the caller's own.
    """
    body = UseSiteTemplateRequest.model_validate(body)
    doc = await _fetch_visible(workspace_id, user_id, template_id)
    wire = await pockets_service.copy_site_snapshot(
        doc.snapshot,
        workspace_id=workspace_id,
        owner=user_id,
        name=body.name or doc.name,
        template_id=str(doc.id),
        template_version=doc.version,
        # A gated template gives a gated pocket; otherwise ``None`` lets
        # copy_site_snapshot apply the create-time stamp (same OR as duplicate).
        source_gated=doc.snapshot.get("source_gated") or None,
        visibility="private",
    )
    pocket_id = wire["_id"]
    await emit(SiteTemplateUsed(data=_event_data(doc, user_id, workspace_id, pocket_id=pocket_id)))
    same_workspace = (
        {"template_workspace_id": doc.workspace} if doc.workspace == workspace_id else {}
    )
    await _audit(
        workspace_id,
        user_id,
        "site_template.used",
        str(doc.id),
        pocket_id=pocket_id,
        **same_workspace,
    )
    return UseSiteTemplateResponse(pocket_id=pocket_id).model_dump(mode="json")


async def report_template(
    workspace_id: str, user_id: str, template_id: str, body: ReportSiteTemplateRequest | dict
) -> dict:
    """Report a public template the caller can see. One report per user (a repeat
    is a no-op); the owner cannot report their own (Forbidden
    ``site_templates.own_template``); anything not public and visible is NotFound.
    ``HIDE_THRESHOLD`` distinct reporters set ``hidden``, which takes the template
    out of the public list and out of get / use for everyone but the owner."""
    body = ReportSiteTemplateRequest.model_validate(body)
    doc = await _fetch_visible(workspace_id, user_id, template_id)
    if doc.visibility != "public":
        raise NotFound("site_template", template_id)
    if doc.owner == user_id:
        raise Forbidden("site_templates.own_template", "You can't report your own template")

    collection = SiteTemplate.get_pymongo_collection()
    report = {"user": user_id, "reason": body.reason, "at": datetime.now(UTC)}
    pushed = await collection.update_one(
        {
            "_id": doc.id,
            "reports.user": {"$ne": user_id},
            f"reports.{MAX_REPORTS - 1}": {"$exists": False},
        },
        {"$push": {"reports": report}},
    )
    if pushed.modified_count:
        # no-event: a report is moderation state; the owner hears only of a hide.
        await _audit(workspace_id, user_id, "site_template.reported", template_id)
        fresh = await SiteTemplate.get(doc.id)
        if fresh is not None and len(fresh.reports) >= HIDE_THRESHOLD:
            hid = await collection.update_one(
                {"_id": doc.id, "hidden": {"$ne": True}}, {"$set": {"hidden": True}}
            )
            if hid.modified_count:
                fresh.hidden = True
                await emit(
                    SiteTemplateUpdated(data=_event_data(fresh, fresh.owner, fresh.workspace))
                )
                # Actor "system": the reporters are other tenants' users, and their
                # ids must not land in the owner's audit log.
                await _audit(
                    fresh.workspace,
                    "system",
                    "site_template.hidden",
                    template_id,
                    reports=str(len(fresh.reports)),
                )
    return {"id": template_id, "reported": True}


__all__ = [
    "HIDE_THRESHOLD",
    "MAX_REPORTS",
    "MAX_SNAPSHOT_BYTES",
    "MAX_TEMPLATES_PER_WORKSPACE",
    "delete_template",
    "get_template",
    "list_templates",
    "refresh_preview",
    "report_template",
    "save_template",
    "update_template",
    "use_template",
]
