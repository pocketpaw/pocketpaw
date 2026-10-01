# Site templates — service (the sole owner of SiteTemplate Beanie reads and writes).
#
# A template is a frozen snapshot of a site pocket's authored content
# (``pockets.service.SITE_SNAPSHOT_FIELDS`` plus the source's raw ``source_gated``
# stamp). This module never imports the Pocket model: it reads the source through
# ``pockets.service.read_site_snapshot`` (workspace filter, read access, site
# check) and creates pockets through ``pockets.service.copy_site_snapshot``
# (Sites plan gate, pocket cap, draft Site mint).
#
# Invariants a reader must not break:
#   * The snapshot never leaves this module. Responses and events are built by
#     ``_meta`` from ``SiteTemplateResponse``, which has no snapshot field.
#   * Every read filters on ``workspace``. A template the caller may not see is
#     NotFound, never Forbidden, so ids are not an existence oracle.
#   * Templates are private (owner-only) for now; ``_visible_to`` is the one
#     place that decides who may read one, and ``delete`` is owner-only by query.
#   * Caps: a snapshot over ``MAX_SNAPSHOT_BYTES`` of JSON is refused
#     (``site_templates.too_large``), and so is a workspace's template number
#     ``MAX_TEMPLATES_PER_WORKSPACE + 1`` (``site_templates.limit``).

from __future__ import annotations

import json

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    SiteTemplateDeleted,
    SiteTemplateSaved,
    SiteTemplateUsed,
)
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.site_templates.domain import SiteTemplateMeta
from pocketpaw_ee.cloud.site_templates.dto import (
    SaveSiteTemplateRequest,
    SiteTemplateResponse,
    UseSiteTemplateRequest,
    UseSiteTemplateResponse,
)

#: Largest snapshot, as UTF-8 JSON, a template may hold.
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
#: Most templates one workspace may hold, across all owners.
MAX_TEMPLATES_PER_WORKSPACE = 50

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _meta(doc: SiteTemplate) -> dict:
    """The template's wire dict: metadata only, never the snapshot."""
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
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )
    return SiteTemplateResponse.model_validate(meta, from_attributes=True).model_dump(mode="json")


def _event_data(doc: SiteTemplate, **extra: str) -> dict:
    return {**_meta(doc), "workspace_id": doc.workspace, **extra}


def _visible_to(doc: SiteTemplate, user_id: str) -> bool:
    """May ``user_id`` read this template? Private templates: the owner only."""
    return doc.owner == user_id


def _oid(template_id: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        raise NotFound("site_template", template_id) from None


async def _fetch_visible(workspace_id: str, user_id: str, template_id: str) -> SiteTemplate:
    doc = await SiteTemplate.find_one(
        SiteTemplate.id == _oid(template_id), SiteTemplate.workspace == workspace_id
    )
    if doc is None or not _visible_to(doc, user_id):
        raise NotFound("site_template", template_id)
    return doc


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
    """Save the site pocket ``body.pocket_id`` as a private template the caller owns.

    Refused, before any write: no Sites on the plan (Forbidden
    ``plan.feature_denied``); a pocket the caller cannot read, in another
    workspace, or not a site (via ``read_site_snapshot``); a snapshot over
    ``MAX_SNAPSHOT_BYTES`` (``site_templates.too_large``); a workspace already
    holding ``MAX_TEMPLATES_PER_WORKSPACE`` templates (``site_templates.limit``).
    """
    body = SaveSiteTemplateRequest.model_validate(body)
    # Function-local import: sites.service reads pockets (cycle).
    from pocketpaw_ee.sites import service as sites_service

    await sites_service.require_sites_plan(workspace_id)
    source = await pockets_service.read_site_snapshot(workspace_id, user_id, body.pocket_id)
    snapshot = {key: source[key] for key in pockets_service.SITE_SNAPSHOT_FIELDS}
    snapshot["source_gated"] = source["source_gated"]

    if len(json.dumps(snapshot, default=str).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValidationError(
            "site_templates.too_large",
            f"This site is too large to save as a template (limit {MAX_SNAPSHOT_BYTES} bytes)",
        )
    count = await SiteTemplate.find(SiteTemplate.workspace == workspace_id).count()
    if count >= MAX_TEMPLATES_PER_WORKSPACE:
        raise ValidationError(
            "site_templates.limit",
            f"This workspace already has {MAX_TEMPLATES_PER_WORKSPACE} templates",
        )

    doc = SiteTemplate(
        workspace=workspace_id,
        owner=user_id,
        name=body.name,
        description=body.description,
        visibility="private",
        version=1,
        source_pocket_id=body.pocket_id,
        engine=snapshot["engine"],
        pattern=snapshot["pattern"],
        snapshot=snapshot,
    )
    await doc.insert()
    await emit(SiteTemplateSaved(data=_event_data(doc)))
    await _audit(
        workspace_id, user_id, "site_template.saved", str(doc.id), source_pocket_id=body.pocket_id
    )
    return _meta(doc)


async def list_templates(workspace_id: str, user_id: str) -> list[dict]:
    """The caller's own private templates in this workspace, newest first."""
    cursor = SiteTemplate.find(
        SiteTemplate.workspace == workspace_id,
        SiteTemplate.owner == user_id,
        SiteTemplate.visibility == "private",
    ).sort([("createdAt", -1), ("_id", -1)])
    return [_meta(doc) async for doc in cursor]


async def get_template(workspace_id: str, user_id: str, template_id: str) -> dict:
    """One template's metadata. NotFound unless it is in this workspace and
    visible to the caller."""
    return _meta(await _fetch_visible(workspace_id, user_id, template_id))


async def delete_template(workspace_id: str, user_id: str, template_id: str) -> dict:
    """Delete a template the caller owns (anyone else gets NotFound). Pockets
    made from it are untouched: they hold their own copy of the snapshot."""
    doc = await SiteTemplate.find_one(
        SiteTemplate.id == _oid(template_id),
        SiteTemplate.workspace == workspace_id,
        SiteTemplate.owner == user_id,
    )
    if doc is None:
        raise NotFound("site_template", template_id)
    data = _event_data(doc)
    await doc.delete()
    await emit(SiteTemplateDeleted(data=data))
    await _audit(workspace_id, user_id, "site_template.deleted", template_id)
    return {"id": template_id, "deleted": True}


async def use_template(
    workspace_id: str, user_id: str, template_id: str, body: UseSiteTemplateRequest | dict
) -> dict:
    """Start a new private site pocket, owned by the caller, from a template.

    ``copy_site_snapshot`` applies the Sites plan gate (Forbidden) and the
    pocket cap (402) before any write, and mints the draft Site. The new pocket
    records ``template_id`` and ``template_version``.
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
    await emit(SiteTemplateUsed(data=_event_data(doc, pocket_id=pocket_id)))
    await _audit(workspace_id, user_id, "site_template.used", str(doc.id), pocket_id=pocket_id)
    return UseSiteTemplateResponse(pocket_id=pocket_id).model_dump(mode="json")


__all__ = [
    "MAX_SNAPSHOT_BYTES",
    "MAX_TEMPLATES_PER_WORKSPACE",
    "delete_template",
    "get_template",
    "list_templates",
    "save_template",
    "use_template",
]
