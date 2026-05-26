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

from datetime import UTC, datetime
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
    exp_doc.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
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
