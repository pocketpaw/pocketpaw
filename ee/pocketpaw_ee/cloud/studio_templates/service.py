# Studio templates — service (the sole owner of StudioTemplate writes in-tenant).
#
# Created 2026-10-02 (feat/studio-templates). Publishing turns one Studio
# generation into a FROZEN SNAPSHOT: the chosen asset becomes ``cover`` and the
# generation's ``{kind, model, prompt, params}`` becomes ``recipe``. Later edits
# or deletes of the generation never reach the template, and deleting the
# template leaves the generation alone. The generation is read through
# ``studio.service.get_generation`` (workspace filter); this module never imports
# the StudioGeneration doc (import-linter "StudioTemplates" contract).
#
# Invariants a reader must not break:
#   * Input images never leave the workspace: ``_strip_input_images`` drops every
#     param naming input images / references / uploads, and ``uses_input_images``
#     records only THAT the run had some (``inputImageCount > 0``).
#   * Only a ``succeeded`` generation publishes (ConflictError
#     ``studio_templates.not_ready``); another workspace's generation is NotFound.
#   * PATCH and DELETE are owner-only by query (``_owned``), like site templates;
#     anyone else gets NotFound.
#   * Every write emits ``studio_template.*`` with ``id`` in the payload; the
#     Discover listener re-reads the template by that id (this module never
#     imports discover).

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from bson.errors import InvalidId

from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    StudioTemplateDeleted,
    StudioTemplateSaved,
    StudioTemplateUpdated,
)
from pocketpaw_ee.cloud.models.studio_template import StudioTemplate
from pocketpaw_ee.cloud.studio import service as studio_service
from pocketpaw_ee.cloud.studio_templates.domain import StudioTemplateMeta
from pocketpaw_ee.cloud.studio_templates.dto import (
    ListStudioTemplatesRequest,
    PatchStudioTemplateRequest,
    PublishStudioTemplateRequest,
    StudioTemplateListResponse,
    StudioTemplateResponse,
)

#: A generation's kind -> the template's Discover kind.
_KINDS = {"image": "image", "video": "video", "audio": "music"}
#: Param-name fragments that mark an input image / reference / upload.
_INPUT_MARKERS = ("input", "upload", "reference")

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _strip_input_images(params: dict[str, Any]) -> dict[str, Any]:
    """``params`` minus every key that names input images, refs or uploads."""

    def _is_input(key: str) -> bool:
        low = key.lower()
        return low.startswith("ref") or any(m in low for m in _INPUT_MARKERS)

    return {k: v for k, v in params.items() if not _is_input(k)}


def _meta(doc: StudioTemplate) -> dict:
    """The template's wire dict (owner view; reports never leave the doc)."""
    meta = StudioTemplateMeta(
        workspace_id=doc.workspace,
        owner=doc.owner,
        id=str(doc.id),
        template_type=doc.template_type,
        source_generation_id=doc.source_generation_id,
        kind=doc.kind,
        title=doc.title,
        description=doc.description,
        audiences=tuple(doc.audiences),
        visibility=doc.visibility,
        cover=doc.cover,
        recipe=doc.recipe,
        uses_input_images=doc.uses_input_images,
        hidden=doc.hidden,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )
    fields = asdict(meta)
    del fields["workspace_id"]
    return StudioTemplateResponse.model_validate(fields).model_dump(mode="json")


def _event_data(doc: StudioTemplate) -> dict:
    """Event payload for its one recipient, the owner."""
    return {**_meta(doc), "workspace_id": doc.workspace, "user_id": doc.owner}


