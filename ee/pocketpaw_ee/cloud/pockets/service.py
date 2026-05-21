"""Pockets domain — business logic service.

Sole owner of writes to the ``Pocket`` Beanie document. Module-level
``async def`` API. The doc → domain mapping helpers (formerly in
``repositories.py``) live alongside the public API as private helpers.

Public API (returns wire dicts for legacy router compatibility):
- ``create``, ``list_pockets``, ``get``, ``update``, ``delete``
- ``create_from_ripple_spec`` — agent-generated pockets
- ``add_widget``, ``update_widget``, ``remove_widget``, ``reorder_widgets``
- ``generate_share_link``, ``revoke_share_link``, ``update_share_link``,
  ``access_via_share_link``
- ``add_collaborator``, ``remove_collaborator``
- ``add_team_member``, ``remove_team_member``
- ``add_agent``, ``remove_agent``

Agent-facing granular ``rippleSpec.ui`` mutations (called from the
``pocket_specialist`` subagent via the in-process MCP server):
- ``agent_add_node``, ``agent_replace_node``, ``agent_set_node_prop``,
  ``agent_move_node``, ``agent_remove_node``
- ``agent_set_prop_array_item``, ``agent_append_prop_array_item``,
  ``agent_remove_prop_array_item`` — Tier-2 surgical edits on a single
  item inside a widget prop-array (chart.data, table.rows, …)

Changes: 2026-05-14 — added the Tier-2 prop-array item ops (reworked
onto the pocketpaw_ee layout from PR #1106).
Changes: 2026-05-21 (#1172) — ``agent_view`` self-heals node ids via
``_heal_node_ids`` so pockets persisted before node-id stamping became
addressable by granular edit ops on first agent read.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import secrets
from collections import OrderedDict
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    PocketCreated,
    PocketDeleted,
    PocketUpdated,
)
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.models.pocket import Widget as _WidgetDoc
from pocketpaw_ee.cloud.pockets import prop_arrays, spec_ops, state_ops
from pocketpaw_ee.cloud.pockets.domain import Pocket, Widget, WidgetPosition
from pocketpaw_ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
    pocket_to_wire_dict,
)
from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec
from pocketpaw_ee.cloud.ripple_validator import validate_ripple_spec_logged
from pocketpaw_ee.cloud.shared.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private mapping + access helpers
# ---------------------------------------------------------------------------


def _widget_to_domain(w: _WidgetDoc) -> Widget:
    return Widget(
        id=w.id,
        name=w.name,
        type=w.type,
        icon=w.icon,
        color=w.color,
        span=w.span,
        data_source_type=w.dataSourceType,
        config=tuple(w.config.items()),
        props=tuple(w.props.items()),
        data=w.data,
        assigned_agent=w.assignedAgent,
        position=WidgetPosition(row=w.position.row, col=w.position.col),
    )


def _pocket_to_domain(doc: _PocketDoc) -> Pocket:
    return Pocket(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        description=doc.description,
        type=doc.type,
        icon=doc.icon,
        color=doc.color,
        owner=doc.owner,
        visibility=doc.visibility,
        team=tuple(str(t) for t in doc.team),
        agents=tuple(str(a) for a in doc.agents),
        widgets=tuple(_widget_to_domain(w) for w in doc.widgets),
        ripple_spec=doc.rippleSpec,
        share_link_token=doc.share_link_token,
        share_link_access=doc.share_link_access,
        shared_with=tuple(doc.shared_with),
        tool_specs=tuple(doc.tool_specs),
        project_id=getattr(doc, "project_id", None),
        created_at=getattr(doc, "createdAt", None),
        updated_at=getattr(doc, "updatedAt", None),
    )


async def _fetch_pocket(pocket_id: str) -> _PocketDoc:
    """Fetch a pocket doc by id; raise NotFound if missing."""
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception:
        doc = None
    if doc is None:
        raise NotFound("pocket", pocket_id)
    return doc


def _check_domain_owner(domain_pocket: Pocket, user_id: str) -> None:
    if domain_pocket.owner != user_id:
        from pocketpaw_ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action="pocket.share",
            code="pocket.not_owner",
            resource_id=domain_pocket.id,
        )
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")


def _check_domain_edit_access(domain_pocket: Pocket, user_id: str) -> None:
    if domain_pocket.owner == user_id:
        return
    if user_id in domain_pocket.shared_with:
        return
    if domain_pocket.visibility == "workspace":
        return
    from pocketpaw_ee.guards.audit import log_denial

    log_denial(
        actor=user_id,
        action="pocket.edit",
        code="pocket.access_denied",
        resource_id=domain_pocket.id,
    )
    raise Forbidden("pocket.access_denied", "You do not have edit access to this pocket")


def _build_widget_doc(payload: dict) -> _WidgetDoc:
    return _WidgetDoc(
        name=payload.get("name", "Widget"),
        type=payload.get("type", "custom"),
        icon=payload.get("icon", ""),
        color=payload.get("color", ""),
        span=payload.get("span", "col-span-1"),
        dataSourceType=payload.get("dataSourceType", payload.get("data_source_type", "static")),
        config=payload.get("config", {}),
        props=payload.get("props", {}),
        data=payload.get("data"),
        assignedAgent=payload.get("assignedAgent", payload.get("assigned_agent")),
    )


async def _resolved_wire_dict(doc: _PocketDoc, viewer_user_id: str) -> dict:
    """Build the wire dict with rippleSpec ``$source`` markers resolved
    against ``viewer_user_id``'s workspace context.

    Used by every boundary that hands a spec to a renderer:
    ``service.get`` (REST), ``service.create``/``update`` return values,
    and ``_pocket_event_payload`` (WebSocket broadcast). Falls back to
    the raw wire dict on resolver failure — never raises.

    Resolution requires a viewer because sources like ``workspace.pockets``
    apply per-user visibility filters. For multi-recipient broadcasts,
    pass the doc's owner; this can over-share owner's private pockets to
    other recipients (metadata only). Tracked for v2: per-recipient
    resolution or frontend refetch on event receipt.
    """
    import dataclasses

    pocket = _pocket_to_domain(doc)
    if pocket.ripple_spec:
        from pocketpaw_ee.cloud import ripple_sources  # noqa: F401  — register sources
        from pocketpaw_ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec

        try:
            resolved = await resolve_ripple_spec(
                pocket.ripple_spec,
                ResolveCtx(
                    workspace_id=doc.workspace,
                    user_id=viewer_user_id,
                    pocket_id=str(doc.id),
                ),
            )
            pocket = dataclasses.replace(pocket, ripple_spec=resolved)
        except Exception:
            logger.warning(
                "ripple_resolver: resolve failed for pocket %s; returning raw spec",
                str(doc.id),
                exc_info=True,
            )
    return pocket_to_wire_dict(pocket)


async def _pocket_event_payload(doc: _PocketDoc) -> dict:
    """Build the realtime event payload for a pocket mutation.

    Always includes ``recipient_ids`` (owner + shared_with). For
    workspace-visible pockets, also includes ``workspace_id`` so the
    audience resolver fans out to every workspace member. Mirrors the
    visibility rules used by ``list_pockets`` so a member only ever sees
    a ``pocket.*`` event for a pocket they could read via REST.

    ``shareLinkToken`` and ``sharedWith`` are stripped from the broadcast
    pocket — workspace-visible pockets fan out to every member, and the
    share token is owner-only state. Owners receive the token directly in
    the REST response from ``generate_share_link``.

    rippleSpec ``$source`` markers are resolved using ``doc.owner`` as
    the viewer. See ``_resolved_wire_dict`` for the v2 follow-up note.
    """
    pocket_dict = await _resolved_wire_dict(doc, doc.owner)
    pocket_dict.pop("shareLinkToken", None)
    pocket_dict.pop("sharedWith", None)
    payload: dict = {
        "pocket_id": str(doc.id),
        "pocket": pocket_dict,
        "recipient_ids": [doc.owner, *list(doc.shared_with or [])],
    }
    if doc.visibility == "workspace":
        payload["workspace_id"] = doc.workspace
    return payload


async def _mutate_list_field(pocket_id: str, field: str, value: str, action: str) -> Pocket:
    """Append/remove a string value on shared_with / team / agents.
    Idempotent in both directions. Emits ``PocketUpdated`` when the doc
    actually changes (no-op mutations stay silent)."""
    doc = await _fetch_pocket(pocket_id)
    current: list[str] = list(getattr(doc, field))
    changed = False
    if action == "add":
        if value not in current:
            current.append(value)
            setattr(doc, field, current)
            await doc.save()
            changed = True
    else:
        if value in current:
            current.remove(value)
            setattr(doc, field, current)
            await doc.save()
            changed = True
    if changed:
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _pocket_to_domain(doc)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create(workspace_id: str, user_id: str, body: CreatePocketRequest) -> dict:
    """Create a pocket with optional agents, widgets, and rippleSpec.

    ``project_id`` is optional — when supplied, the service validates it
    points at a real project in the same workspace and rejects with
    ``project.not_found`` otherwise. Pockets without a ``project_id``
    surface as "unassigned" in the Mission Control project picker, which
    is exactly the pre-projects-rollout shape.
    """
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
    if normalized_spec:
        validate_ripple_spec_logged(normalized_spec, workspace_id=workspace_id)
    widget_docs = [_build_widget_doc(w) for w in (body.widgets or [])]

    if body.project_id:
        await _ensure_project_in_workspace(workspace_id, body.project_id)

    doc = _PocketDoc(
        workspace=workspace_id,
        project_id=body.project_id,
        name=body.name,
        description=body.description,
        type=body.type,
        icon=body.icon,
        color=body.color,
        owner=user_id,
        visibility=body.visibility,
        agents=list(body.agents or []),
        widgets=widget_docs,
        rippleSpec=normalized_spec,
    )
    await doc.insert()
    pocket = _pocket_to_domain(doc)

    if body.session_id:
        await sessions_service.link_pocket(workspace_id, body.session_id, pocket.id)

    await emit(PocketCreated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def list_pockets(
    workspace_id: str,
    user_id: str,
    *,
    project_id: str | None = None,
) -> list[dict]:
    """List pockets visible to the user (owned, shared_with, or workspace-visible).

    Each returned pocket has its rippleSpec ``$source`` markers resolved
    against ``user_id``'s context — the desktop client renders the canvas
    directly from this list response, so unresolved markers would surface
    as empty widgets. Resolution per pocket is independent; sources that
    fail fall back to raw markers individually (see ``_resolved_wire_dict``).

    ``project_id`` filter: when provided, narrows the result to pockets
    whose ``project_id`` matches. Pass an empty string to filter for
    "no project assigned" — that's the Mission Control "Unassigned"
    bucket. Kept as a kwarg so existing callers don't change.
    """
    query: dict = {
        "workspace": workspace_id,
        "$or": [
            {"owner": user_id},
            {"shared_with": user_id},
            {"visibility": "workspace"},
        ],
    }
    if project_id is not None:
        # Empty string is intentional → "no project assigned".
        query["project_id"] = project_id or None
    docs = await _PocketDoc.find(query).to_list()
    return [await _resolved_wire_dict(d, user_id) for d in docs]


async def get(pocket_id: str, user_id: str) -> dict:
    """Get a single pocket. Access check: owner, shared_with, or workspace-visible.

    rippleSpec $source markers are resolved on read against the calling user's
    workspace context.
    """
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)
    if (
        pocket.owner != user_id
        and user_id not in pocket.shared_with
        and pocket.visibility == "private"
    ):
        raise Forbidden("pocket.access_denied", "You do not have access to this pocket")
    return await _resolved_wire_dict(doc, user_id)


async def update(pocket_id: str, user_id: str, body: UpdatePocketRequest) -> dict:
    """Update pocket fields. Edit-access required; visibility changes require ownership."""
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)

    _check_domain_edit_access(pocket, user_id)
    if body.visibility is not None:
        _check_domain_owner(pocket, user_id)

    normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
    if normalized_spec:
        validate_ripple_spec_logged(
            normalized_spec, pocket_id=str(doc.id), workspace_id=doc.workspace
        )

    if body.name is not None:
        doc.name = body.name
    if body.description is not None:
        doc.description = body.description
    if body.type is not None:
        doc.type = body.type
    if body.icon is not None:
        doc.icon = body.icon
    if body.color is not None:
        doc.color = body.color
    if body.visibility is not None:
        doc.visibility = body.visibility
    if normalized_spec is not None:
        doc.rippleSpec = normalized_spec
    if body.project_id is not None:
        # Empty string is the "unassign" signal; non-empty must point at a
        # real project in the same workspace.
        if body.project_id:
            await _ensure_project_in_workspace(doc.workspace, body.project_id)
            doc.project_id = body.project_id
        else:
            doc.project_id = None
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def _ensure_project_in_workspace(workspace_id: str, project_id: str) -> None:
    """Validate that ``project_id`` exists in the workspace. Lazy-imports
    the projects service to avoid a circular import at module load and
    to degrade silently if the projects entity is missing on a fork.
    """
    try:
        from pocketpaw_ee.cloud.projects import service as projects_service
    except Exception:
        # Projects entity unavailable on this branch — accept the id as-is
        # for forward-compat; the rollout PR has the entity present.
        return
    ok = await projects_service.exists_in_workspace(workspace_id, project_id)
    if not ok:
        raise NotFound("project", project_id)


async def unassign_project_on_pockets(workspace_id: str, project_id: str) -> int:
    """Soft-unassign every pocket in ``workspace_id`` whose ``project_id``
    matches. Called by ``projects.service.agent_delete`` when a project
    is removed — pockets keep their data, only the project reference
    clears. Returns the number of rows updated.

    Stays inside the pockets service so the 4-file rule holds (only
    ``pockets/service.py`` may write to the Pocket Beanie collection).
    """
    if not workspace_id or not project_id:
        return 0
    collection = _PocketDoc.get_pymongo_collection()
    result = await collection.update_many(
        {"workspace": workspace_id, "project_id": project_id},
        {"$set": {"project_id": None}},
    )
    return getattr(result, "modified_count", 0) or 0


async def delete(pocket_id: str, user_id: str) -> None:
    """Hard-delete a pocket. Owner only."""
    doc = await _fetch_pocket(pocket_id)
    if doc.owner != user_id:
        from pocketpaw_ee.guards.audit import log_denial

        log_denial(
            actor=user_id,
            action="pocket.share",
            code="pocket.not_owner",
            resource_id=str(doc.id),
        )
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")
    # Capture audience before delete so receivers can drop the pocket from
    # their list. The wire dict isn't useful here — only the id is.
    delete_payload = {
        "pocket_id": str(doc.id),
        "recipient_ids": [doc.owner, *list(doc.shared_with or [])],
    }
    if doc.visibility == "workspace":
        delete_payload["workspace_id"] = doc.workspace
    await doc.delete()
    await emit(PocketDeleted(data=delete_payload))


# ---------------------------------------------------------------------------
# Agent-generated pockets
# ---------------------------------------------------------------------------


async def create_from_ripple_spec(
    workspace_id: str,
    owner_id: str,
    ripple_spec: dict,
    description: str = "",
) -> str | None:
    """Auto-create a pocket from an agent-generated ripple spec.
    Returns the pocket id on success, None on failure."""
    try:
        normalized = normalize_ripple_spec(ripple_spec)
        if not normalized:
            return None
        validate_ripple_spec_logged(normalized, workspace_id=workspace_id)

        name = (
            normalized.get("lifecycle", {}).get("name")
            or normalized.get("name")
            or normalized.get("title")
            or "Agent-generated Pocket"
        )

        doc = _PocketDoc(
            workspace=workspace_id,
            name=name,
            description=description,
            type="ai-generated",
            owner=owner_id,
            visibility="workspace",
            rippleSpec=normalized,
        )
        await doc.insert()
        pocket_id = str(doc.id)
        logger.info("Auto-created pocket %s from ripple spec", pocket_id)
        await emit(PocketCreated(data=await _pocket_event_payload(doc)))
        return pocket_id
    except Exception:
        logger.warning("Failed to auto-create pocket from ripple spec", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


async def add_widget(pocket_id: str, user_id: str, body: AddWidgetRequest) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    widget = _build_widget_doc(
        {
            "name": body.name,
            "type": body.type,
            "icon": body.icon,
            "color": body.color,
            "span": body.span,
            "dataSourceType": body.data_source_type,
            "config": body.config,
            "props": body.props,
            "assignedAgent": body.assigned_agent,
        }
    )
    doc.widgets.append(widget)
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def update_widget(
    pocket_id: str, widget_id: str, user_id: str, body: UpdateWidgetRequest
) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    widget = next((w for w in doc.widgets if w.id == widget_id), None)
    if widget is None:
        raise NotFound("widget", widget_id)
    if body.name is not None:
        widget.name = body.name
    if body.type is not None:
        widget.type = body.type
    if body.icon is not None:
        widget.icon = body.icon
    if body.config is not None:
        widget.config = body.config
    if body.props is not None:
        widget.props = body.props
    if body.data is not None:
        widget.data = body.data
    if body.assigned_agent is not None:
        widget.assignedAgent = body.assigned_agent
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def remove_widget(pocket_id: str, widget_id: str, user_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    before = len(doc.widgets)
    doc.widgets = [w for w in doc.widgets if w.id != widget_id]
    if len(doc.widgets) == before:
        raise NotFound("widget", widget_id)
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def reorder_widgets(pocket_id: str, user_id: str, widget_ids: list[str]) -> dict:
    """Reorder widgets. Tolerates legacy callers that may omit ids
    (missing ids appended at the end) or include unknown ids (dropped)."""
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)

    existing_ids = {w.id for w in doc.widgets}
    seen: set[str] = set()
    ordered: list[str] = []
    for wid in widget_ids:
        if wid in existing_ids and wid not in seen:
            ordered.append(wid)
            seen.add(wid)
    for w in doc.widgets:
        if w.id not in seen:
            ordered.append(w.id)
            seen.add(w.id)

    if set(ordered) != existing_ids:
        # Defensive: should be impossible after the fill above
        raise ValidationError(
            "widget.reorder_mismatch",
            "widget_ids must match the current set exactly",
        )
    widgets_by_id = {w.id: w for w in doc.widgets}
    doc.widgets = [widgets_by_id[wid] for wid in ordered]
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


# ---------------------------------------------------------------------------
# Sharing — Share links
# ---------------------------------------------------------------------------


async def generate_share_link(pocket_id: str, user_id: str, access: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)

    token = secrets.token_urlsafe(32)
    doc.share_link_token = token
    doc.share_link_access = access
    await doc.save()
    # no-event: share-link state is owner-only; the token comes back inline
    # in this REST response. Broadcasting would leak the token.
    return {"token": token, "access": access, "url": f"/shared/{token}"}


async def revoke_share_link(pocket_id: str, user_id: str) -> None:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)

    doc.share_link_token = None
    doc.share_link_access = "view"
    await doc.save()
    # no-event: see ``generate_share_link``.


async def update_share_link(pocket_id: str, user_id: str, access: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)

    if not doc.share_link_token:
        raise NotFound("share_link", pocket_id)

    doc.share_link_access = access
    await doc.save()
    # no-event: see ``generate_share_link``.
    return {
        "token": doc.share_link_token,
        "access": access,
        "url": f"/shared/{doc.share_link_token}",
    }


async def access_via_share_link(token: str) -> dict:
    doc = await _PocketDoc.find_one(_PocketDoc.share_link_token == token)
    if doc is None:
        raise NotFound("pocket", "shared link")
    # no-resolve: share-link viewers have no auth context to build a ResolveCtx;
    # $source markers surface raw. v2: resolve with a guest-scoped context.
    return pocket_to_wire_dict(_pocket_to_domain(doc))


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


async def add_collaborator(pocket_id: str, user_id: str, body: AddCollaboratorRequest) -> None:
    doc = await _fetch_pocket(pocket_id)
    pocket = _pocket_to_domain(doc)
    _check_domain_owner(pocket, user_id)

    await _mutate_list_field(pocket_id, "shared_with", body.user_id, "add")

    await event_bus.emit(
        "pocket.shared",
        {
            "pocket_id": pocket.id,
            "owner_id": user_id,
            "collaborator_id": body.user_id,
            "access": body.access,
        },
    )


async def remove_collaborator(pocket_id: str, user_id: str, target_user_id: str) -> None:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_owner(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "shared_with", target_user_id, "remove")


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


async def add_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "team", member_id, "add")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


async def remove_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "team", member_id, "remove")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


async def add_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "agents", agent_id, "add")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


async def remove_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
    doc = await _fetch_pocket(pocket_id)
    _check_domain_edit_access(_pocket_to_domain(doc), user_id)
    await _mutate_list_field(pocket_id, "agents", agent_id, "remove")
    doc = await _fetch_pocket(pocket_id)
    return await _resolved_wire_dict(doc, user_id)


# ---------------------------------------------------------------------------
# Agent-facing helpers — back the in-process MCP write tools the cloud
# SSE chat agent uses to edit the pocket it lives inside. The MCP shape
# wrapper (``{ok, error}`` returns + SSE event push) lives in
# ``pockets/agent_context.py``; the Beanie ops live here.
# ---------------------------------------------------------------------------


_AGENT_INVISIBLE_FIELDS = (
    "share_link_token",
    "shared_with",
    "team",
    "agents",
)


def _agent_view_dict(doc: _PocketDoc) -> dict:
    """Json-safe pocket dict with secrets/relationship fields stripped.

    Used by the in-process MCP tool channel — same shape every
    ``agent_*`` helper returns on success.
    """
    import json

    raw = doc.model_dump(mode="json", by_alias=True, exclude_none=True)
    for k in _AGENT_INVISIBLE_FIELDS:
        raw.pop(k, None)
    return json.loads(json.dumps(raw, default=str))


async def _agent_load_doc(pocket_id: str) -> tuple[_PocketDoc | None, str | None]:
    """Load a pocket for an agent-initiated mutation, with workspace +
    access-control checks.

    Pulls ``workspace_id`` and ``user_id`` from the per-stream
    ContextVars set by ``agent_router._run_agent_stream``. Rejects when
    no stream is active, when the pocket belongs to a different
    workspace, or when the caller lacks edit access — mirroring the
    REST path's ``_check_domain_edit_access`` (owner OR shared_with OR
    workspace-visible). Cross-workspace mismatches return the same
    "not found" message as a genuinely missing pocket so the agent can't
    enumerate the existence of pockets in other tenants.
    """
    from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

    if not pocket_id or not isinstance(pocket_id, str):
        return None, "pocket_id is required (string)"
    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return None, (
            "no active workspace/user — agent pocket mutations require a cloud SSE chat stream"
        )
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load pocket {pocket_id}: {exc}"
    if doc is None or doc.workspace != workspace_id:
        return None, f"pocket {pocket_id} not found"
    if (
        doc.owner != user_id
        and user_id not in (doc.shared_with or [])
        and doc.visibility != "workspace"
    ):
        return None, f"pocket {pocket_id} not found"
    return doc, None


async def has_edit_access(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` if ``user_id`` may edit the pocket — owner,
    explicit shared_with, or workspace-visible. Raises ``NotFound`` if
    the pocket doesn't exist.

    Used by the ``require_pocket_edit`` FastAPI guard so the Pocket
    Beanie load stays inside the service.
    """
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)

    if doc.owner == user_id:
        return True
    if user_id in (doc.shared_with or []):
        return True
    return doc.visibility == "workspace"


