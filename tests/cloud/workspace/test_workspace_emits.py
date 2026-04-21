"""Tests that WorkspaceService emits realtime events via the bus.

Each mutating WorkspaceService method must fire the appropriate Event
class through ``emit()`` and, when membership / admin list shifts,
invalidate the AudienceResolver workspace cache. We patch DB/permission
primitives at their seams so we exercise emit behavior in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.cloud.realtime.events import (
    WorkspaceDeleted,
    WorkspaceInviteAccepted,
    WorkspaceInviteCreated,
    WorkspaceInviteRevoked,
    WorkspaceMemberAdded,
    WorkspaceMemberRemoved,
    WorkspaceMemberRole,
    WorkspaceUpdated,
)


def _capture_emits():
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


def _make_user(user_id: str = "u1", email: str = "u1@example.com"):
    u = SimpleNamespace()
    u.id = user_id
    u.email = email
    u.workspaces = []
    u.active_workspace = None
    u.save = AsyncMock()
    return u


def _make_workspace(
    *,
    ws_id: str = "w1",
    owner: str = "u1",
    name: str = "W",
    slug: str = "w",
    seats: int = 10,
    deleted: bool = False,
):
    ws = SimpleNamespace()
    ws.id = ws_id
    ws.name = name
    ws.slug = slug
    ws.owner = owner
    ws.plan = "free"
    ws.seats = seats
    ws.createdAt = None
    ws.deleted_at = None if not deleted else object()
    ws.settings = None
    ws.save = AsyncMock()
    ws.insert = AsyncMock()
    return ws


def _make_membership(workspace: str, role: str = "member"):
    return SimpleNamespace(workspace=workspace, role=role, joined_at=None)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_emits_member_added_and_invalidates_cache():
    from ee.cloud.workspace.schemas import CreateWorkspaceRequest
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    resolver_mock = MagicMock()

    constructed: list = []

    def fake_workspace_ctor(*args, **kwargs):
        ws = _make_workspace(ws_id="w_new", owner=kwargs.get("owner", "u1"))
        ws.name = kwargs.get("name", "W")
        ws.slug = kwargs.get("slug", "w")
        constructed.append(ws)
        return ws

    workspace_stub = MagicMock(side_effect=fake_workspace_ctor)
    workspace_stub.find_one = AsyncMock(return_value=None)
    workspace_stub.slug = MagicMock()
    workspace_stub.deleted_at = MagicMock()

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch("ee.cloud.workspace.service.get_resolver", lambda: resolver_mock),
        patch("ee.cloud.workspace.service.Workspace", new=workspace_stub),
    ):
        await WorkspaceService.create(user, CreateWorkspaceRequest(name="Test", slug="test"))

    added = [e for e in recorded if isinstance(e, WorkspaceMemberAdded)]
    assert len(added) == 1
    assert added[0].data == {"workspace_id": "w_new", "user_id": "u1", "role": "owner"}
    resolver_mock.invalidate_workspace.assert_called_once_with("w_new")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_emits_workspace_updated():
    from ee.cloud.workspace.schemas import UpdateWorkspaceRequest
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace()

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.update("w1", user, UpdateWorkspaceRequest(name="NewName"))

    events = [e for e in recorded if isinstance(e, WorkspaceUpdated)]
    assert len(events) == 1
    assert events[0].data["workspace_id"] == "w1"
    assert events[0].data["name"] == "NewName"


@pytest.mark.asyncio
async def test_update_only_sends_patched_fields():
    from ee.cloud.workspace.schemas import UpdateWorkspaceRequest
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace()

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.update("w1", user, UpdateWorkspaceRequest())

    events = [e for e in recorded if isinstance(e, WorkspaceUpdated)]
    assert len(events) == 1
    # Body was empty — only workspace_id present
    assert events[0].data == {"workspace_id": "w1"}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_emits_workspace_deleted_and_invalidates_cache():
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace()
    resolver_mock = MagicMock()

    user_cls = MagicMock()
    # No other members — the cascade loop is a no-op.
    user_cls.find = MagicMock(return_value=SimpleNamespace(to_list=AsyncMock(return_value=[])))

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch("ee.cloud.workspace.service.get_resolver", lambda: resolver_mock),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch("ee.cloud.workspace.service.User", new=user_cls),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.delete("w1", user)

    events = [e for e in recorded if isinstance(e, WorkspaceDeleted)]
    assert len(events) == 1
    assert events[0].data == {"workspace_id": "w1"}
    resolver_mock.invalidate_workspace.assert_called_once_with("w1")


@pytest.mark.asyncio
async def test_delete_cascades_membership_cleanup():
    """Delete must strip the workspace out of every member + clear
    active_workspace on users who had the deleted workspace selected."""
    from ee.cloud.workspace.service import WorkspaceService

    _, fake_emit = _capture_emits()
    owner = _make_user("u1")
    ws = _make_workspace(owner="u1")
    resolver_mock = MagicMock()

    # Two other members, both with the deleted workspace as active.
    member_a = _make_user("u2")
    member_a.workspaces = [
        _make_membership("w1", role="member"),
        _make_membership("w2", role="member"),
    ]
    member_a.active_workspace = "w1"

    member_b = _make_user("u3")
    member_b.workspaces = [_make_membership("w1", role="admin")]
    member_b.active_workspace = "w1"

    user_cls = MagicMock()
    user_cls.find = MagicMock(
        return_value=SimpleNamespace(to_list=AsyncMock(return_value=[member_a, member_b]))
    )

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch("ee.cloud.workspace.service.get_resolver", lambda: resolver_mock),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch("ee.cloud.workspace.service.User", new=user_cls),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.delete("w1", owner)

    # Member A had another workspace — active should shift, membership dropped.
    assert [m.workspace for m in member_a.workspaces] == ["w2"]
    assert member_a.active_workspace == "w2"
    member_a.save.assert_called_once()

    # Member B had only the deleted workspace — active resets to None so the
    # first-run modal fires on their next login.
    assert member_b.workspaces == []
    assert member_b.active_workspace is None
    member_b.save.assert_called_once()


# ---------------------------------------------------------------------------
# update_member_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_member_role_emits_and_invalidates_cache():
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace(owner="u1")
    target = _make_user("u2")
    target.workspaces = [_make_membership("w1", role="member")]
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch("ee.cloud.workspace.service.get_resolver", lambda: resolver_mock),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service.User.get",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.update_member_role("w1", "u2", "admin", user)

    events = [e for e in recorded if isinstance(e, WorkspaceMemberRole)]
    assert len(events) == 1
    assert events[0].data == {"workspace_id": "w1", "user_id": "u2", "role": "admin"}
    resolver_mock.invalidate_workspace.assert_called_once_with("w1")


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_member_emits_and_invalidates_cache():
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace(owner="u1")
    target = _make_user("u2")
    target.workspaces = [_make_membership("w1", role="member")]
    resolver_mock = MagicMock()

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch("ee.cloud.workspace.service.get_resolver", lambda: resolver_mock),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service.User.get",
            new=AsyncMock(return_value=target),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.remove_member("w1", "u2", user)

    events = [e for e in recorded if isinstance(e, WorkspaceMemberRemoved)]
    assert len(events) == 1
    assert events[0].data == {"workspace_id": "w1", "user_id": "u2"}
    resolver_mock.invalidate_workspace.assert_called_once_with("w1")


# ---------------------------------------------------------------------------
# create_invite
# ---------------------------------------------------------------------------


def _make_invite_stub(constructed: list) -> MagicMock:
    def fake_invite_ctor(*args, **kwargs):
        inv = SimpleNamespace(
            id="inv_new",
            workspace=kwargs.get("workspace"),
            email=kwargs.get("email"),
            role=kwargs.get("role"),
            invited_by=kwargs.get("invited_by"),
            token=kwargs.get("token"),
            group=kwargs.get("group"),
            accepted=False,
            revoked=False,
            expired=False,
            expires_at=None,
        )
        inv.insert = AsyncMock()
        constructed.append(inv)
        return inv

    invite_stub = MagicMock(side_effect=fake_invite_ctor)
    invite_stub.find_one = AsyncMock(return_value=None)
    return invite_stub


@pytest.mark.asyncio
async def test_create_invite_emits_invite_created_without_token():
    from ee.cloud.workspace.schemas import CreateInviteRequest
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace(seats=10)
    invite_stub = _make_invite_stub([])
    user_stub = MagicMock()
    user_stub.email = MagicMock()
    user_stub.find_one = AsyncMock(return_value=None)

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch("ee.cloud.workspace.service.Invite", new=invite_stub),
        patch("ee.cloud.workspace.service.User", new=user_stub),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.create_invite(
            "w1",
            user,
            CreateInviteRequest(email="invitee@example.com", role="member"),
        )

    events = [e for e in recorded if isinstance(e, WorkspaceInviteCreated)]
    assert len(events) == 1
    data = events[0].data
    assert data["workspace_id"] == "w1"
    assert data["invite_id"] == "inv_new"
    assert data["email"] == "invitee@example.com"
    # token MUST NOT leak into the event payload
    assert "token" not in data
    # Unknown invitee -> no user_id to route the event through the resolver's
    # invitee branch.
    assert "user_id" not in data


@pytest.mark.asyncio
async def test_create_invite_includes_user_id_when_invitee_is_known_user():
    """When the invitee already has an account, emit user_id so the resolver
    routes invite.created to them (not just workspace admins)."""
    from ee.cloud.workspace.schemas import CreateInviteRequest
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")
    ws = _make_workspace(seats=10)
    existing_invitee = _make_user("u99", email="invitee@example.com")
    invite_stub = _make_invite_stub([])
    user_stub = MagicMock()
    user_stub.email = MagicMock()
    user_stub.find_one = AsyncMock(return_value=existing_invitee)

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch("ee.cloud.workspace.service.Invite", new=invite_stub),
        patch("ee.cloud.workspace.service.User", new=user_stub),
        patch(
            "ee.cloud.workspace.service.NotificationService.create",
            new=AsyncMock(),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.create_invite(
            "w1",
            user,
            CreateInviteRequest(email="invitee@example.com", role="member"),
        )

    events = [e for e in recorded if isinstance(e, WorkspaceInviteCreated)]
    assert len(events) == 1
    data = events[0].data
    assert data["user_id"] == "u99"
    assert "token" not in data


# ---------------------------------------------------------------------------
# accept_invite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_invite_emits_accepted_and_member_added_and_invalidates_cache():
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u2")
    ws = _make_workspace(ws_id="w1", owner="u1", seats=10)
    resolver_mock = MagicMock()

    invite = SimpleNamespace(
        id="inv1",
        workspace="w1",
        email="u2@example.com",
        role="admin",
        invited_by="u1",
        token="tok",
        group=None,
        accepted=False,
        revoked=False,
        expired=False,
        expires_at=None,
    )
    invite.save = AsyncMock()

    invite_stub = MagicMock()
    invite_stub.find_one = AsyncMock(return_value=invite)
    invite_stub.token = MagicMock()  # allow `Invite.token == token` arg construction

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch("ee.cloud.workspace.service.get_resolver", lambda: resolver_mock),
        patch("ee.cloud.workspace.service.Invite", new=invite_stub),
        patch(
            "ee.cloud.workspace.service.Workspace.get",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ee.cloud.workspace.service._count_members",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.accept_invite("tok", user)

    accepted = [e for e in recorded if isinstance(e, WorkspaceInviteAccepted)]
    added = [e for e in recorded if isinstance(e, WorkspaceMemberAdded)]
    assert len(accepted) == 1
    assert accepted[0].data == {
        "workspace_id": "w1",
        "invite_id": "inv1",
        "user_id": "u2",
    }
    assert len(added) == 1
    assert added[0].data == {"workspace_id": "w1", "user_id": "u2", "role": "admin"}
    resolver_mock.invalidate_workspace.assert_called_once_with("w1")


# ---------------------------------------------------------------------------
# revoke_invite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_invite_emits_invite_revoked():
    from ee.cloud.workspace.service import WorkspaceService

    recorded, fake_emit = _capture_emits()
    user = _make_user("u1")

    invite = SimpleNamespace(
        id="inv1",
        workspace="w1",
        email="x@example.com",
        role="member",
        invited_by="u1",
        token="tok",
        group=None,
        accepted=False,
        revoked=False,
        expired=False,
        expires_at=None,
    )
    invite.save = AsyncMock()

    with (
        patch("ee.cloud.workspace.service.emit", new=fake_emit),
        patch(
            "ee.cloud.workspace.service.Invite.get",
            new=AsyncMock(return_value=invite),
        ),
        patch(
            "ee.cloud.workspace.service.PydanticObjectId",
            new=lambda x: x,
        ),
    ):
        await WorkspaceService.revoke_invite("w1", "inv1", user)

    events = [e for e in recorded if isinstance(e, WorkspaceInviteRevoked)]
    assert len(events) == 1
    assert events[0].data == {"workspace_id": "w1", "invite_id": "inv1"}
