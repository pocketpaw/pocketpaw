"""Pockets domain — FastAPI router."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.cloud.license import require_license
from ee.cloud.pockets.schemas import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    ReorderWidgetsRequest,
    ShareLinkRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
)
from ee.cloud.pockets.service import PocketService
from ee.cloud.sessions.schemas import CreateSessionRequest
from ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(prefix="/pockets", tags=["Pockets"], dependencies=[Depends(require_license)])

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("")
async def create_pocket(
    body: CreatePocketRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.create(workspace_id, user_id, body)


@router.get("")
async def list_pockets(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    return await PocketService.list_pockets(workspace_id, user_id)


@router.get("/{pocket_id}")
async def get_pocket(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.get(pocket_id, user_id)


@router.patch("/{pocket_id}")
async def update_pocket(
    pocket_id: str,
    body: UpdatePocketRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.update(pocket_id, user_id, body)


@router.delete("/{pocket_id}", status_code=204)
async def delete_pocket(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.delete(pocket_id, user_id)
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
    return await PocketService.add_widget(pocket_id, user_id, body)


@router.patch("/{pocket_id}/widgets/{widget_id}")
async def update_widget(
    pocket_id: str,
    widget_id: str,
    body: UpdateWidgetRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.update_widget(pocket_id, widget_id, user_id, body)


@router.delete("/{pocket_id}/widgets/{widget_id}", status_code=204)
async def remove_widget(
    pocket_id: str,
    widget_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.remove_widget(pocket_id, widget_id, user_id)
    return Response(status_code=204)


@router.post("/{pocket_id}/widgets/reorder")
async def reorder_widgets(
    pocket_id: str,
    body: ReorderWidgetsRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.reorder_widgets(pocket_id, user_id, body.widget_ids)


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/team")
async def add_team_member(
    pocket_id: str,
    body: dict,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.add_team_member(pocket_id, user_id, body["member_id"])


@router.delete("/{pocket_id}/team/{member_id}", status_code=204)
async def remove_team_member(
    pocket_id: str,
    member_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.remove_team_member(pocket_id, user_id, member_id)
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
    return await PocketService.add_agent(pocket_id, user_id, agent_id)


@router.delete("/{pocket_id}/agents/{agent_id}", status_code=204)
async def remove_agent(
    pocket_id: str,
    agent_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.remove_agent(pocket_id, user_id, agent_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Sharing — Share links
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/share")
async def generate_share_link(
    pocket_id: str,
    body: ShareLinkRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.generate_share_link(pocket_id, user_id, body.access)


@router.delete("/{pocket_id}/share", status_code=204)
async def revoke_share_link(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.revoke_share_link(pocket_id, user_id)
    return Response(status_code=204)


@router.patch("/{pocket_id}/share")
async def update_share_link_access(
    pocket_id: str,
    body: ShareLinkRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await PocketService.update_share_link(pocket_id, user_id, body.access)


@router.get("/shared/{token}")
async def access_via_share_link(token: str) -> dict:
    return await PocketService.access_via_share_link(token)


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


@router.post("/{pocket_id}/collaborators", status_code=204)
async def add_collaborator(
    pocket_id: str,
    body: AddCollaboratorRequest,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.add_collaborator(pocket_id, user_id, body)
    return Response(status_code=204)


@router.delete("/{pocket_id}/collaborators/{target_user_id}", status_code=204)
async def remove_collaborator(
    pocket_id: str,
    target_user_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await PocketService.remove_collaborator(pocket_id, user_id, target_user_id)
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
    from ee.cloud.sessions.service import SessionService

    return await SessionService.create_for_pocket(workspace_id, user_id, pocket_id, body)


@router.get("/{pocket_id}/sessions")
async def list_pocket_sessions(
    pocket_id: str,
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    from ee.cloud.sessions.service import SessionService

    return await SessionService.list_for_pocket(pocket_id, user_id)
