"""Tests for the workspace service.

Uses the shared ``mongo_db`` fixture (mongomock-motor) so service
functions exercise real Beanie writes against an isolated in-memory DB.
Asserts on:
- Domain operations (CRUD + members + invites)
- Event emission via recording_bus + monkey-patched event_bus
- Cache invalidation via get_resolver().invalidate_workspace
- Cross-module side effects (Notification creation on invite-to-existing-user)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from beanie import PydanticObjectId
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import (
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
)
from pocketpaw_ee.cloud._core.realtime.events import (
    WorkspaceDeleted,
    WorkspaceInviteAccepted,
    WorkspaceInviteCreated,
    WorkspaceInviteRevoked,
    WorkspaceMemberAdded,
    WorkspaceMemberRemoved,
    WorkspaceMemberRole,
    WorkspaceUpdated,
)
from pocketpaw_ee.cloud.models.invite import Invite as _InviteDoc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import (
    BulkInviteRequest,
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user(*, email: str = "u@x.c", full_name: str = "U") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name=full_name,
        workspaces=[],
    )
    await doc.insert()
    return doc


def _ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def captured_legacy_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    class _FakeBus:
        async def emit(self, name: str, payload: dict) -> None:
            events.append((name, payload))

    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.event_bus", _FakeBus())
    return events


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the resolver so get_resolver() doesn't explode (real one
    needs init_realtime which we don't run in unit tests)."""
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest.fixture
async def owner() -> _UserDoc:
    return await _seed_user(email="owner@x.c", full_name="Owner")


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


async def test_create_emits_member_added_and_invalidates_cache(
    owner, recording_bus, resolver_mock
) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )
    assert ws.name == "Acme"
    assert ws.member_count == 1
    assert any(isinstance(e, WorkspaceMemberAdded) for e in recording_bus.events)
    resolver_mock.invalidate_workspace.assert_called_once_with(ws.id)


