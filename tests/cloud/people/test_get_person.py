# tests/cloud/people/test_get_person.py — Fabric Person read (get_person).
# Created: 2026-06-08 (feat/vip-agent-block, pp#1367).
#
# Locks the contract for get_person (the people read side):
#   1. A materialized member round-trips: write via the materializer, read via
#      get_person → a typed Person with the same identity + provenance fields,
#      mapped back from the journal projection's property bag.
#   2. A member with no Person (never materialized) → None, no error. This is
#      the "pre-existing / non-invited user" path the about-block relies on.
#   3. Tenant isolation: get_person scoped to workspace A never sees a Person
#      that lives in workspace B (same user_id), even when both share a store.
#   4. The deterministic id is matched exactly — a different user in the same
#      workspace doesn't return the wrong Person.
#
# Uses a throwaway FabricJournalStore over a tmp journal injected via the
# service's ``store=`` arg — same fixture shape as test_materialize_person.py.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.people.domain import SOURCE_ADMIN_CONTEXT
from pocketpaw_ee.cloud.people.service import get_person, materialize_person_from_invite
from pocketpaw_ee.cloud.workspace.domain import Invite, InviteContext
from soul_protocol.engine.journal import open_journal

from pocketpaw.fabric.journal_store import FabricJournalStore


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def store(journal) -> FabricJournalStore:
    s = FabricJournalStore(journal)
    s.bootstrap()
    return s


def _invite(
    *,
    workspace_id: str = "ws1",
    email: str = "member@x.c",
    role: str = "member",
    invited_by: str = "admin1",
    group_id: str | None = "team-eng",
    context: InviteContext | None = None,
) -> Invite:
    return Invite(
        id="inv1",
        workspace_id=workspace_id,
        email=email,
        role=role,
        invited_by=invited_by,
        token=None,
        group_id=group_id,
        accepted=True,
        revoked=False,
        expired=False,
        expires_at=datetime.now(UTC),
        context=context,
    )


# ---------------------------------------------------------------------------
# 1. Round-trip — materialized member reads back as a typed Person
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_get_person_returns_typed_person_with_all_fields(
        self, store: FabricJournalStore
    ) -> None:
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada Lovelace",
            email="member@x.c",
            avatar="https://cdn/ada.png",
            invite=_invite(
                role="admin",
                invited_by="admin-99",
                group_id="team-eng",
                context=InviteContext(focus="Own the billing rewrite", profile_pic="file-42"),
            ),
            store=store,
        )

        person = await get_person("ws1", "user-7", store=store)

        assert person is not None
        # id comes from the object's own id; workspace_id from the read scope.
        assert person.id == "person-ws1-user-7"
        assert person.workspace_id == "ws1"
        # Identity + role/team + onboarding, mapped back from the property bag.
        assert person.user_id == "user-7"
        assert person.name == "Ada Lovelace"
        assert person.email == "member@x.c"
        assert person.avatar == "https://cdn/ada.png"
        assert person.role == "admin"
        assert person.group == "team-eng"
        assert person.focus == "Own the billing rewrite"
        assert person.profile_pic == "file-42"
        # Provenance survives the round-trip.
        assert person.invited_by == "admin-99"
        assert person.source == SOURCE_ADMIN_CONTEXT

    @pytest.mark.asyncio
    async def test_get_person_no_context_maps_group_none(self, store: FabricJournalStore) -> None:
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="",
            invite=_invite(context=None, group_id=None),
            store=store,
        )

        person = await get_person("ws1", "user-7", store=store)

        assert person is not None
        assert person.group is None  # falsy stored value maps back to None
        assert person.focus == ""
        assert person.profile_pic == ""


# ---------------------------------------------------------------------------
# 2. Absent member — no Person → None, no error
# ---------------------------------------------------------------------------


class TestAbsent:
    @pytest.mark.asyncio
    async def test_unmaterialized_member_returns_none(self, store: FabricJournalStore) -> None:
        # Empty store — this user was never invited / materialized.
        person = await get_person("ws1", "ghost-user", store=store)
        assert person is None

    @pytest.mark.asyncio
    async def test_other_user_in_same_workspace_returns_none(
        self, store: FabricJournalStore
    ) -> None:
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="",
            invite=_invite(),
            store=store,
        )
        # Different user id, same workspace — deterministic id won't match.
        person = await get_person("ws1", "user-OTHER", store=store)
        assert person is None


# ---------------------------------------------------------------------------
# 3. Tenant isolation — workspace A can't read workspace B's Person
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_get_person_is_scoped_to_its_workspace(self, store: FabricJournalStore) -> None:
        # Same user id materialized in two different workspaces.
        await materialize_person_from_invite(
            workspace_id="ws-A",
            user_id="user-7",
            name="Ada in A",
            email="a@x.c",
            avatar="",
            invite=_invite(workspace_id="ws-A"),
            store=store,
        )
        await materialize_person_from_invite(
            workspace_id="ws-B",
            user_id="user-7",
            name="Ada in B",
            email="b@x.c",
            avatar="",
            invite=_invite(workspace_id="ws-B"),
            store=store,
        )

        a = await get_person("ws-A", "user-7", store=store)
        b = await get_person("ws-B", "user-7", store=store)

        assert a is not None and a.name == "Ada in A"
        assert a.id == "person-ws-A-user-7"
        assert b is not None and b.name == "Ada in B"
        assert b.id == "person-ws-B-user-7"

    @pytest.mark.asyncio
    async def test_workspace_with_no_people_returns_none(self, store: FabricJournalStore) -> None:
        # A Person exists, but in a different workspace than we query.
        await materialize_person_from_invite(
            workspace_id="ws-A",
            user_id="user-7",
            name="Ada",
            email="a@x.c",
            avatar="",
            invite=_invite(workspace_id="ws-A"),
            store=store,
        )
        # Query a workspace that has no Person for this user.
        person = await get_person("ws-EMPTY", "user-7", store=store)
        assert person is None
