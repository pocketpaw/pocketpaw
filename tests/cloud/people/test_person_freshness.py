# tests/cloud/people/test_person_freshness.py — Fabric Person freshness (T-2).
# Created: 2026-08-05 (feat/coupling-person-freshness).
#
# Locks the contract that the Person is a LIVE projection, not a one-time
# invite snapshot:
#   1. Role change: ``update_member_role`` emits ``workspace.member_role``;
#      the people listener re-materializes → ``get_person`` returns the NEW
#      role. Works both when a Person already exists (fields preserved) and
#      when the member never had one (materialized fresh from profile).
#   2. Profile edit: ``update_profile`` emits ``profile.updated``; the
#      listener refreshes name/avatar in EVERY workspace the user belongs
#      to. A status-only change does not touch the Person.
#   3. Owner: ``workspace.create`` materializes a Person (role=owner,
#      source=membership) for the creator.
#   4. Lazy backfill: ``get_person`` miss for a user who IS a live member
#      materializes on first read; a non-member stays ``None``;
#      ``materialize_missing=False`` disables the backfill.
#
# Spy-don't-mock: the real workspace/auth/people services run against
# mongomock Beanie, events flow over a real InProcessBus with the real
# listeners registered, and the Fabric store is a throwaway journal-backed
# store injected via the people service's ``_default_store`` seam (same
# shape as tests/cloud/workspace/test_service_v2.py's person_store).

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud.auth import service as auth_service
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership as _Membership
from pocketpaw_ee.cloud.people import service as people_service
from pocketpaw_ee.cloud.people.domain import SOURCE_ADMIN_CONTEXT, SOURCE_MEMBERSHIP
from pocketpaw_ee.cloud.people.listeners import register_people_listeners
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.domain import Invite, InviteContext
from pocketpaw_ee.cloud.workspace.dto import CreateWorkspaceRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubConnManager:
    async def send_to_user(self, user_id, payload) -> None:  # noqa: ARG002
        return None


class _StubResolver(AudienceResolver):
    """No WebSocket recipients — only the in-process subscribers matter here."""

    def __init__(self) -> None:
        async def _empty(_):
            return []

        super().__init__(
            group_members=_empty,
            workspace_members=_empty,
            workspace_admins=_empty,
            workspace_peers=_empty,
        )

    async def audience(self, event):  # type: ignore[override]
        return []


@pytest.fixture
def person_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Throwaway journal-backed Fabric store injected as the people default.

    Patching ``_default_store`` covers every internal ``store=None`` call —
    the listeners, the workspace create/accept paths, and the lazy
    backfill — so nothing touches the real org journal at ~/.soul/.
    """
    from soul_protocol.engine.journal import open_journal

    from pocketpaw.fabric.journal_store import FabricJournalStore

    journal = open_journal(tmp_path / "people_journal.db")
    store = FabricJournalStore(journal)
    store.bootstrap()
    monkeypatch.setattr(people_service, "_default_store", lambda: store)
    yield store
    journal.close()


@pytest_asyncio.fixture
async def real_bus():
    """Install a real InProcessBus with the people listeners registered.

    Overrides the autouse RecordingBus from the cloud conftest so emits
    from the workspace/auth services actually dispatch to the subscribers.
    """
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus = InProcessBus(resolver=_StubResolver(), conn_manager=_StubConnManager())
    bus_mod._bus = bus  # type: ignore[attr-defined]
    register_people_listeners()
    try:
        yield bus
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the workspace service's cache resolver (needs init_realtime)."""
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


def _ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_user(
    *,
    email: str = "u@x.c",
    full_name: str = "U",
    avatar: str = "",
    memberships: list[tuple[str, str]] | None = None,
) -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name=full_name,
        avatar=avatar,
        workspaces=[
            _Membership(workspace=w, role=r, joined_at=datetime.now(UTC))
            for (w, r) in (memberships or [])
        ],
    )
    await doc.insert()
    return doc


def _invite(*, workspace_id: str, email: str, role: str = "member") -> Invite:
    return Invite(
        id="inv1",
        workspace_id=workspace_id,
        email=email,
        role=role,
        invited_by="admin1",
        token=None,
        group_id="team-eng",
        accepted=True,
        revoked=False,
        expired=False,
        expires_at=datetime.now(UTC),
        context=InviteContext(focus="Own billing", profile_pic="pic-1"),
    )


# ---------------------------------------------------------------------------
# 1. Role change → get_person shows the new role
# ---------------------------------------------------------------------------