async def test_create_rejects_duplicate_slug(owner) -> None:
    await workspace_service.create(_ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a"))
    with pytest.raises(ConflictError):
        await workspace_service.create(
            _ctx(str(owner.id)), CreateWorkspaceRequest(name="A2", slug="a")
        )


async def test_update_emits_workspace_updated(owner, recording_bus) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    recording_bus.events.clear()
    await workspace_service.update(_ctx(str(owner.id)), ws.id, UpdateWorkspaceRequest(name="B"))
    assert any(isinstance(e, WorkspaceUpdated) for e in recording_bus.events)


async def test_delete_cascades_and_emits(owner, recording_bus, resolver_mock) -> None:
    other = await _seed_user(email="other@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    # add `other` as a member
    await workspace_service._add_member(ws.id, str(other.id), role="member")

    recording_bus.events.clear()
    await workspace_service.delete(_ctx(str(owner.id)), ws.id)

    assert any(isinstance(e, WorkspaceDeleted) for e in recording_bus.events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)
    # cascade: other's membership is gone
    refreshed = await _UserDoc.get(other.id)
    assert refreshed is not None
    assert all(m.workspace != ws.id for m in refreshed.workspaces)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def test_update_member_role_blocks_demoting_owner(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    with pytest.raises(Forbidden) as exc:
        await workspace_service.update_member_role(ws.id, str(owner.id), "member", str(owner.id))
    assert exc.value.code == "workspace.cannot_demote_owner"


async def test_update_member_role_emits_role_event(owner, recording_bus, resolver_mock) -> None:
    other = await _seed_user(email="other@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(other.id), role="member")

    recording_bus.events.clear()
    resolver_mock.invalidate_workspace.reset_mock()
    await workspace_service.update_member_role(ws.id, str(other.id), "admin", str(owner.id))
    assert any(isinstance(e, WorkspaceMemberRole) for e in recording_bus.events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)


async def test_remove_member_blocks_owner(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    with pytest.raises(Forbidden) as exc:
        await workspace_service.remove_member(ws.id, str(owner.id), str(owner.id))
    assert exc.value.code == "workspace.cannot_remove_owner"


async def test_remove_member_emits_both_paths(
    owner, recording_bus, captured_legacy_events, resolver_mock
) -> None:
    other = await _seed_user(email="other@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(other.id), role="member")

    recording_bus.events.clear()
    captured_legacy_events.clear()
    resolver_mock.invalidate_workspace.reset_mock()

    await workspace_service.remove_member(ws.id, str(other.id), str(owner.id))
    assert any(isinstance(e, WorkspaceMemberRemoved) for e in recording_bus.events)
    assert any(name == "member.removed" for (name, _) in captured_legacy_events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)


async def test_update_member_role_blocks_demoting_last_owner(owner) -> None:
    """Role-based last-owner check fires even when target is not doc.owner.

    Seeds a NON-doc-owner user, promotes them to owner-role, then strips the
    original doc.owner's owner role so the new user is the SOLE owner. The
    doc.owner-field check would not catch this; the new _count_owners guard
    must.
    """
    sole_owner = await _seed_user(email="sole@x.c", full_name="Sole")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(sole_owner.id), role="owner")
    # Demote the doc.owner directly via the User doc so we bypass the
    # cannot_demote_owner guard; this leaves sole_owner as the only owner.
    owner_doc = await _UserDoc.get(owner.id)
    assert owner_doc is not None
    for m in owner_doc.workspaces:
        if m.workspace == ws.id:
            m.role = "member"
    await owner_doc.save()

    with pytest.raises(Forbidden) as exc:
        await workspace_service.update_member_role(
            ws.id, str(sole_owner.id), "admin", str(sole_owner.id)
        )
    assert exc.value.code == "workspace.last_owner"


async def test_update_member_role_allows_demoting_one_of_many_owners(
    owner, recording_bus, resolver_mock
) -> None:
    co_owner = await _seed_user(email="co@x.c", full_name="Co")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(co_owner.id), role="owner")

    # Demoting co_owner (not doc.owner) when there are two owners — allowed.
    await workspace_service.update_member_role(ws.id, str(co_owner.id), "admin", str(owner.id))

    role = await workspace_service._get_member_role(ws.id, str(co_owner.id))
    assert role == "admin"


async def test_remove_member_blocks_removing_last_owner(owner) -> None:
    sole_owner = await _seed_user(email="sole@x.c", full_name="Sole")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(sole_owner.id), role="owner")
    # Strip doc.owner's owner role so sole_owner is the only owner.
    owner_doc = await _UserDoc.get(owner.id)
    assert owner_doc is not None
    for m in owner_doc.workspaces:
        if m.workspace == ws.id:
            m.role = "member"
    await owner_doc.save()

    with pytest.raises(Forbidden) as exc:
        await workspace_service.remove_member(ws.id, str(sole_owner.id), str(sole_owner.id))
    assert exc.value.code == "workspace.last_owner"


async def test_remove_member_allows_removing_one_of_many_owners(
    owner, recording_bus, captured_legacy_events, resolver_mock
) -> None:
    co_owner = await _seed_user(email="co@x.c", full_name="Co")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service._add_member(ws.id, str(co_owner.id), role="owner")

    # Removing one owner when another remains — allowed.
    await workspace_service.remove_member(ws.id, str(co_owner.id), str(owner.id))

    role = await workspace_service._get_member_role(ws.id, str(co_owner.id))
    assert role is None


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


async def test_create_invite_seat_limit(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    # Saturate seats
    for i in range(ws.seats):
        u = await _seed_user(email=f"seat{i}@x.c")
        await workspace_service._add_member(ws.id, str(u.id), role="member")

    with pytest.raises(SeatLimitError):
        await workspace_service.create_invite(
            _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
        )


async def test_create_invite_rejects_duplicate_pending(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    with pytest.raises(ConflictError) as exc:
        await workspace_service.create_invite(
            _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
        )
    assert exc.value.code == "invite.already_pending"


async def test_create_invite_emits_invite_created(owner, recording_bus, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    recording_bus.events.clear()
    await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    assert any(isinstance(e, WorkspaceInviteCreated) for e in recording_bus.events)


async def test_create_invite_to_existing_user_includes_user_id_and_notifies(
    owner, recording_bus, monkeypatch
) -> None:
    notifications: list[dict[str, Any]] = []

    async def fake_notify(**kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", fake_notify
    )
    invitee = await _seed_user(email="invitee@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    recording_bus.events.clear()
    await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="invitee@x.c")
    )

    created = next(e for e in recording_bus.events if isinstance(e, WorkspaceInviteCreated))
    assert created.data.get("user_id") == str(invitee.id)
    assert len(notifications) == 1
    assert notifications[0]["recipient"] == str(invitee.id)


async def test_accept_invite_adds_member_and_emits(
    owner, recording_bus, resolver_mock, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    invitee = await _seed_user(email="invitee@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="invitee@x.c")
    )

    recording_bus.events.clear()
    resolver_mock.invalidate_workspace.reset_mock()
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)

    refreshed = await _UserDoc.get(invitee.id)
    assert refreshed is not None
    assert any(m.workspace == ws.id and m.role == invite.role for m in refreshed.workspaces)
    assert any(isinstance(e, WorkspaceInviteAccepted) for e in recording_bus.events)
    assert any(isinstance(e, WorkspaceMemberAdded) for e in recording_bus.events)
    resolver_mock.invalidate_workspace.assert_called_with(ws.id)


async def test_accept_invite_rejects_revoked(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite_doc = _InviteDoc(
        workspace=ws.id,
        email="x@y.z",
        role="member",
        invited_by=str(owner.id),
        token="tok-revoked",
        revoked=True,
    )
    await invite_doc.insert()

    invitee = await _seed_user(email="x@y.z")
    with pytest.raises(Forbidden) as exc:
        await workspace_service.accept_invite(_ctx(str(invitee.id)), "tok-revoked")
    assert exc.value.code == "invite.revoked"


async def test_revoke_invite_emits_invite_revoked(owner, recording_bus, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    recording_bus.events.clear()
    await workspace_service.revoke_invite(ws.id, invite.id)
    assert any(isinstance(e, WorkspaceInviteRevoked) for e in recording_bus.events)


async def test_validate_invite_returns_workspace_name(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    inv, ws_name = await workspace_service.validate_invite(invite.token)
    assert inv.id == invite.id
    assert ws_name == "Acme"


async def test_validate_invite_unknown_token_raises_not_found() -> None:
    with pytest.raises(NotFound):
        await workspace_service.validate_invite("nope")


async def _async_noop(*_args, **_kwargs):
    return None


async def test_create_invite_hashes_token_at_rest(
    mongo_db: Any, captured_legacy_events, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    owner = await _seed_user(email="o@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="W", slug="w"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="invitee@x.c", role="member"),
    )

    # The returned domain object carries the plaintext token (the
    # only place it lives outside the email URL).
    assert invite.token and len(invite.token) >= 32

    # The DB row stores the HASH, not the plaintext.
    from pocketpaw_ee.cloud.models.invite import Invite as _D
    from pocketpaw_ee.cloud.models.invite import hash_token

    row = await _D.find_one(_D.token_hash == hash_token(invite.token))
    assert row is not None
    assert row.token in (None, "")  # plaintext column is not populated for new invites


async def test_validate_invite_accepts_plaintext_by_hash(
    mongo_db: Any, captured_legacy_events, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    owner = await _seed_user(email="o2@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="W2", slug="w2"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="i2@x.c", role="member"),
    )

    looked_up, ws_name = await workspace_service.validate_invite(invite.token)
    assert looked_up.email == "i2@x.c"
    assert ws_name == "W2"


async def test_validate_invite_unknown_hash_404(mongo_db: Any) -> None:
    with pytest.raises(NotFound):
        await workspace_service.validate_invite("definitely-not-a-real-token")


async def test_accept_invite_is_single_use(mongo_db: Any, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    owner = await _seed_user(email="own@x.c")
    invitee = await _seed_user(email="inv@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="SU", slug="su"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="inv@x.c", role="member"),
    )
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)
    with pytest.raises(ConflictError):
        await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)


async def test_accept_invite_concurrent_only_one_wins(mongo_db: Any, monkeypatch) -> None:
    """Two concurrent accepts on the same token: exactly one succeeds."""
    import asyncio

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    owner = await _seed_user(email="own2@x.c")
    inv = await _seed_user(email="i@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="CC", slug="cc"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="i@x.c", role="member"),
    )
    results = await asyncio.gather(
        workspace_service.accept_invite(_ctx(str(inv.id)), invite.token),
        workspace_service.accept_invite(_ctx(str(inv.id)), invite.token),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, ConflictError)]
    assert len(successes) == 1
    assert len(failures) == 1


async def test_accept_invite_rejects_email_mismatch(mongo_db: Any, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    owner = await _seed_user(email="own3@x.c")
    invitee = await _seed_user(email="invitee@x.c")
    impostor = await _seed_user(email="impostor@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="EM", slug="em"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="invitee@x.c", role="admin"),
    )
    with pytest.raises(Forbidden, match="email"):
        await workspace_service.accept_invite(_ctx(str(impostor.id)), invite.token)

    # Invite is still usable by the real invitee — the rejected claim
    # must NOT have consumed it.
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)


async def test_accept_invite_case_insensitive_email(mongo_db: Any, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    owner = await _seed_user(email="own4@x.c")
    invitee = await _seed_user(email="Mixed@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="CI", slug="ci"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="mixed@x.c", role="member"),
    )
    # Should succeed — email comparison is case-insensitive.
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)


# ---------------------------------------------------------------------------
# preview_invite — typed state for the accept UI
# ---------------------------------------------------------------------------


async def test_preview_invite_states(mongo_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _async_noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )

    owner = await _seed_user(email="po@x.c")
    matching_viewer = await _seed_user(email="pm@x.c")
    other_viewer = await _seed_user(email="potherviewer@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="P", slug="pview"),
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="pm@x.c", role="member"),
    )

    # Anonymous viewer -> ready_new
    out = await workspace_service.preview_invite(invite.token, viewer_user_id=None)
    assert out["state"] == "ready_new"
    assert out["email"] == "pm@x.c"
    assert out["role"] == "member"
    assert out["workspace_name"] == "P"
    assert out["viewer_email"] is None
    assert out["group_name"] is None

    # Signed in as matching user -> ready_existing
    out = await workspace_service.preview_invite(
        invite.token, viewer_user_id=str(matching_viewer.id)
    )
    assert out["state"] == "ready_existing"
    assert out["viewer_email"] == "pm@x.c"

    # Signed in as wrong user -> ready_wrong_user
    out = await workspace_service.preview_invite(invite.token, viewer_user_id=str(other_viewer.id))
    assert out["state"] == "ready_wrong_user"
    assert out["viewer_email"] == "potherviewer@x.c"

    # Unknown token -> not_found
    out = await workspace_service.preview_invite("not-a-real-token", viewer_user_id=None)
    assert out["state"] == "not_found"


async def test_preview_invite_revoked_expired_accepted(
    mongo_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _async_noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )

    owner = await _seed_user(email="prv@x.c")
    ws = await workspace_service.create(
        _ctx(str(owner.id)),
        CreateWorkspaceRequest(name="PRV", slug="prv"),
    )

    # revoked
    revoked_invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="rev@x.c", role="member"),
    )
    await workspace_service.revoke_invite(ws.id, revoked_invite.id)
    out = await workspace_service.preview_invite(revoked_invite.token, viewer_user_id=None)
    assert out["state"] == "revoked"
    assert out["email"] == "rev@x.c"

    # expired — mutate the underlying doc directly
    expired_invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="exp@x.c", role="member"),
    )
    exp_doc = await _InviteDoc.get(PydanticObjectId(expired_invite.id))
    assert exp_doc is not None
    # Past, but inside the 14-day TTL grace so mongomock doesn't reap the
    # row before preview_invite can read it.
    exp_doc.expires_at = datetime.now(UTC) - timedelta(days=1)
    await exp_doc.save()
    out = await workspace_service.preview_invite(expired_invite.token, viewer_user_id=None)
    assert out["state"] == "expired"
    assert out["email"] == "exp@x.c"

    # already_accepted — actually accept it with the matching user
    accepted_invite = await workspace_service.create_invite(
        _ctx(str(owner.id)),
        ws.id,
        CreateInviteRequest(email="acc@x.c", role="member"),
    )
    acceptor = await _seed_user(email="acc@x.c")
    await workspace_service.accept_invite(_ctx(str(acceptor.id)), accepted_invite.token)
    out = await workspace_service.preview_invite(accepted_invite.token, viewer_user_id=None)
    assert out["state"] == "already_accepted"
    assert out["email"] == "acc@x.c"


async def test_create_invite_cleans_up_expired_rows(owner, monkeypatch) -> None:
    """A previously-expired invite must not block a fresh invite to the same email."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    first = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="dup@x.c")
    )
    # Push expires_at into the past but inside the 14-day TTL grace, so
    # mongomock's TTL enforcer doesn't purge the row before we get to it.
    doc = await _InviteDoc.get(PydanticObjectId(first.id))
    assert doc is not None
    doc.expires_at = datetime.now(UTC) - timedelta(days=1)
    await doc.save()

    # Should not collide; the dead row gets cleaned up first.
    second = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="dup@x.c")
    )
    assert second.id != first.id
    # Old expired row is gone.
    assert await _InviteDoc.get(PydanticObjectId(first.id)) is None


async def test_create_invite_cleans_up_revoked_rows(owner, monkeypatch) -> None:
    """A revoked invite must not block a fresh invite to the same (email, group)."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    first = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="dup@x.c")
    )
    await workspace_service.revoke_invite(ws.id, first.id)

    second = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="dup@x.c")
    )
    assert second.id != first.id
    # Old revoked row is gone.
    assert await _InviteDoc.get(PydanticObjectId(first.id)) is None


