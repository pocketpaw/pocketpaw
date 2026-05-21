"""Pockets domain — FastAPI router.

Updated: 2026-04-19 (Cluster B Sub-PR #3) — Added three new routes that
close UI-TESTING-GUIDE §11 gap B5 (no widget layout save/share):

    POST /pockets/{id}/export-layout   — return the pocket's layout as YAML
    POST /pockets/templates            — save a YAML template to "My templates"
    GET  /pockets/templates            — list the workspace's user templates

The YAML + in-process store live in ee.cloud.pockets.layouts. Export is
pure. Template storage is workspace-scoped and in-process for now; the
REST contract matches the MongoDB-backed version that Wave 4 will ship.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from starlette.responses import Response

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    ReorderWidgetsRequest,
    ShareLinkRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
)
from pocketpaw_ee.cloud.pockets.layouts import (
    UserPocketTemplate,
    UserTemplateStore,
    export_layout_yaml,
    get_user_template_store,
    parse_layout_yaml,
)
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_pocket_edit,
    require_pocket_owner,
)

router = APIRouter(prefix="/pockets", tags=["Pockets"], dependencies=[Depends(require_license)])


# ---------------------------------------------------------------------------
# Layout export + user templates — Cluster B Sub-PR #3.
# ---------------------------------------------------------------------------


class ExportLayoutRequest(BaseModel):
    """Optional overrides on the metadata block of the exported YAML.

    The pocket's own name / description / category seed the defaults —
    the override fields let the operator ship the template under a
    different display name without renaming the source pocket. Empty
    fields fall back to the pocket's values.
    """

    name: str | None = None
    description: str | None = None
    category: str | None = None


class ExportLayoutResponse(BaseModel):
    pocket_id: str
    yaml: str


class CreateTemplateRequest(BaseModel):
    """Body for POST /pockets/templates.

    ``yaml_source`` is the YAML a previous /export-layout call produced
    or a hand-authored equivalent. ``name`` / ``description`` /
    ``category`` are required on the template row even when the YAML
    carries them — the store indexes on those fields for the gallery.
    """

    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    category: str = "custom"
    yaml_source: str = Field(min_length=1)


class UserTemplateResponse(BaseModel):
    id: str
    workspace_id: str
    owner_id: str
    name: str
    description: str
    category: str
    spec: dict
    created_at: str


@router.post("/{pocket_id}/export-layout", response_model=ExportLayoutResponse)
async def export_layout(
    pocket_id: str,
    body: ExportLayoutRequest | None = None,
    user_id: str = Depends(current_user_id),
) -> ExportLayoutResponse:
    """Serialise this pocket's layout as YAML.

    Read-only, safe on any pocket the caller can fetch. The YAML is
    deterministic (sort_keys=True) so a round-trip save-then-create
    reproduces the original layout byte-identically — the PR's e2e
    test depends on that guarantee.
    """

    body = body or ExportLayoutRequest()
    pocket = await pockets_service.get(pocket_id, user_id)
    widgets_dump = pocket.get("widgets") or []
    yaml_text = export_layout_yaml(
        pocket_id=pocket_id,
        name=body.name or pocket.get("name", ""),
        description=body.description or pocket.get("description", ""),
        category=body.category or pocket.get("type", "custom"),
        ripple_spec=pocket.get("rippleSpec"),
        widgets=widgets_dump,
    )
    return ExportLayoutResponse(pocket_id=pocket_id, yaml=yaml_text)


@router.post("/templates", response_model=UserTemplateResponse)
async def create_user_template(
    body: CreateTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
    store: UserTemplateStore = Depends(get_user_template_store),
) -> UserTemplateResponse:
    """Persist a user-defined YAML template under the caller's workspace.

    The template shows up in PocketTemplates's "My templates" category
    once Cluster B's frontend wires the read side. Malformed YAML
    returns 400 with a human-readable message instead of 500 — the UI
    surfaces the error inline on the Save-as-template dialog.
    """

    try:
        spec = parse_layout_yaml(body.yaml_source)
    except ValueError as exc:
        raise CloudError(400, "layout.invalid_yaml", str(exc)) from None

    row = store.save(
        UserPocketTemplate(
            id=uuid4().hex,
            workspace_id=workspace_id,
            owner_id=user_id,
            name=body.name,
            description=body.description,
            category=body.category,
            spec=spec,
        ),
    )
    return UserTemplateResponse(**row.to_dict())


@router.get("/templates", response_model=list[UserTemplateResponse])
async def list_user_templates(
    workspace_id: str = Depends(current_workspace_id),
    store: UserTemplateStore = Depends(get_user_template_store),
) -> list[UserTemplateResponse]:
    """List user-defined templates for the caller's active workspace."""

    return [UserTemplateResponse(**row.to_dict()) for row in store.list_for_workspace(workspace_id)]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("")