def _oid(template_id: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(template_id)
    except (InvalidId, TypeError, ValueError):
        raise NotFound("studio_template", template_id) from None


async def _owned(workspace_id: str, user_id: str, template_id: str) -> StudioTemplate:
    doc = await StudioTemplate.find_one(
        StudioTemplate.id == _oid(template_id),
        StudioTemplate.workspace == workspace_id,
        StudioTemplate.owner == user_id,
    )
    if doc is None:
        raise NotFound("studio_template", template_id)
    return doc


async def _audit(workspace_id: str, user_id: str, action: str, target_id: str, **meta: str) -> None:
    from pocketpaw_ee.cloud.audit import service as audit_service

    await audit_service.record(
        workspace_id=workspace_id,
        actor_id=user_id,
        action=action,
        target_type="studio_template",
        target_id=target_id,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def publish_template(
    workspace_id: str, user_id: str, body: PublishStudioTemplateRequest | dict
) -> dict:
    """Publish one asset of a generation in the caller's workspace as a template
    the caller owns. NotFound for an unknown generation (or another
    workspace's) or asset id; ConflictError ``studio_templates.not_ready`` unless
    the generation succeeded."""
    body = PublishStudioTemplateRequest.model_validate(body)
    gen = await studio_service.get_generation(workspace_id, body.generation_id)
    if gen is None:
        raise NotFound("studio_generation", body.generation_id)
    if gen.status != "succeeded":
        raise ConflictError(
            "studio_templates.not_ready", "Only a finished generation can be published"
        )
    kind = _KINDS.get(gen.kind)
    if kind is None:
        raise ValidationError("studio_templates.bad_kind", f"Cannot publish a {gen.kind!r}")
    if body.asset_id:
        asset = next((a for a in gen.assets if a.id == body.asset_id), None)
    else:
        asset = gen.assets[0] if gen.assets else None
    if asset is None:
        raise NotFound("studio_asset", body.asset_id or body.generation_id)

    params = gen.params.model_dump(mode="json", exclude_none=True)
    doc = StudioTemplate(
        workspace=workspace_id,
        owner=user_id,
        source_generation_id=gen.id,
        kind=kind,
        title=body.title,
        description=body.description,
        audiences=body.audiences,
        visibility=body.visibility,
        cover={
            "url": asset.url,
            "mime": asset.mime,
            "width": asset.width,
            "height": asset.height,
            "poster_url": asset.posterUrl,
        },
        recipe={
            "kind": gen.kind,
            "model": gen.model,
            "prompt": gen.prompt,
            "params": _strip_input_images(params),
        },
        uses_input_images=(gen.params.inputImageCount or 0) > 0,
    )
    await doc.insert()
    await emit(StudioTemplateSaved(data=_event_data(doc)))
    await _audit(
        workspace_id, user_id, "studio_template.saved", str(doc.id), generation_id=gen.id
    )
    return _meta(doc)


async def list_templates(
    workspace_id: str, user_id: str, body: ListStudioTemplatesRequest | dict | None = None
) -> dict:
    """A page of the caller's own templates in this workspace, newest first."""
    body = ListStudioTemplatesRequest.model_validate(body or {})
    filters: list[Any] = [StudioTemplate.workspace == workspace_id, StudioTemplate.owner == user_id]
    if body.cursor:
        try:
            filters.append({"_id": {"$lt": PydanticObjectId(body.cursor)}})
        except (InvalidId, TypeError, ValueError):
            raise ValidationError("studio_templates.bad_cursor", "Invalid cursor") from None
    rows = await StudioTemplate.find(*filters).sort([("_id", -1)]).limit(body.limit + 1).to_list()
    next_cursor = str(rows[body.limit - 1].id) if len(rows) > body.limit else None
    templates = [_meta(row) for row in rows[: body.limit]]
    return StudioTemplateListResponse(templates=templates, next_cursor=next_cursor).model_dump(
        mode="json"
    )


async def update_template(
    workspace_id: str, user_id: str, template_id: str, body: PatchStudioTemplateRequest | dict
) -> dict:
    """Change a template's title, description, audiences or visibility (owner
    only; anyone else gets NotFound). The cover and recipe never change."""
    body = PatchStudioTemplateRequest.model_validate(body)
    doc = await _owned(workspace_id, user_id, template_id)
    changes = body.model_dump(exclude_none=True)
    if changes:
        changes["updatedAt"] = datetime.now(UTC)
        # ``$set`` of the changed fields only: a whole-doc save could race a
        # Discover hide.
        await doc.set(changes)
        await emit(StudioTemplateUpdated(data=_event_data(doc)))
        await _audit(
            workspace_id, user_id, "studio_template.updated", template_id, visibility=doc.visibility
        )
    return _meta(doc)


async def delete_template(workspace_id: str, user_id: str, template_id: str) -> dict:
    """Delete a template the caller owns (anyone else gets NotFound). The source
    generation is untouched."""
    doc = await _owned(workspace_id, user_id, template_id)
    data = _event_data(doc)
    await doc.delete()
    await emit(StudioTemplateDeleted(data=data))
    await _audit(workspace_id, user_id, "studio_template.deleted", template_id)
    return {"id": template_id, "deleted": True}


__all__ = [
    "delete_template",
    "list_templates",
    "publish_template",
    "update_template",
]
