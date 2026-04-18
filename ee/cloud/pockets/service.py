"""Pockets domain — business logic service.

Changes: Added create_from_ripple_spec() static method to PocketService for
auto-creating pockets from agent-generated ripple specs (moved from agent_bridge.py).
"""

from __future__ import annotations

import logging
import secrets

from beanie import PydanticObjectId

from ee.cloud.models.pocket import Pocket, Widget
from ee.cloud.models.session import Session
from ee.cloud.pockets.schemas import (
    AddCollaboratorRequest,
    AddWidgetRequest,
    CreatePocketRequest,
    UpdatePocketRequest,
    UpdateWidgetRequest,
)
from ee.cloud.ripple_normalizer import normalize_ripple_spec
from ee.cloud.shared.errors import Forbidden, NotFound
from ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)


def _pocket_response(pocket: Pocket) -> dict:
    """Build a frontend-compatible dict from a Pocket document."""
    return {
        "_id": str(pocket.id),
        "workspace": pocket.workspace,
        "name": pocket.name,
        "description": pocket.description,
        "type": pocket.type,
        "icon": pocket.icon,
        "color": pocket.color,
        "owner": pocket.owner,
        "visibility": pocket.visibility,
        "team": pocket.team,
        "agents": pocket.agents,
        "widgets": [w.model_dump(by_alias=True) for w in pocket.widgets],
        "rippleSpec": pocket.rippleSpec,
        "shareLinkToken": pocket.share_link_token,
        "shareLinkAccess": pocket.share_link_access,
        "sharedWith": pocket.shared_with,
        "createdAt": pocket.createdAt.isoformat() if pocket.createdAt else None,
        "updatedAt": pocket.updatedAt.isoformat() if pocket.updatedAt else None,
    }


def _check_owner(pocket: Pocket, user_id: str) -> None:
    """Raise Forbidden if user is not the pocket owner."""
    if pocket.owner != user_id:
        raise Forbidden("pocket.not_owner", "Only the pocket owner can perform this action")


def _check_edit_access(pocket: Pocket, user_id: str) -> None:
    """Raise Forbidden if user has no edit access (owner or shared_with)."""
    if pocket.owner == user_id:
        return
    if user_id in pocket.shared_with:
        return
    if pocket.visibility == "workspace":
        return
    raise Forbidden("pocket.access_denied", "You do not have edit access to this pocket")


async def _get_pocket_or_404(pocket_id: str) -> Pocket:
    """Fetch pocket by ID or raise NotFound."""
    pocket = await Pocket.get(PydanticObjectId(pocket_id))
    if not pocket:
        raise NotFound("pocket", pocket_id)
    return pocket