# ---------------------------------------------------------------------------
# Wave 2 Task 3: decline_invite
# ---------------------------------------------------------------------------


async def test_decline_invite_marks_revoked_with_reason(owner, recording_bus, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="d@x.c")
    )
    recording_bus.events.clear()

    await workspace_service.decline_invite(invite.token)

    row = await _InviteDoc.get(PydanticObjectId(invite.id))
    assert row is not None
    assert row.revoked is True
    assert row.revoked_reason == "declined"
    assert row.accepted is False

    revoked_events = [e for e in recording_bus.events if isinstance(e, WorkspaceInviteRevoked)]
    assert len(revoked_events) == 1
    assert revoked_events[0].data.get("reason") == "declined"
    assert revoked_events[0].data.get("workspace_id") == ws.id
    assert revoked_events[0].data.get("invite_id") == invite.id


async def test_decline_invite_404_unknown_token() -> None:
    with pytest.raises(NotFound):
        await workspace_service.decline_invite("not-a-real-token")


async def test_decline_invite_409_already_accepted(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="acc@x.c")
    )
    invitee = await _seed_user(email="acc@x.c")
    await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)

    with pytest.raises(ConflictError) as exc:
        await workspace_service.decline_invite(invite.token)
    assert exc.value.code == "invite.already_accepted"