class TestRoleChangeRefresh:
    @pytest.mark.asyncio
    async def test_promote_member_refreshes_person_role(self, person_store, real_bus) -> None:
        """The acceptance case: promote a member → get_person returns admin."""
        owner = await _seed_user(email="owner@x.c", full_name="Owner")
        ws = await workspace_service.create(
            _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
        )
        member = await _seed_user(email="m@x.c", full_name="Mem", memberships=[(ws.id, "member")])
        # Materialize the invite-time snapshot the old one-shot path left.
        await people_service.materialize_person_from_invite(
            workspace_id=ws.id,
            user_id=str(member.id),
            name="Mem",
            email="m@x.c",
            avatar="av-1",
            invite=_invite(workspace_id=ws.id, email="m@x.c", role="member"),
            store=person_store,
        )
        before = await people_service.get_person(ws.id, str(member.id), store=person_store)
        assert before is not None and before.role == "member"

        await workspace_service.update_member_role(ws.id, str(member.id), "admin", str(owner.id))

        after = await people_service.get_person(ws.id, str(member.id), store=person_store)
        assert after is not None
        assert after.role == "admin"
        # Identity + onboarding fields survive the refresh.
        assert after.name == "Mem"
        assert after.focus == "Own billing"
        assert after.invited_by == "admin1"
        assert after.source == SOURCE_ADMIN_CONTEXT

    @pytest.mark.asyncio
    async def test_role_change_materializes_missing_person(self, person_store, real_bus) -> None:
        """A member with NO Person (pre-freshness data) gets one on role change."""
        owner = await _seed_user(email="owner@x.c", full_name="Owner")
        ws = await workspace_service.create(
            _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
        )
        member = await _seed_user(
            email="m@x.c",
            full_name="Mem",
            avatar="av-9",
            memberships=[(ws.id, "member")],
        )

        await workspace_service.update_member_role(ws.id, str(member.id), "admin", str(owner.id))

        person = await people_service.get_person(
            ws.id, str(member.id), store=person_store, materialize_missing=False
        )
        assert person is not None
        assert person.role == "admin"
        assert person.name == "Mem"
        assert person.avatar == "av-9"
        assert person.source == SOURCE_MEMBERSHIP
        assert person.invited_by == ""


# ---------------------------------------------------------------------------
# 2. Profile edit → name/avatar refresh in all memberships
# ---------------------------------------------------------------------------


class TestProfileEditRefresh:
    @pytest.mark.asyncio
    async def test_profile_edit_refreshes_every_workspace(self, person_store, real_bus) -> None:
        user = await _seed_user(
            email="m@x.c",
            full_name="Old Name",
            avatar="old-av",
            memberships=[("wsA", "member"), ("wsB", "admin")],
        )
        uid = str(user.id)
        for ws_id, role in (("wsA", "member"), ("wsB", "admin")):
            await people_service.materialize_person_from_membership(
                workspace_id=ws_id,
                user_id=uid,
                name="Old Name",
                email="m@x.c",
                avatar="old-av",
                role=role,
                store=person_store,
            )

        await auth_service.update_profile(_ctx(uid), full_name="New Name", avatar="new-av")

        for ws_id, role in (("wsA", "member"), ("wsB", "admin")):
            person = await people_service.get_person(
                ws_id, uid, store=person_store, materialize_missing=False
            )
            assert person is not None, ws_id
            assert person.name == "New Name"
            assert person.avatar == "new-av"
            # Role is untouched by a profile refresh.
            assert person.role == role

    @pytest.mark.asyncio
    async def test_profile_edit_backfills_missing_person(self, person_store, real_bus) -> None:
        """A membership with no Person yet is materialized by the refresh."""
        user = await _seed_user(email="m@x.c", full_name="Name", memberships=[("wsA", "member")])
        uid = str(user.id)

        await auth_service.update_profile(_ctx(uid), full_name="Renamed")

        person = await people_service.get_person(
            "wsA", uid, store=person_store, materialize_missing=False
        )
        assert person is not None
        assert person.name == "Renamed"
        assert person.role == "member"
        assert person.source == SOURCE_MEMBERSHIP

    @pytest.mark.asyncio
    async def test_status_only_change_does_not_touch_person(self, person_store, real_bus) -> None:
        user = await _seed_user(email="m@x.c", full_name="Name", memberships=[("wsA", "member")])
        uid = str(user.id)

        await auth_service.update_profile(_ctx(uid), status="away")

        person = await people_service.get_person(
            "wsA", uid, store=person_store, materialize_missing=False
        )
        assert person is None  # listener skipped — no identity field changed

    @pytest.mark.asyncio
    async def test_update_profile_emits_changed_fields(self, recording_bus) -> None:
        """The emit contract the listener keys off: field names, no values."""
        from pocketpaw_ee.cloud._core.realtime.events import ProfileUpdated

        user = await _seed_user(email="m@x.c", full_name="Name")
        uid = str(user.id)

        await auth_service.update_profile(_ctx(uid), full_name="Other", status="dnd")

        profile_events = [e for e in recording_bus.events if isinstance(e, ProfileUpdated)]
        assert len(profile_events) == 1
        assert profile_events[0].data == {
            "user_id": uid,
            "changed": ["full_name", "status"],
        }

    @pytest.mark.asyncio
    async def test_noop_update_emits_nothing(self, recording_bus) -> None:
        from pocketpaw_ee.cloud._core.realtime.events import ProfileUpdated

        user = await _seed_user(email="m@x.c", full_name="Name")
        await auth_service.update_profile(_ctx(str(user.id)), full_name="Name")

        assert not any(isinstance(e, ProfileUpdated) for e in recording_bus.events)