async def create_pocket(
    body: CreatePocketRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.create(workspace_id, user_id, body)


@router.get("")
async def list_pockets(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
    project_id: str | None = Query(default=None, alias="project_id"),
) -> list[dict]:
    return await pockets_service.list_pockets(workspace_id, user_id, project_id=project_id)


@router.get("/{pocket_id}")
async def get_pocket(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.get(pocket_id, user_id)


@router.patch("/{pocket_id}", dependencies=[Depends(require_pocket_edit)])
async def update_pocket(
    pocket_id: str,
    body: UpdatePocketRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update(pocket_id, user_id, body)


@router.delete("/{pocket_id}", status_code=204, dependencies=[Depends(require_pocket_owner)])
async def delete_pocket(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.delete(pocket_id, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/widgets")
async def add_widget(
    pocket_id: str,
    body: AddWidgetRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.add_widget(pocket_id, user_id, body)


@router.patch("/{pocket_id}/widgets/{widget_id}")
async def update_widget(
    pocket_id: str,
    widget_id: str,
    body: UpdateWidgetRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update_widget(pocket_id, widget_id, user_id, body)


@router.delete("/{pocket_id}/widgets/{widget_id}", status_code=204)
async def remove_widget(
    pocket_id: str,
    widget_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_widget(pocket_id, widget_id, user_id)
    return Response(status_code=204)


@router.post("/{pocket_id}/widgets/reorder")
async def reorder_widgets(
    pocket_id: str,
    body: ReorderWidgetsRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.reorder_widgets(pocket_id, user_id, body.widget_ids)


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/team")
async def add_team_member(
    pocket_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.add_team_member(pocket_id, user_id, body["member_id"])


@router.delete("/{pocket_id}/team/{member_id}", status_code=204)
async def remove_team_member(
    pocket_id: str,
    member_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_team_member(pocket_id, user_id, member_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/agents")
async def add_agent(
    pocket_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
) -> dict:
    agent_id = body.get("agentId") or body.get("agent_id")
    return await pockets_service.add_agent(pocket_id, user_id, agent_id)


@router.delete("/{pocket_id}/agents/{agent_id}", status_code=204)
async def remove_agent(
    pocket_id: str,
    agent_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_agent(pocket_id, user_id, agent_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Sharing — Share links
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/share", dependencies=[Depends(require_pocket_owner)])
async def generate_share_link(
    pocket_id: str,
    body: ShareLinkRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.generate_share_link(pocket_id, user_id, body.access)


@router.delete("/{pocket_id}/share", status_code=204, dependencies=[Depends(require_pocket_owner)])
async def revoke_share_link(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.revoke_share_link(pocket_id, user_id)
    return Response(status_code=204)


@router.patch("/{pocket_id}/share", dependencies=[Depends(require_pocket_owner)])
async def update_share_link_access(
    pocket_id: str,
    body: ShareLinkRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await pockets_service.update_share_link(pocket_id, user_id, body.access)


@router.get("/shared/{token}")
async def access_via_share_link(token: str) -> dict:
    return await pockets_service.access_via_share_link(token)


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


@router.post(
    "/{pocket_id}/collaborators",
    status_code=204,
    dependencies=[Depends(require_pocket_owner)],
)
async def add_collaborator(
    pocket_id: str,
    body: AddCollaboratorRequest,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.add_collaborator(pocket_id, user_id, body)
    return Response(status_code=204)


@router.delete(
    "/{pocket_id}/collaborators/{target_user_id}",
    status_code=204,
    dependencies=[Depends(require_pocket_owner)],
)
async def remove_collaborator(
    pocket_id: str,
    target_user_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await pockets_service.remove_collaborator(pocket_id, user_id, target_user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Sessions under pocket
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/sessions")
async def create_pocket_session(
    pocket_id: str,
    body: CreateSessionRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    from pocketpaw_ee.cloud.sessions import service as sessions_service
    from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

    body_with_pocket = CreateSessionRequest(
        title=body.title,
        pocket_id=pocket_id,
        group_id=body.group_id,
        agent_id=body.agent_id,
        session_id=body.session_id,
    )
    ctx = sessions_service.legacy_ctx(user_id, workspace_id)
    session = await sessions_service.create(ctx, workspace_id, body_with_pocket)
    return session_to_wire_dict(session)


@router.get("/{pocket_id}/sessions")
async def list_pocket_sessions(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    from pocketpaw_ee.cloud.sessions import service as sessions_service
    from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

    ctx = sessions_service.legacy_ctx(user_id)
    items = await sessions_service.list_for_pocket(ctx, pocket_id)
    return [session_to_wire_dict(s) for s in items]