async def test_decline_invite_atomic_against_accept(owner, monkeypatch) -> None:
    """After decline, accept must surface the revoked-branch in accept_invite."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    invite = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="race@x.c")
    )

    await workspace_service.decline_invite(invite.token)

    invitee = await _seed_user(email="race@x.c")
    with pytest.raises(Forbidden) as exc:
        await workspace_service.accept_invite(_ctx(str(invitee.id)), invite.token)
    assert exc.value.code == "invite.revoked"


# ---------------------------------------------------------------------------
# Bulk invites
# ---------------------------------------------------------------------------


async def test_bulk_create_invites_happy_path_3_emails(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    result = await workspace_service.bulk_create_invites(
        _ctx(str(owner.id)),
        ws.id,
        BulkInviteRequest(emails=["a@x.c", "b@x.c", "c@x.c"]),
    )
    assert len(result["created"]) == 3
    assert result["skipped"] == []
    # Each minted invite carries a plaintext token.
    assert all(inv.token and len(inv.token) >= 32 for inv in result["created"])


async def test_bulk_create_invites_skips_existing_members(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    # Seed an existing member of the workspace.
    member = await _seed_user(email="member@x.c")
    await workspace_service._add_member(ws.id, str(member.id), role="member")

    result = await workspace_service.bulk_create_invites(
        _ctx(str(owner.id)),
        ws.id,
        BulkInviteRequest(emails=["new@x.c", "member@x.c"]),
    )
    assert len(result["created"]) == 1
    assert result["created"][0].email == "new@x.c"
    assert result["skipped"] == [{"email": "member@x.c", "reason": "already_member"}]


async def test_bulk_create_invites_skips_pending_dupes(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="dup@x.c")
    )

    result = await workspace_service.bulk_create_invites(
        _ctx(str(owner.id)),
        ws.id,
        BulkInviteRequest(emails=["dup@x.c", "fresh@x.c"]),
    )
    assert len(result["created"]) == 1
    assert result["created"][0].email == "fresh@x.c"
    assert result["skipped"] == [{"email": "dup@x.c", "reason": "already_pending"}]


async def test_bulk_create_invites_rejects_over_seats(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    # Shrink seats to 5 and pre-fill 3 extra members → current_count=4.
    ws_doc = await workspace_service._WorkspaceDoc.get(PydanticObjectId(ws.id))
    assert ws_doc is not None
    ws_doc.seats = 5
    await ws_doc.save()
    for i in range(3):
        u = await _seed_user(email=f"seat{i}@x.c")
        await workspace_service._add_member(ws.id, str(u.id), role="member")

    invite_count_before = await _InviteDoc.find({"workspace": ws.id}).count()
    with pytest.raises(SeatLimitError):
        await workspace_service.bulk_create_invites(
            _ctx(str(owner.id)),
            ws.id,
            BulkInviteRequest(emails=["a@x.c", "b@x.c", "c@x.c"]),
        )
    # No partial writes: the invite collection is untouched.
    invite_count_after = await _InviteDoc.find({"workspace": ws.id}).count()
    assert invite_count_after == invite_count_before


async def test_bulk_create_invites_max_100_emails() -> None:
    # 100 is the cap — exactly 100 validates.
    ok = BulkInviteRequest(emails=[f"u{i}@x.c" for i in range(100)])
    assert len(ok.emails) == 100
    # 101 trips the pydantic max_length validator.
    with pytest.raises(Exception):  # pydantic.ValidationError
        BulkInviteRequest(emails=[f"u{i}@x.c" for i in range(101)])
    # Empty list also rejected (min_length=1).
    with pytest.raises(Exception):
        BulkInviteRequest(emails=[])


# ---------------------------------------------------------------------------
# get_workspace_plan — fail-closed semantics (Wave 2 Task 8)
# ---------------------------------------------------------------------------


async def test_get_workspace_plan_returns_real_plan(owner) -> None:
    """Happy path: an existing workspace returns its plan tier verbatim."""
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="W", slug="w-plan")
    )
    # Upgrade plan directly on the doc.
    ws_doc = await workspace_service._WorkspaceDoc.get(PydanticObjectId(ws.id))
    assert ws_doc is not None
    ws_doc.plan = "enterprise"
    await ws_doc.save()

    plan = await workspace_service.get_workspace_plan(ws.id)
    assert plan == "enterprise"


async def test_get_workspace_plan_returns_none_for_missing_workspace() -> None:
    """A genuinely missing workspace returns None — caller decides 404."""
    # Valid ObjectId shape, but no doc with that id.
    missing_id = str(PydanticObjectId())
    plan = await workspace_service.get_workspace_plan(missing_id)
    assert plan is None


async def test_get_workspace_plan_returns_none_for_invalid_id() -> None:
    """A malformed id is treated as 'doesn't exist', not propagated."""
    plan = await workspace_service.get_workspace_plan("not-an-objectid")
    assert plan is None


