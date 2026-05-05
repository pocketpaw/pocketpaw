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
"""

from __future__ import annotations

import logging
import secrets

from beanie import PydanticObjectId

from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    PocketCreated,
    PocketDeleted,
    PocketUpdated,
)
from ee.cloud.models.pocket import Pocket as _PocketDoc
from ee.cloud.models.pocket import Widget as _WidgetDoc
from ee.cloud.pockets.domain import Pocket, Widget, WidgetPosition
from ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
    pocket_to_wire_dict,
)
from ee.cloud.ripple_normalizer import normalize_ripple_spec
from ee.cloud.shared.errors import Forbidden, NotFound, ValidationError
from ee.cloud.shared.events import event_bus

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
        from pocketpaw.ee.guards.audit import log_denial

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
    from pocketpaw.ee.guards.audit import log_denial

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
        from ee.cloud import ripple_sources  # noqa: F401  — register sources
        from ee.cloud.ripple_resolver import ResolveCtx, resolve_ripple_spec

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
    """Create a pocket with optional agents, widgets, and rippleSpec."""
    from ee.cloud.sessions import service as sessions_service

    normalized_spec = normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None
    widget_docs = [_build_widget_doc(w) for w in (body.widgets or [])]

    doc = _PocketDoc(
        workspace=workspace_id,
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


async def list_pockets(workspace_id: str, user_id: str) -> list[dict]:
    """List pockets visible to the user (owned, shared_with, or workspace-visible).

    Each returned pocket has its rippleSpec ``$source`` markers resolved
    against ``user_id``'s context — the desktop client renders the canvas
    directly from this list response, so unresolved markers would surface
    as empty widgets. Resolution per pocket is independent; sources that
    fail fall back to raw markers individually (see ``_resolved_wire_dict``).
    """
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
    await doc.save()
    await emit(PocketUpdated(data=await _pocket_event_payload(doc)))
    return await _resolved_wire_dict(doc, user_id)


async def delete(pocket_id: str, user_id: str) -> None:
    """Hard-delete a pocket. Owner only."""
    doc = await _fetch_pocket(pocket_id)
    if doc.owner != user_id:
        from pocketpaw.ee.guards.audit import log_denial

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
    if not pocket_id or not isinstance(pocket_id, str):
        return None, "pocket_id is required (string)"
    try:
        doc = await _PocketDoc.get(PydanticObjectId(pocket_id))
    except Exception as exc:  # noqa: BLE001
        return None, f"could not load pocket {pocket_id}: {exc}"
    if doc is None:
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


async def agent_view(pocket_id: str) -> tuple[dict | None, str | None]:
    """Read-only fetch — returns ``(view_dict, None)`` on success or
    ``(None, error)`` on failure.

    Note: $source markers in rippleSpec are intentionally NOT resolved
    here. The agent must see raw markers so that on edit it preserves
    them; resolving would let the agent bake a snapshot of live data
    into the spec, defeating the marker mechanism. Resolution happens
    only in ``service.get`` (the user-facing read path)."""
    doc, err = await _agent_load_doc(pocket_id)
    if err:
        return None, err
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


__all__ = [
    "access_via_share_link",
    "add_agent",
    "add_collaborator",
    "add_team_member",
    "add_widget",
    "agent_add_widget",
    "agent_create",
    "agent_list",
    "agent_remove_widget",
    "agent_update",
    "agent_update_widget",
    "agent_view",
    "create",
    "create_from_ripple_spec",
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
    "update",
    "update_share_link",
    "update_widget",
]
