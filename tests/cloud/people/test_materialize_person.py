# tests/cloud/people/test_materialize_person.py — Fabric Person materialization.
# Created: 2026-06-08 (feat/vip-fabric-person, pp#1366).
#
# Locks the contract for materialize_person_from_invite (the people service):
#   1. Accept → a typed Fabric Person exists with the expected fields, drawn
#      from the member's profile + the invite's admin context.
#   2. Provenance — invited_by + source=admin_context, recorded both in the
#      property bag and as the journal Actor / FabricObject.source_*.
#   3. Idempotent — a second materialize for the same member UPDATES the
#      existing Person (no duplicate row); changed context is reflected.
#   4. No-context invite — still creates a Person from the identity fields,
#      with focus / profile_pic empty.
#
# Uses a throwaway FabricJournalStore over a tmp journal (same fixture shape
# as tests/ee/test_fabric_journal.py) injected via the service's ``store=``
# arg — no real org journal, no Mongo needed for these unit-level checks.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.people.domain import (
    PERSON_TYPE_ID,
    PERSON_TYPE_NAME,
    SOURCE_ADMIN_CONTEXT,
)
from pocketpaw_ee.cloud.people.service import materialize_person_from_invite
from pocketpaw_ee.cloud.workspace.domain import Invite, InviteContext
from soul_protocol.engine.journal import open_journal

from pocketpaw.fabric.events import ACTION_OBJECT_CREATED, ACTION_OBJECT_UPDATED
from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricQuery


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


async def _all_people(store: FabricJournalStore) -> list:
    result = await store.query(
        FabricQuery(type_id=PERSON_TYPE_ID, limit=10_000),
        requester_scopes=None,
    )
    return result.objects


# ---------------------------------------------------------------------------
# 1. Accept → Person exists with the expected fields
# ---------------------------------------------------------------------------


class TestMaterializeCreatesPerson:
    @pytest.mark.asyncio
    async def test_person_holds_identity_and_context_fields(
        self, store: FabricJournalStore
    ) -> None:
        invite = _invite(
            context=InviteContext(focus="Own the billing rewrite", profile_pic="file-42"),
        )

        person = await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada Lovelace",
            email="member@x.c",
            avatar="https://cdn/ada.png",
            invite=invite,
            store=store,
        )

        # Returned typed view.
        assert person.id == "person-ws1-user-7"
        assert person.workspace_id == "ws1"
        assert person.user_id == "user-7"

        # Persisted Fabric object.
        people = await _all_people(store)
        assert len(people) == 1
        obj = people[0]
        assert obj.id == "person-ws1-user-7"
        assert obj.type_id == PERSON_TYPE_ID
        assert obj.type_name == PERSON_TYPE_NAME

        props = obj.properties
        # Identity — from the member's own profile.
        assert props["name"] == "Ada Lovelace"
        assert props["email"] == "member@x.c"
        assert props["avatar"] == "https://cdn/ada.png"
        # Role + group + onboarding — from the invite / admin context.
        assert props["role"] == "member"
        assert props["group"] == "team-eng"
        assert props["focus"] == "Own the billing rewrite"
        assert props["profile_pic"] == "file-42"

    @pytest.mark.asyncio
    async def test_person_is_queryable_by_type_name(self, store: FabricJournalStore) -> None:
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="",
            invite=_invite(),
            store=store,
        )

        by_name = await store.query(
            FabricQuery(type_name=PERSON_TYPE_NAME, limit=10),
            requester_scopes=None,
        )
        assert by_name.total == 1
        assert by_name.objects[0].properties["name"] == "Ada"


# ---------------------------------------------------------------------------
# 2. Provenance — invited_by + source=admin_context
# ---------------------------------------------------------------------------


class TestProvenance:
    @pytest.mark.asyncio
    async def test_provenance_in_property_bag_and_object(self, store: FabricJournalStore) -> None:
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="",
            invite=_invite(invited_by="admin-99"),
            store=store,
        )

        obj = (await _all_people(store))[0]
        # Property-bag provenance (readable from the projected object alone).
        assert obj.properties["invited_by"] == "admin-99"
        assert obj.properties["source"] == SOURCE_ADMIN_CONTEXT
        # Native Fabric provenance fields.
        assert obj.source_connector == SOURCE_ADMIN_CONTEXT
        assert obj.source_id == "user-7"

    @pytest.mark.asyncio
    async def test_journal_actor_is_the_inviting_admin(
        self, store: FabricJournalStore, journal
    ) -> None:
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="",
            invite=_invite(invited_by="admin-99"),
            store=store,
        )

        events = journal.query(action=ACTION_OBJECT_CREATED)
        assert len(events) == 1
        assert events[0].actor.kind == "user"
        assert events[0].actor.id == "user:admin-99"
        # Tenancy: the write scope is the workspace, not global.
        assert events[0].scope == ["workspace:ws1"]


# ---------------------------------------------------------------------------
# 3. Idempotent — re-materialize updates, never duplicates
# ---------------------------------------------------------------------------


class TestIdempotent:
    @pytest.mark.asyncio
    async def test_second_call_updates_no_duplicate(self, store: FabricJournalStore) -> None:
        first = _invite(context=InviteContext(focus="Initial focus", profile_pic="pic-1"))
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="av-1",
            invite=first,
            store=store,
        )

        # Re-accept with a changed profile + context.
        second = _invite(
            role="admin",
            context=InviteContext(focus="Revised focus", profile_pic="pic-2"),
        )
        await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada L.",
            email="member@x.c",
            avatar="av-2",
            invite=second,
            store=store,
        )

        people = await _all_people(store)
        assert len(people) == 1  # NOT two — same deterministic id.
        props = people[0].properties
        assert props["name"] == "Ada L."
        assert props["avatar"] == "av-2"
        assert props["role"] == "admin"
        assert props["focus"] == "Revised focus"
        assert props["profile_pic"] == "pic-2"

    @pytest.mark.asyncio
    async def test_second_call_emits_update_event(self, store: FabricJournalStore, journal) -> None:
        for _ in range(2):
            await materialize_person_from_invite(
                workspace_id="ws1",
                user_id="user-7",
                name="Ada",
                email="member@x.c",
                avatar="",
                invite=_invite(),
                store=store,
            )

        created = journal.query(action=ACTION_OBJECT_CREATED)
        updated = journal.query(action=ACTION_OBJECT_UPDATED)
        # Exactly one create, the rest are updates — never two creates.
        assert len(created) == 1
        assert len(updated) == 1


# ---------------------------------------------------------------------------
# 4. No-context invite — still creates a Person, focus/pic empty
# ---------------------------------------------------------------------------


class TestNoContext:
    @pytest.mark.asyncio
    async def test_none_context_creates_person_with_empty_onboarding(
        self, store: FabricJournalStore
    ) -> None:
        invite = _invite(context=None, group_id=None)

        person = await materialize_person_from_invite(
            workspace_id="ws1",
            user_id="user-7",
            name="Ada",
            email="member@x.c",
            avatar="av",
            invite=invite,
            store=store,
        )

        assert person.focus == ""
        assert person.profile_pic == ""

        obj = (await _all_people(store))[0]
        assert obj.properties["name"] == "Ada"
        assert obj.properties["focus"] == ""
        assert obj.properties["profile_pic"] == ""
        assert obj.properties["group"] is None
        # Provenance still recorded even with no context.
        assert obj.properties["source"] == SOURCE_ADMIN_CONTEXT