# ---------------------------------------------------------------------------
# 3. Workspace create → owner has a Person
# ---------------------------------------------------------------------------


class TestOwnerPerson:
    @pytest.mark.asyncio
    async def test_create_materializes_owner_person(self, person_store) -> None:
        owner = await _seed_user(email="owner@x.c", full_name="Founder", avatar="f-av")
        ws = await workspace_service.create(
            _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
        )

        person = await people_service.get_person(
            ws.id, str(owner.id), store=person_store, materialize_missing=False
        )
        assert person is not None
        assert person.role == "owner"
        assert person.name == "Founder"
        assert person.email == "owner@x.c"
        assert person.avatar == "f-av"
        assert person.source == SOURCE_MEMBERSHIP
        assert person.invited_by == ""

    @pytest.mark.asyncio
    async def test_create_survives_person_store_failure(self, person_store, monkeypatch) -> None:
        """A Fabric hiccup must never fail workspace creation."""

        async def _boom(**_kwargs):
            raise RuntimeError("journal down")

        monkeypatch.setattr(
            "pocketpaw_ee.cloud.workspace.service.people_service."
            "materialize_person_from_membership",
            _boom,
        )
        owner = await _seed_user(email="owner@x.c", full_name="Founder")
        ws = await workspace_service.create(
            _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
        )
        assert ws.id  # created fine


# ---------------------------------------------------------------------------
# 4. Lazy backfill on get_person miss
# ---------------------------------------------------------------------------


class TestLazyBackfill:
    @pytest.mark.asyncio
    async def test_miss_for_live_member_materializes(self, person_store) -> None:
        """Pre-existing owner/member with no Person converges on first read."""
        user = await _seed_user(
            email="m@x.c",
            full_name="Legacy Owner",
            avatar="lo-av",
            memberships=[("wsA", "owner")],
        )
        uid = str(user.id)

        person = await people_service.get_person("wsA", uid, store=person_store)
        assert person is not None
        assert person.role == "owner"
        assert person.name == "Legacy Owner"
        assert person.source == SOURCE_MEMBERSHIP

        # And it PERSISTED — a second read with the backfill disabled hits it.
        again = await people_service.get_person(
            "wsA", uid, store=person_store, materialize_missing=False
        )
        assert again is not None
        assert again.id == person.id

    @pytest.mark.asyncio
    async def test_miss_for_non_member_stays_none(self, person_store) -> None:
        user = await _seed_user(email="m@x.c", memberships=[("wsA", "member")])
        person = await people_service.get_person("other-ws", str(user.id), store=person_store)
        assert person is None

    @pytest.mark.asyncio
    async def test_materialize_missing_false_disables_backfill(self, person_store) -> None:
        user = await _seed_user(email="m@x.c", memberships=[("wsA", "member")])
        person = await people_service.get_person(
            "wsA", str(user.id), store=person_store, materialize_missing=False
        )
        assert person is None


# ---------------------------------------------------------------------------
# Listener payload guards (defensive skips, no explosion)
# ---------------------------------------------------------------------------


class TestListenerGuards:
    @pytest.mark.asyncio
    async def test_role_listener_skips_malformed_payload(self, person_store) -> None:
        from pocketpaw_ee.cloud._core.realtime.events import WorkspaceMemberRole
        from pocketpaw_ee.cloud.people.listeners import refresh_person_on_role_change

        # Missing user_id — handler returns without touching the store.
        await refresh_person_on_role_change(
            WorkspaceMemberRole(data={"workspace_id": "wsA", "role": "admin"})
        )

    @pytest.mark.asyncio
    async def test_profile_listener_skips_non_identity_change(self, person_store) -> None:
        from pocketpaw_ee.cloud._core.realtime.events import ProfileUpdated
        from pocketpaw_ee.cloud.people.listeners import (
            refresh_person_on_profile_update,
        )

        await refresh_person_on_profile_update(
            ProfileUpdated(data={"user_id": "u1", "changed": ["status"]})
        )

    @pytest.mark.asyncio
    async def test_role_listener_swallows_service_failure(self, person_store, monkeypatch) -> None:
        from pocketpaw_ee.cloud._core.realtime.events import WorkspaceMemberRole
        from pocketpaw_ee.cloud.people.listeners import refresh_person_on_role_change

        async def _boom(*_a, **_k):
            raise RuntimeError("journal down")

        monkeypatch.setattr(people_service, "refresh_person_role", _boom)
        # Must not raise — one broken refresh can't take down the bus chain.
        await refresh_person_on_role_change(
            WorkspaceMemberRole(data={"workspace_id": "wsA", "user_id": "u1", "role": "admin"})
        )