async def test_get_workspace_plan_returns_none_for_soft_deleted(owner) -> None:
    """Soft-deleted workspaces are indistinguishable from missing here."""
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="W", slug="w-del")
    )
    ws_doc = await workspace_service._WorkspaceDoc.get(PydanticObjectId(ws.id))
    assert ws_doc is not None
    ws_doc.deleted_at = datetime.now(UTC)
    await ws_doc.save()

    plan = await workspace_service.get_workspace_plan(ws.id)
    assert plan is None


async def test_get_workspace_plan_reraises_on_db_error(
    owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient DB failures must propagate, not silently downgrade
    enterprise customers to the most restrictive plan."""
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="W", slug="w-flap")
    )

    class _Boom(Exception):
        pass

    async def _raise(*_a: Any, **_k: Any) -> None:
        raise _Boom("simulated mongo outage")

    monkeypatch.setattr(workspace_service._WorkspaceDoc, "get", _raise)
    with pytest.raises(_Boom):
        await workspace_service.get_workspace_plan(ws.id)


# ---------------------------------------------------------------------------
# resend_invite (Wave 2 Task 16)
# ---------------------------------------------------------------------------


async def test_resend_invite_returns_new_plaintext(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    original = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )

    result = await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)

    assert result["invite_id"] == original.id
    new_plaintext = result["token"]
    assert isinstance(new_plaintext, str) and len(new_plaintext) >= 32
    assert new_plaintext != original.token

    from pocketpaw_ee.cloud.models.invite import hash_token

    row = await _InviteDoc.get(PydanticObjectId(original.id))
    assert row is not None
    assert row.token_hash == hash_token(new_plaintext)
    assert row.token in (None, "")


async def test_resend_invite_resets_expires_at(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    original = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    row = await _InviteDoc.get(PydanticObjectId(original.id))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await row.save()

    await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)

    refreshed = await _InviteDoc.get(PydanticObjectId(original.id))
    assert refreshed is not None
    exp = refreshed.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    delta = exp - datetime.now(UTC)
    # Should be very close to 7 days; allow a wide window for slow CI.
    assert timedelta(days=6, hours=23) <= delta <= timedelta(days=7, minutes=1)


async def test_resend_invite_increments_count(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    original = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )

    await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)
    await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)

    refreshed = await _InviteDoc.get(PydanticObjectId(original.id))
    assert refreshed is not None
    assert refreshed.resend_count == 2


async def test_resend_invite_emits_audit_row(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    original = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )

    await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)

    from pocketpaw_ee.cloud.models.audit_event import AuditEvent as _AuditEventDoc

    rows = await _AuditEventDoc.find(
        {"workspace": ws.id, "action": "workspace.invite_resent"}
    ).to_list()
    assert len(rows) == 1
    assert rows[0].metadata["invite_id"] == original.id
    assert rows[0].metadata["email"] == "x@y.z"
    assert rows[0].metadata["resend_count"] == 1


async def test_resend_invite_rejects_accepted_invite(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    original = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    invitee = await _seed_user(email="x@y.z")
    await workspace_service.accept_invite(_ctx(str(invitee.id)), original.token)

    with pytest.raises(ConflictError) as exc:
        await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)
    assert exc.value.code == "invite.already_accepted"


async def test_resend_invite_rejects_revoked_invite(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    original = await workspace_service.create_invite(
        _ctx(str(owner.id)), ws.id, CreateInviteRequest(email="x@y.z")
    )
    await workspace_service.revoke_invite(ws.id, original.id, str(owner.id))

    with pytest.raises(ConflictError) as exc:
        await workspace_service.resend_invite(_ctx(str(owner.id)), ws.id, original.id)
    assert exc.value.code == "invite.revoked"


# ---------------------------------------------------------------------------
# Delete preview (Wave 2 Task 19)
# ---------------------------------------------------------------------------


async def test_delete_preview_counts_resources(owner, monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.notifications_service.create", _async_noop
    )
    from pocketpaw_ee.cloud.models.agent import Agent as _AgentDoc
    from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
    from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUploadDoc

    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )

    # 2 additional members → 3 total including owner.
    for i in range(2):
        u = await _seed_user(email=f"m{i}@x.c", full_name=f"M{i}")
        await workspace_service._add_member(ws.id, str(u.id), role="member")

    # 2 rooms.
    for i in range(2):
        await _GroupDoc(workspace=ws.id, name=f"g{i}", owner=str(owner.id)).insert()

    # 1 agent.
    await _AgentDoc(workspace=ws.id, name="bot", slug="bot", owner=str(owner.id)).insert()

    # 4 file uploads, 256 bytes each → 1024 total.
    for i in range(4):
        await _FileUploadDoc(
            file_id=f"f{i}",
            storage_key=f"key/{i}",
            filename=f"f{i}.txt",
            mime="text/plain",
            size=256,
            workspace=ws.id,
            owner=str(owner.id),
        ).insert()

    # 5 pending invites.
    for i in range(5):
        await workspace_service.create_invite(
            _ctx(str(owner.id)), ws.id, CreateInviteRequest(email=f"inv{i}@x.c")
        )

    preview = await workspace_service.get_delete_preview(ws.id)

    assert preview == {
        "member_count": 3,
        "room_count": 2,
        "agent_count": 1,
        "file_count": 4,
        "invite_count": 5,
        "total_bytes": 1024,
    }


async def test_delete_preview_zero_resources(owner) -> None:
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )
    preview = await workspace_service.get_delete_preview(ws.id)
    # Owner counts as a member; everything else is zero.
    assert preview["member_count"] == 1
    assert preview["room_count"] == 0
    assert preview["agent_count"] == 0
    assert preview["file_count"] == 0
    assert preview["invite_count"] == 0
    assert preview["total_bytes"] == 0


async def test_delete_preview_workspace_not_found() -> None:
    bogus = str(PydanticObjectId())
    with pytest.raises(NotFound):
        await workspace_service.get_delete_preview(bogus)