async def is_owner(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` only if ``user_id`` owns the pocket. Raises
    ``NotFound`` if the pocket doesn't exist. Used by the
    ``require_pocket_owner`` FastAPI guard."""
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)
    return doc.owner == user_id


async def is_member(pocket_id: str, user_id: str) -> bool:
    """Return ``True`` if ``user_id`` may read the pocket — owner, team
    member, explicit shared_with, or any caller when visibility is
    ``workspace`` / public.

    Mirrors the read-side rule in ``agent_service._resolve_pocket`` so
    the Files panel filter and the chat scope-resolver agree on who
    sees a pocket's files. Raises ``NotFound`` if the pocket doesn't
    exist — callers convert to a 403 / 404 as appropriate.

    Stage 3.E: used by ``files/router.py`` to gate ``GET /files?pocket_id=X``
    for non-members and by the upload router for the read-side ABAC
    check (writes go through ``has_edit_access``).
    """
    try:
        pocket_oid = PydanticObjectId(pocket_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("pocket", pocket_id) from exc

    doc = await _PocketDoc.get(pocket_oid)
    if doc is None:
        raise NotFound("pocket", pocket_id)

    if doc.owner == user_id:
        return True
    if user_id in (doc.team or []):
        return True
    if user_id in (doc.shared_with or []):
        return True
    # Workspace-visible pockets: any workspace caller can read. The
    # route-level ``current_workspace_id`` dependency already enforced
    # workspace membership before we got here, so this branch implicitly
    # requires the caller be in this pocket's workspace.
    return getattr(doc, "visibility", "workspace") == "workspace"


async def _heal_node_ids(doc: _PocketDoc) -> None:
    """Defense-in-depth: stamp ``n_xxxxxxxx`` ids on a pocket's UISpec
    tree(s) if any node lacks one, persisting the heal.

    Pockets created or persisted after #1172 always carry node ids
    (``normalize_ripple_spec`` stamps them). This heals pockets written
    before the fix so they become editable on first agent read without
    a separate DB migration. Idempotent — a no-op when ids are already
    present.
    """
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        return
    changed = False
    ui = spec.get("ui")
    if isinstance(ui, dict) and spec_ops.ensure_ids(ui):
        changed = True
    panes = spec.get("panes")
    if isinstance(panes, dict):
        for pane in panes.values():
            if isinstance(pane, dict) and spec_ops.ensure_ids(pane):
                changed = True
    if changed:
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("pocket %s node-id heal save failed: %s", doc.id, exc)


async def agent_view(pocket_id: str) -> tuple[dict | None, str | None]:
    """Read-only fetch — returns ``(view_dict, None)`` on success or
    ``(None, error)`` on failure.

    Self-heals node ids: a legacy pocket persisted before #1172 has an
    id-less ``ui`` tree, so the chat agent would have no ``n_xxxxxxxx``
    id to address with granular edit ops. ``_heal_node_ids`` stamps and
    persists them on first read.

    Note: $source markers in rippleSpec are intentionally NOT resolved
    here. The agent must see raw markers so that on edit it preserves
    them; resolving would let the agent bake a snapshot of live data
    into the spec, defeating the marker mechanism. Resolution happens
    only in ``service.get`` (the user-facing read path)."""
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    await _heal_node_ids(doc)
    return _agent_view_dict(doc), None


async def agent_list(workspace_id: str, user_id: str) -> list[dict]:
    """Compact list of pockets the user can see in this workspace.

    Returned shape per pocket: ``{id, name, description, type, icon,
    color, owner}``. The full ``rippleSpec`` is intentionally excluded —
    callers (the in-process MCP ``list_pockets`` tool, the
    ``cloud_list_pockets`` CLI) hit this on every creation flow as the
    "have we already got one of these?" check, so the payload stays
    cheap. Visibility rules mirror ``list_pockets``: owned by the user,
    explicitly shared, or workspace-visible.
    """
    if not workspace_id or not user_id:
        return []
    docs = await _PocketDoc.find(
        {
            "workspace": workspace_id,
            "$or": [
                {"owner": user_id},
                {"shared_with": user_id},
                {"visibility": "workspace"},
            ],
        }
    ).to_list()
    out: list[dict] = []
    for d in docs:
        out.append(
            {
                "id": str(d.id),
                "name": d.name,
                "description": d.description or "",
                "type": d.type or "",
                "icon": d.icon or "",
                "color": d.color or "",
                "owner": d.owner,
            }
        )
    return out


async def agent_update(
    pocket_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    ripple_spec: dict | None = None,
) -> tuple[dict | None, str | None]:
    """Patch top-level pocket fields. Only fields the caller explicitly
    provides are touched. ``ripple_spec`` is normalized."""
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    if name is not None:
        doc.name = name
    if description is not None:
        doc.description = description
    if icon is not None:
        doc.icon = icon
    if color is not None:
        doc.color = color
    if ripple_spec is not None:
        doc.rippleSpec = normalize_ripple_spec(ripple_spec)
        if doc.rippleSpec:
            validate_ripple_spec_logged(
                doc.rippleSpec, pocket_id=str(doc.id), workspace_id=doc.workspace
            )
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), None


async def agent_add_widget(pocket_id: str, widget: dict) -> tuple[dict | None, str | None]:
    if not isinstance(widget, dict):
        return None, "widget must be a JSON object"
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    try:
        new_widget = _build_widget_doc(widget)
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid widget spec: {exc}"
    doc.widgets.append(new_widget)
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), None


async def agent_update_widget(
    pocket_id: str, widget_id: str, fields: dict
) -> tuple[dict | None, str | None]:
    if not isinstance(fields, dict):
        return None, "fields must be a JSON object"
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    widget = next((w for w in doc.widgets if w.id == widget_id), None)
    if widget is None:
        return None, f"widget {widget_id} not found in pocket {pocket_id}"
    for k in ("name", "type", "icon", "color", "span", "data", "assignedAgent"):
        if k in fields:
            setattr(widget, k, fields[k])
    if "config" in fields and isinstance(fields["config"], dict):
        widget.config = fields["config"]
    if "props" in fields and isinstance(fields["props"], dict):
        widget.props = fields["props"]
    if "dataSourceType" in fields:
        widget.dataSourceType = fields["dataSourceType"]
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), None


async def agent_remove_widget(pocket_id: str, widget_id: str) -> tuple[dict | None, str | None]:
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
    before = len(doc.widgets)
    doc.widgets = [w for w in doc.widgets if w.id != widget_id]
    if len(doc.widgets) == before:
        return None, f"widget {widget_id} not found in pocket {pocket_id}"
    try:
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        return None, f"save failed: {exc}"
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), None


# ---------------------------------------------------------------------------
# Granular rippleSpec.ui mutations — agent-facing
# ---------------------------------------------------------------------------

# Per-pocket asyncio.Lock cache. Granular ops on the same pocket
# serialize so a flurry of specialist calls can't race on doc.save().
# Bounded LRU so the cache doesn't grow unboundedly with pocket churn.
_POCKET_LOCK_CACHE_MAX = 256
_pocket_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()


def _pocket_lock(pocket_id: str) -> asyncio.Lock:
    """Return (creating if needed) the per-pocket lock. LRU-evicted."""
    lock = _pocket_locks.get(pocket_id)
    if lock is None:
        lock = asyncio.Lock()
        _pocket_locks[pocket_id] = lock
        if len(_pocket_locks) > _POCKET_LOCK_CACHE_MAX:
            _pocket_locks.popitem(last=False)
    else:
        _pocket_locks.move_to_end(pocket_id)
    return lock


def _spec_root(doc: _PocketDoc) -> tuple[dict[str, Any] | None, str | None]:
    """Return the mutable ``rippleSpec.ui`` root for a doc, or
    ``(None, error)`` if the pocket has no spec or no root node.

    Mutating the returned dict mutates ``doc.rippleSpec['ui']`` in place
    — the caller follows up with ``doc.save()`` to persist.
    """
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        return None, "pocket has no rippleSpec to mutate"
    ui = spec.get("ui")
    if not isinstance(ui, dict):
        return None, "pocket rippleSpec has no 'ui' root"
    return ui, None


async def _load_and_ensure_ids(
    pocket_id: str,
) -> tuple[_PocketDoc | None, dict[str, Any] | None, str | None]:
    """Load the pocket doc, get its UI root, and ensure every node has
    an id (persisting if newly assigned). Returns
    ``(doc, ui_root, None)`` on success.
    """
    doc, err = await _agent_load_doc(pocket_id)
    if err or doc is None:
        return None, None, err
    ui, err = _spec_root(doc)
    if err or ui is None:
        return None, None, err
    if spec_ops.ensure_ids(ui):
        # Persist newly-assigned ids so subsequent ops can target them.
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, None, f"id-assignment save failed: {exc}"
    return doc, ui, None


async def agent_add_node(
    pocket_id: str,
    *,
    parent_id: str,
    spec: dict[str, Any],
    after_id: str | None = None,
) -> tuple[dict | None, str | None]:
    """Insert ``spec`` as a child of ``parent_id`` (after ``after_id`` or
    appended). The new node's id is assigned if absent.

    Returns ``({"subtree": <new-node>, "pocket": <view>}, None)`` on
    success.
    """
    if not isinstance(spec, dict):
        return None, "spec must be a JSON object"
    if not parent_id:
        return None, "parent_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        parent = spec_ops.find_by_id(ui, parent_id)
        if parent is None:
            return None, f"no node with id {parent_id!r}"
        new_node = dict(spec)
        if not spec_ops.is_valid_id(new_node.get("id")):
            new_node["id"] = spec_ops.new_node_id()
        # Make sure any nested children also have ids.
        spec_ops.ensure_ids(new_node)
        try:
            spec_ops.insert_child(parent, new_node, after_id=after_id)
        except ValueError as exc:
            return None, str(exc)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"subtree": new_node, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_replace_node(
    pocket_id: str,
    *,
    node_id: str,
    spec: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Replace the subtree rooted at ``node_id`` with ``spec``. The
    target's id is preserved if ``spec['id']`` is absent.

    Returns ``({"subtree": <new>, "old": <prev>, "pocket": <view>}, None)``.
    """
    if not isinstance(spec, dict):
        return None, "spec must be a JSON object"
    if not node_id:
        return None, "node_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        new_node = dict(spec)
        try:
            old = spec_ops.replace_node(ui, node_id, new_node)
        except ValueError as exc:
            return None, str(exc)
        spec_ops.ensure_ids(new_node)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"subtree": new_node, "old": old, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_set_node_prop(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    value: Any,
) -> tuple[dict | None, str | None]:
    """Set a single prop on ``node_id``. ``prop`` writes into ``props``
    by default; top-level node keys (``show``, ``bind``, ``on_click``,
    etc.) are addressable by their bare name. Dotted paths
    (``data.rows``) walk inside ``props``.

    Returns ``({"subtree": <node>, "old_value": <prev>, "pocket": <view>},
    None)``.
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"
        try:
            old = spec_ops.set_prop(node, prop, value)
        except ValueError as exc:
            return None, str(exc)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "subtree": node,
                "old_value": old,
                "prop": prop,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_set_prop_array_item(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    match: dict[str, Any],
    partial: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Merge ``partial`` into the first item of ``node.props[prop]`` that
    matches ``match``. Surgical alternative to ``set_node_prop`` when the
    agent only wants to change one row/slice in a chart/table/etc.

    Merge is SHALLOW: top-level keys in ``partial`` overwrite the matched
    item's keys, but nested dicts/lists are replaced wholesale rather
    than deep-merged. Matches ``patch_state`` semantics; if the agent
    needs to preserve nested structure, fetch the item first and pass a
    fully-built nested dict in ``partial``. Non-dict matched items are
    replaced wholesale by ``partial`` (rare — most prop-array items are
    dicts).

    Returns ``({"item_index": int, "item": <new item>, "old_item": <prev>,
    "pocket": <view>}, None)``.

    Error codes (returned as the error string):
      * ``"unsupported_prop_array: <type>.<prop>"``
      * ``"no node with id 'n_xxx'"``
      * ``"prop {prop!r} is not an array on node {node_id!r}"``
      * ``"not_found: no item matched"``
      * ``"ambiguous: N items matched; candidates=[idx, ...]"``
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"
    if not isinstance(partial, dict):
        return None, "partial must be a dict"

    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"

        wtype = node.get("type")
        if not isinstance(wtype, str) or not prop_arrays.is_allowed(wtype, prop):
            return None, f"unsupported_prop_array: {wtype}.{prop}"

        props = node.get("props")
        if props is None:
            return None, f"node {node_id!r} has no props"
        if not isinstance(props, dict):
            return None, f"node {node_id!r} has non-dict props"
        arr = props.get(prop)
        if not isinstance(arr, list):
            return None, f"prop {prop!r} is not an array on node {node_id!r}"

        try:
            candidates = spec_ops.match_array_item_candidates(arr, match)
        except ValueError as exc:
            return None, str(exc)
        if len(candidates) == 0:
            return None, "not_found: no item matched"
        if len(candidates) > 1:
            preview = candidates[:5]
            return None, f"ambiguous: {len(candidates)} items matched; candidates={preview}"

        idx = candidates[0]
        old_item = copy.deepcopy(arr[idx])
        if isinstance(arr[idx], dict):
            arr[idx] = {**arr[idx], **partial}
        else:
            arr[idx] = partial  # non-dict element: replace wholesale

        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "item_index": idx,
                "item": arr[idx],
                "old_item": old_item,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_append_prop_array_item(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    value: Any,
    after: dict[str, Any] | None = None,
) -> tuple[dict | None, str | None]:
    """Append ``value`` to ``node.props[prop]``. If ``after`` is given,
    insert immediately AFTER the first item matching that ItemMatch.

    Returns ``({"item_index": int, "item": <inserted>, "pocket": <view>}, None)``.

    Errors: see ``agent_set_prop_array_item``. ``after`` resolution uses
    the same match grammar; ``not_found`` / ``ambiguous`` propagate.
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"

    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"

        wtype = node.get("type")
        if not isinstance(wtype, str) or not prop_arrays.is_allowed(wtype, prop):
            return None, f"unsupported_prop_array: {wtype}.{prop}"

        # Append intentionally creates props and the array on demand —
        # set/remove bail when either is missing because there is no
        # item to address, but append's whole job is to add the first
        # one. Asymmetry is by design.
        props = node.setdefault("props", {})
        if not isinstance(props, dict):
            return None, f"node {node_id!r} has non-dict props"
        arr = props.get(prop)
        if arr is None:
            arr = []
            props[prop] = arr
        if not isinstance(arr, list):
            return None, f"prop {prop!r} is not an array on node {node_id!r}"

        if after is None:
            arr.append(value)
            idx = len(arr) - 1
        else:
            try:
                candidates = spec_ops.match_array_item_candidates(arr, after)
            except ValueError as exc:
                return None, str(exc)
            if len(candidates) == 0:
                return None, "not_found: after target did not match"
            if len(candidates) > 1:
                return (
                    None,
                    f"ambiguous: after matched {len(candidates)} items; "
                    f"candidates={candidates[:5]}",
                )
            arr.insert(candidates[0] + 1, value)
            idx = candidates[0] + 1

        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "item_index": idx,
                "item": value,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_prop_array_item(
    pocket_id: str,
    *,
    node_id: str,
    prop: str,
    match: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Remove the first item in ``node.props[prop]`` matching ``match``.

    Returns ``({"removed_index": int, "removed_item": Any, "pocket": <view>},
    None)``.

    Errors: see ``agent_set_prop_array_item``. Refuses ambiguous matches —
    the agent must disambiguate.
    """
    if not node_id:
        return None, "node_id is required"
    if not prop:
        return None, "prop is required"

    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        node = spec_ops.find_by_id(ui, node_id)
        if node is None:
            return None, f"no node with id {node_id!r}"

        wtype = node.get("type")
        if not isinstance(wtype, str) or not prop_arrays.is_allowed(wtype, prop):
            return None, f"unsupported_prop_array: {wtype}.{prop}"

        props = node.get("props")
        if props is None:
            return None, f"node {node_id!r} has no props"
        if not isinstance(props, dict):
            return None, f"node {node_id!r} has non-dict props"
        arr = props.get(prop)
        if not isinstance(arr, list):
            return None, f"prop {prop!r} is not an array on node {node_id!r}"

        try:
            candidates = spec_ops.match_array_item_candidates(arr, match)
        except ValueError as exc:
            return None, str(exc)
        if len(candidates) == 0:
            return None, "not_found: no item matched"
        if len(candidates) > 1:
            return None, f"ambiguous: {len(candidates)} items matched; candidates={candidates[:5]}"

        idx = candidates[0]
        removed = arr.pop(idx)

        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "removed_index": idx,
                "removed_item": removed,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_move_node(
    pocket_id: str,
    *,
    node_id: str,
    new_parent_id: str,
    after_id: str | None = None,
) -> tuple[dict | None, str | None]:
    """Move ``node_id`` under ``new_parent_id`` (after ``after_id`` or
    appended). Refuses to move the root or to move into a descendant.

    Returns ``({"subtree": <moved>, "old_parent_id": ..., "old_index": ...,
    "pocket": <view>}, None)``.
    """
    if not node_id:
        return None, "node_id is required"
    if not new_parent_id:
        return None, "new_parent_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        try:
            old_parent_id, old_idx = spec_ops.move_node(
                ui, node_id, new_parent_id, after_id=after_id
            )
        except ValueError as exc:
            return None, str(exc)
        moved = spec_ops.find_by_id(ui, node_id) or {}
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "subtree": moved,
                "old_parent_id": old_parent_id,
                "old_index": old_idx,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_node(
    pocket_id: str,
    *,
    node_id: str,
) -> tuple[dict | None, str | None]:
    """Remove the subtree at ``node_id``. Returns the removed subtree
    plus enough position info to rebuild the inverse.

    Returns ``({"removed_id": ..., "removed": <subtree>, "parent_id": ...,
    "index": ..., "pocket": <view>}, None)``.
    """
    if not node_id:
        return None, "node_id is required"
    async with _pocket_lock(pocket_id):
        doc, ui, err = await _load_and_ensure_ids(pocket_id)
        if err or doc is None or ui is None:
            return None, err
        try:
            parent, _key, idx, removed = spec_ops.remove_node(ui, node_id)
        except ValueError as exc:
            return None, str(exc)
        if doc.rippleSpec is not None:
            doc.rippleSpec = normalize_ripple_spec(doc.rippleSpec) or doc.rippleSpec
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "removed_id": node_id,
                "removed": removed,
                "parent_id": str(parent.get("id", "")),
                "index": idx,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


# ---------------------------------------------------------------------------
# Granular rippleSpec.state mutations — agent-facing
# ---------------------------------------------------------------------------
#
# The "data" half of the 3-layer mutation rule:
# - data the user sees   → set_state / append_state / remove_state / patch_state
# - widget appearance    → set_node_prop
# - widget structure     → add_node / move_node / remove_node
#
# Reuses the per-pocket asyncio.Lock cache from the node ops above so
# concurrent state + node mutations on the same pocket serialize.


def _state_root(doc: _PocketDoc) -> tuple[dict[str, Any], str | None]:
    """Return the mutable ``rippleSpec.state`` dict for a doc, creating
    it (and the parent ``rippleSpec``) if absent.

    Unlike ``_spec_root`` (which requires a ``ui`` root), state is
    legitimately empty for new pockets, so we materialise it on demand.
    """
    spec = doc.rippleSpec
    if not isinstance(spec, dict):
        spec = {}
        doc.rippleSpec = spec
    state = spec.get("state")
    if not isinstance(state, dict):
        state = {}
        spec["state"] = state
    return state, None


async def agent_set_state(
    pocket_id: str,
    *,
    path: str,
    value: Any,
) -> tuple[dict | None, str | None]:
    """Write ``value`` at ``path`` in the pocket's state. Returns
    ``({"path": ..., "value": ..., "old_value": ..., "pocket": <view>},
    None)`` on success.

    See ``state_ops`` for path syntax (dotted with bracket indexing).
    """
    if not path:
        return None, "path is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            old = state_ops.set_path(state, path, value)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "path": path,
                "value": value,
                "old_value": old,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_append_state(
    pocket_id: str,
    *,
    path: str,
    item: Any,
) -> tuple[dict | None, str | None]:
    """Append ``item`` to the array at ``path``. Creates an empty list
    if the path is absent. Returns the new length plus the appended item.
    """
    if not path:
        return None, "path is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            new_len = state_ops.append_path(state, path, item)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {
                "path": path,
                "item": item,
                "new_length": new_len,
                "pocket": _agent_view_dict(doc),
            },
            None,
        )


async def agent_remove_state(
    pocket_id: str,
    *,
    path: str,
) -> tuple[dict | None, str | None]:
    """Remove the value at ``path``. Returns the removed value (used as
    the inverse for undo)."""
    if not path:
        return None, "path is required"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            removed = state_ops.remove_path(state, path)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"path": path, "removed": removed, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_patch_state(
    pocket_id: str,
    *,
    partial: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Shallow-merge ``partial`` into state at the top level. For
    batched independent-key writes. Returns the previous values of
    overwritten keys."""
    if not isinstance(partial, dict):
        return None, "partial must be a dict"
    async with _pocket_lock(pocket_id):
        doc, err = await _agent_load_doc(pocket_id)
        if err or doc is None:
            return None, err
        state, err = _state_root(doc)
        if err:
            return None, err
        try:
            prev = state_ops.patch(state, partial)
        except ValueError as exc:
            return None, str(exc)
        try:
            await doc.save()
        except Exception as exc:  # noqa: BLE001
            return None, f"save failed: {exc}"
        await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
        return (
            {"partial": partial, "previous": prev, "pocket": _agent_view_dict(doc)},
            None,
        )


async def agent_create(
    *,
    workspace_id: str,
    owner_id: str,
    name: str,
    description: str = "",
    type_: str = "custom",
    icon: str = "",
    color: str = "",
    ripple_spec: dict | None = None,
) -> tuple[dict | None, str | None, str | None]:
    """Insert a brand-new pocket owned by ``owner_id`` in ``workspace_id``.

    Returns ``(view_dict, pocket_id, None)`` on success or
    ``(None, None, error)`` on failure. Returning the id alongside the
    view lets the caller link sessions / push SSE events without
    re-parsing the dict.
    """
    if not name:
        return None, None, "name is required"
    normalized = normalize_ripple_spec(ripple_spec) if ripple_spec else None
    if normalized:
        validate_ripple_spec_logged(normalized, workspace_id=workspace_id)
    try:
        doc = _PocketDoc(
            workspace=workspace_id,
            name=name,
            description=description,
            type=type_,
            icon=icon,
            color=color,
            owner=owner_id,
            rippleSpec=normalized,
            visibility="workspace",
        )
        await doc.insert()
    except Exception as exc:  # noqa: BLE001
        return None, None, f"insert failed: {exc}"
    await emit(PocketCreated(data=await _pocket_event_payload(doc)))
    return _agent_view_dict(doc), str(doc.id), None


async def create_pocket_and_session(
    spec: dict[str, Any],
    session_key: str,
    user_id: str | None = None,
    workspace_id: str | None = None,
) -> str | None:
    """Create a pocket + linked chat session in MongoDB. Returns the pocket
    id, or ``None`` on failure.

    Backs the core ``pocketpaw.pockets`` PocketWriter extension point:
    ``pocketpaw.agents.loop`` calls this when a local-mode CreatePocketTool
    emits a ``pocket_event: created``. Identity arrives as explicit args
    (threaded from ``InboundMessage.metadata``) rather than per-stream
    ContextVars, because the agent loop has no SSE request scope. When
    ``user_id`` / ``workspace_id`` are missing it falls back to legacy
    heuristics — ``user.active_workspace`` first, then first-owned
    workspace, then any workspace — so self-hosted single-user deployments
    (CLI, Telegram) without JWT auth still work.
    """
    try:
        from datetime import UTC, datetime

        from pocketpaw_ee.cloud.models.session import Session
        from pocketpaw_ee.cloud.models.user import User
        from pocketpaw_ee.cloud.models.workspace import Workspace

        # ── User selection ──────────────────────────────────────────────
        # Prefer an explicitly-threaded user id so agent-created pockets
        # land under the caller, not whichever user comes first out of Mongo.
        user = None
        if user_id:
            try:
                user = await User.get(PydanticObjectId(user_id))
            except Exception:  # noqa: BLE001
                logger.warning("Invalid cloud_user_id %r; falling back to first user", user_id)
                user = None
        if not user:
            user = await User.find_one()
        if not user:
            logger.warning("Cannot create pocket — no user in DB")
            return None
        user_id = str(user.id)

        # ── Workspace selection ─────────────────────────────────────────
        # Priority: explicit workspace_id → user.active_workspace → first
        # owned workspace → any workspace.
        workspace = None
        target_ws = workspace_id or getattr(user, "active_workspace", None)
        if target_ws:
            try:
                workspace = await Workspace.get(PydanticObjectId(target_ws))
            except Exception:  # noqa: BLE001
                workspace = None
        if not workspace:
            workspace = await Workspace.find_one(Workspace.owner == user_id)
        if not workspace:
            workspace = await Workspace.find_one()
        if not workspace:
            logger.warning("Cannot create pocket — no workspace in DB")
            return None
        workspace_id = str(workspace.id)

        # Create the pocket through the standard service entry point.
        meta = spec.get("metadata", {})
        pocket = await create(
            workspace_id,
            user_id,
            CreatePocketRequest(
                name=spec.get("title") or spec.get("name") or "Untitled",
                description=spec.get("description", ""),
                type=meta.get("category", "custom"),
                icon="sparkles",
                color=meta.get("color", "#0A84FF"),
                rippleSpec=spec,
            ),
        )
        pocket_id = str(pocket["_id"])

        # Link (find-or-create) the chat session to the new pocket.
        safe_key = session_key.replace(":", "_") if session_key else ""
        if safe_key:
            existing = await Session.find_one(Session.sessionId == safe_key)
            if existing:
                existing.pocket = pocket_id
                await existing.save()
            else:
                session = Session(
                    sessionId=safe_key,
                    workspace=workspace_id,
                    owner=user_id,
                    title=spec.get("title") or "New Chat",
                    pocket=pocket_id,
                    lastActivity=datetime.now(UTC),
                )
                await session.insert()

        logger.info("Created pocket %s + session %s in MongoDB", pocket_id, safe_key)
        return pocket_id
    except Exception:
        logger.warning("Failed to create pocket/session in MongoDB", exc_info=True)
        return None


__all__ = [
    "access_via_share_link",
    "add_agent",
    "add_collaborator",
    "add_team_member",
    "add_widget",
    "agent_add_node",
    "agent_add_widget",
    "agent_append_state",
    "agent_create",
    "agent_list",
    "agent_move_node",
    "agent_patch_state",
    "agent_remove_node",
    "agent_remove_state",
    "agent_remove_widget",
    "agent_replace_node",
    "agent_set_node_prop",
    "agent_set_state",
    "agent_update",
    "agent_update_widget",
    "agent_view",
    "create",
    "create_from_ripple_spec",
    "create_pocket_and_session",
    "delete",
    "generate_share_link",
    "get",
    "has_edit_access",
    "is_member",
    "is_owner",
    "list_pockets",
    "remove_agent",
    "remove_collaborator",
    "remove_team_member",
    "remove_widget",
    "reorder_widgets",
    "revoke_share_link",
    "unassign_project_on_pockets",
    "update",
    "update_share_link",
    "update_widget",
]