class PocketService:
    """Stateless service encapsulating pocket business logic."""

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    @staticmethod
    async def create(workspace_id: str, user_id: str, body: CreatePocketRequest) -> dict:
        """Create a pocket with optional agents, widgets, and rippleSpec."""
        # Build initial widgets from request body
        initial_widgets: list[Widget] = []
        for w in body.widgets:
            initial_widgets.append(
                Widget(
                    name=w.get("name", "Widget"),
                    type=w.get("type", "custom"),
                    icon=w.get("icon", ""),
                    color=w.get("color", ""),
                    span=w.get("span", "col-span-1"),
                    dataSourceType=w.get("dataSourceType", w.get("data_source_type", "static")),
                    config=w.get("config", {}),
                    props=w.get("props", {}),
                    data=w.get("data"),
                    assignedAgent=w.get("assignedAgent", w.get("assigned_agent")),
                )
            )

        pocket = Pocket(
            workspace=workspace_id,
            name=body.name,
            description=body.description,
            type=body.type,
            icon=body.icon,
            color=body.color,
            owner=user_id,
            visibility=body.visibility,
            agents=body.agents,
            widgets=initial_widgets,
            rippleSpec=normalize_ripple_spec(body.ripple_spec) if body.ripple_spec else None,
        )
        await pocket.insert()

        # If session_id provided, link the session to this pocket
        if body.session_id:
            session = await Session.find_one(
                Session.sessionId == body.session_id,
                Session.workspace == workspace_id,
            )
            if session:
                session.pocket = str(pocket.id)
                await session.save()

        return _pocket_response(pocket)

    @staticmethod
    async def list_pockets(workspace_id: str, user_id: str) -> list[dict]:
        """List pockets visible to the user.

        Includes: owned by user, shared with user, or workspace-visible.
        """
        pockets = await Pocket.find(
            Pocket.workspace == workspace_id,
            {
                "$or": [
                    {"owner": user_id},
                    {"shared_with": user_id},
                    {"visibility": "workspace"},
                ]
            },
        ).to_list()
        return [_pocket_response(p) for p in pockets]

    @staticmethod
    async def get(pocket_id: str, user_id: str) -> dict:
        """Get a single pocket. Checks access."""
        pocket = await _get_pocket_or_404(pocket_id)

        # Access check: owner, shared_with, or workspace-visible
        if (
            pocket.owner != user_id
            and user_id not in pocket.shared_with
            and pocket.visibility == "private"
        ):
            raise Forbidden("pocket.access_denied", "You do not have access to this pocket")

        return _pocket_response(pocket)

    @staticmethod
    async def update(pocket_id: str, user_id: str, body: UpdatePocketRequest) -> dict:
        """Update pocket fields. Owner or edit-access users."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        if body.name is not None:
            pocket.name = body.name
        if body.description is not None:
            pocket.description = body.description
        if body.type is not None:
            pocket.type = body.type
        if body.icon is not None:
            pocket.icon = body.icon
        if body.color is not None:
            pocket.color = body.color
        if body.visibility is not None:
            _check_owner(pocket, user_id)  # Only owner can change visibility
            pocket.visibility = body.visibility
        if body.ripple_spec is not None:
            pocket.rippleSpec = normalize_ripple_spec(body.ripple_spec)

        await pocket.save()
        return _pocket_response(pocket)

    @staticmethod
    async def delete(pocket_id: str, user_id: str) -> None:
        """Hard-delete a pocket. Owner only."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_owner(pocket, user_id)
        await pocket.delete()

    # -----------------------------------------------------------------
    # Agent-generated pockets
    # -----------------------------------------------------------------

    @staticmethod
    async def create_from_ripple_spec(
        workspace_id: str,
        owner_id: str,
        ripple_spec: dict,
        description: str = "",
    ) -> str | None:
        """Auto-create a pocket from an agent-generated ripple spec.

        Returns the pocket ID on success, None on failure.
        """
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

            pocket = Pocket(
                workspace=workspace_id,
                name=name,
                description=description,
                type="ai-generated",
                owner=owner_id,
                rippleSpec=normalized,
                visibility="workspace",
            )
            await pocket.insert()
            logger.info("Auto-created pocket %s from ripple spec", pocket.id)
            return str(pocket.id)
        except Exception:
            logger.warning("Failed to auto-create pocket from ripple spec", exc_info=True)
            return None

    # -----------------------------------------------------------------
    # Widgets
    # -----------------------------------------------------------------

    @staticmethod
    async def add_widget(pocket_id: str, user_id: str, body: AddWidgetRequest) -> dict:
        """Add a widget to the pocket."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        widget = Widget(
            name=body.name,
            type=body.type,
            icon=body.icon,
            color=body.color,
            span=body.span,
            dataSourceType=body.data_source_type,
            config=body.config,
            props=body.props,
            assignedAgent=body.assigned_agent,
        )
        pocket.widgets.append(widget)
        await pocket.save()
        return _pocket_response(pocket)

    @staticmethod
    async def update_widget(
        pocket_id: str, widget_id: str, user_id: str, body: UpdateWidgetRequest
    ) -> dict:
        """Update a specific widget inside the pocket."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        widget = next((w for w in pocket.widgets if w.id == widget_id), None)
        if not widget:
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

        await pocket.save()
        return _pocket_response(pocket)

    @staticmethod
    async def remove_widget(pocket_id: str, widget_id: str, user_id: str) -> dict:
        """Remove a widget from the pocket."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        original_count = len(pocket.widgets)
        pocket.widgets = [w for w in pocket.widgets if w.id != widget_id]
        if len(pocket.widgets) == original_count:
            raise NotFound("widget", widget_id)

        await pocket.save()
        return _pocket_response(pocket)

    @staticmethod
    async def reorder_widgets(pocket_id: str, user_id: str, widget_ids: list[str]) -> dict:
        """Reorder widgets by the given ordered list of widget IDs."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        widget_map = {w.id: w for w in pocket.widgets}
        reordered: list[Widget] = []
        for wid in widget_ids:
            if wid in widget_map:
                reordered.append(widget_map.pop(wid))
        # Append any widgets not in the reorder list at the end
        reordered.extend(widget_map.values())
        pocket.widgets = reordered

        await pocket.save()
        return _pocket_response(pocket)

    # -----------------------------------------------------------------
    # Sharing — Share links
    # -----------------------------------------------------------------

    @staticmethod
    async def generate_share_link(pocket_id: str, user_id: str, access: str) -> dict:
        """Generate a share link token. Owner only."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_owner(pocket, user_id)

        token = secrets.token_urlsafe(32)
        pocket.share_link_token = token
        pocket.share_link_access = access
        await pocket.save()

        return {"token": token, "access": access, "url": f"/shared/{token}"}

    @staticmethod
    async def revoke_share_link(pocket_id: str, user_id: str) -> None:
        """Revoke the share link. Owner only."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_owner(pocket, user_id)

        pocket.share_link_token = None
        pocket.share_link_access = "view"
        await pocket.save()

    @staticmethod
    async def update_share_link(pocket_id: str, user_id: str, access: str) -> dict:
        """Update the share link access level. Owner only."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_owner(pocket, user_id)

        if not pocket.share_link_token:
            raise NotFound("share_link", pocket_id)

        pocket.share_link_access = access
        await pocket.save()

        return {
            "token": pocket.share_link_token,
            "access": access,
            "url": f"/shared/{pocket.share_link_token}",
        }

    @staticmethod
    async def access_via_share_link(token: str) -> dict:
        """Access a pocket via share link token."""
        pocket = await Pocket.find_one(Pocket.share_link_token == token)
        if not pocket:
            raise NotFound("pocket", "shared link")
        return _pocket_response(pocket)

    # -----------------------------------------------------------------
    # Collaborators
    # -----------------------------------------------------------------

    @staticmethod
    async def add_collaborator(pocket_id: str, user_id: str, body: AddCollaboratorRequest) -> None:
        """Add a collaborator to the pocket. Owner only."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_owner(pocket, user_id)

        if body.user_id not in pocket.shared_with:
            pocket.shared_with.append(body.user_id)
            await pocket.save()

        await event_bus.emit(
            "pocket.shared",
            {
                "pocket_id": str(pocket.id),
                "owner_id": user_id,
                "collaborator_id": body.user_id,
                "access": body.access,
            },
        )

    @staticmethod
    async def remove_collaborator(pocket_id: str, user_id: str, target_user_id: str) -> None:
        """Remove a collaborator from the pocket. Owner only."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_owner(pocket, user_id)

        if target_user_id in pocket.shared_with:
            pocket.shared_with.remove(target_user_id)
            await pocket.save()

    # -----------------------------------------------------------------
    # Team
    # -----------------------------------------------------------------

    @staticmethod
    async def add_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
        """Add a team member to the pocket. Owner or edit access."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        if member_id not in pocket.team:
            pocket.team.append(member_id)
            await pocket.save()

        return _pocket_response(pocket)

    @staticmethod
    async def remove_team_member(pocket_id: str, user_id: str, member_id: str) -> dict:
        """Remove a team member from the pocket. Owner or edit access."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        if member_id in pocket.team:
            pocket.team.remove(member_id)
            await pocket.save()

        return _pocket_response(pocket)

    # -----------------------------------------------------------------
    # Agents
    # -----------------------------------------------------------------

    @staticmethod
    async def add_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
        """Add an agent to the pocket. Owner or edit access."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        if agent_id not in pocket.agents:
            pocket.agents.append(agent_id)
            await pocket.save()

        return _pocket_response(pocket)

    @staticmethod
    async def remove_agent(pocket_id: str, user_id: str, agent_id: str) -> dict:
        """Remove an agent from the pocket. Owner or edit access."""
        pocket = await _get_pocket_or_404(pocket_id)
        _check_edit_access(pocket, user_id)

        if agent_id in pocket.agents:
            pocket.agents.remove(agent_id)
            await pocket.save()

        return _pocket_response(pocket)
