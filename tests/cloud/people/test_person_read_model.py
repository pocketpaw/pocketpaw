# tests/cloud/people/test_person_read_model.py — CN-6: journal-written Persons
# reach the per-workspace FabricStore read model.
# Created: 2026-10-01 (CN-6 — canonicalization night run). Before CN-6 the
# people service wrote Persons only to the journal, so the Fabric API, the
# agents' Fabric MCP and Ripple (all reading get_fabric_store) never saw them.
# Locks: (1) materialize through the real default store lands the Person in
# THAT workspace's FabricStore and not another's; (2) the MCP fabric_query
# handler returns it for that workspace only; (3) re-materialize updates the
# same row; (4) objects journaled before the wiring are backfilled by an
# explicit sync_read_model() (never by a read); (5) a workspace-authored
# "Person" type is reused, not duplicated or left dangling; (6) archive
# unprojects, and a missed removal heals on sync; (7) a re-scope moves the row;
# (8) a stale upsert never rolls a newer row back.

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.fabric as fabric_mcp  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.people import service as people_service  # noqa: E402
from pocketpaw_ee.cloud.workspace.domain import Invite  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

from pocketpaw import journal_dep, stores  # noqa: E402
from pocketpaw.fabric import read_model  # noqa: E402
from pocketpaw.fabric.journal_store import FabricJournalStore  # noqa: E402
from pocketpaw.fabric.models import FabricQuery  # noqa: E402


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tmp org journal + tmp ~/.pocketpaw, wired through the REAL default store."""
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path / "pocketpaw")
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()
    j = open_journal(tmp_path / "journal.db")
    monkeypatch.setattr(journal_dep, "get_journal", lambda: j)
    read_model.default_journal_store.cache_clear()
    yield j
    read_model.default_journal_store.cache_clear()
    stores.reset_store_caches()
    j.close()


def _invite(workspace_id: str) -> Invite:
    return Invite(
        id="inv1",
        workspace_id=workspace_id,
        email="m@x.c",
        role="member",
        invited_by="admin1",
        token=None,
        group_id=None,
        accepted=True,
        revoked=False,
        expired=False,
        expires_at=datetime.now(UTC),
        context=None,
    )


async def _materialize(ws: str, user: str = "u1", name: str = "Mira") -> None:
    await people_service.materialize_person_from_invite(
        workspace_id=ws, user_id=user, name=name, email="m@x.c", avatar="", invite=_invite(ws)
    )


async def _people(ws: str) -> list:
    res = await stores.get_fabric_store(workspace_id=ws).query(
        FabricQuery(type_name="Person"), workspace_id=ws
    )
    return res.objects


@pytest.mark.asyncio
async def test_materialized_person_visible_in_own_workspace_store_only(journal):
    await _materialize("ws1")

    got = await _people("ws1")
    assert [o.id for o in got] == ["person-ws1-u1"]
    assert got[0].properties["name"] == "Mira"
    assert got[0].type_id == "person"
    assert await _people("ws2") == []
    # The type is listable in the workspace's catalog under the stable id.
    t = await stores.get_fabric_store(workspace_id="ws1").get_type("person")
    assert t is not None and t.name == "Person" and t.workspace_id == "ws1"


@pytest.mark.asyncio
async def test_rematerialize_updates_the_same_row(journal):
    await _materialize("ws1", name="Mira")
    await _materialize("ws1", name="Mira K")

    got = await _people("ws1")
    assert len(got) == 1
    assert got[0].properties["name"] == "Mira K"


@pytest.mark.asyncio
async def test_mcp_fabric_query_returns_person_for_its_workspace(journal):
    await _materialize("ws1")

    tokens = attach_agent_identity(workspace_id="ws1", user_id="u9", session_mongo_id="s1")
    try:
        res = await fabric_mcp._fabric_query_handler({"type_name": "Person"})
    finally:
        detach_agent_identity(tokens)
    assert res.get("is_error") is not True, res

    body = json.loads(res["content"][0]["text"])
    assert [o["id"] for o in body["objects"]] == ["person-ws1-u1"]

    tokens = attach_agent_identity(workspace_id="ws2", user_id="u9", session_mongo_id="s1")
    try:
        res = await fabric_mcp._fabric_query_handler({"type_name": "Person"})
    finally:
        detach_agent_identity(tokens)
    assert json.loads(res["content"][0]["text"])["objects"] == []


@pytest.mark.asyncio
async def test_pre_wiring_journal_objects_are_backfilled(journal):
    # Written the pre-CN-6 way: journal only, no read model.
    legacy = FabricJournalStore(journal)
    legacy.bootstrap()
    await _materialize_into(legacy, "ws1")
    assert await _people("ws1") == []

    # A read through the wired store must NOT trigger the backfill.
    assert await people_service.get_person("ws1", "u1") is not None
    assert await _people("ws1") == []

    # The explicit (startup) backfill does; re-running it is harmless.
    store = read_model.default_journal_store()
    assert await store.sync_read_model() == 1
    assert await store.sync_read_model() == 1
    assert [o.id for o in await _people("ws1")] == ["person-ws1-u1"]


async def _materialize_into(store: FabricJournalStore, ws: str) -> None:
    await people_service.materialize_person_from_invite(
        workspace_id=ws,
        user_id="u1",
        name="Mira",
        email="m@x.c",
        avatar="",
        invite=_invite(ws),
        store=store,
    )


@pytest.mark.asyncio
async def test_workspace_authored_person_type_is_not_duplicated(journal):
    fs = stores.get_fabric_store(workspace_id="ws1")
    authored = await fs.define_type(name="Person", properties=[], workspace_id="ws1")

    await _materialize("ws1")

    types = [t for t in await fs.list_types(workspace_id="ws1") if t.name.lower() == "person"]
    assert [t.id for t in types] == [authored.id]
    got = await _people("ws1")
    assert [o.id for o in got] == ["person-ws1-u1"]
    # Stamped with the workspace's own type id, not a dangling "person".
    assert got[0].type_id == authored.id
    res = await fs.query(FabricQuery(type_id=authored.id), workspace_id="ws1")
    assert [o.id for o in res.objects] == ["person-ws1-u1"]


@pytest.mark.asyncio
async def test_archive_removes_object_from_read_model(journal):
    await _materialize("ws1")
    assert len(await _people("ws1")) == 1
    store = read_model.default_journal_store()
    assert await store.archive("person-ws1-u1", scope=["workspace:ws1"])
    assert await _people("ws1") == []


def test_workspace_ids_from_scope():
    assert read_model.workspace_ids_from_scope(
        ["workspace:a", "org:x", "workspace:b:team:t", "workspace:a"]
    ) == ["a", "b"]
    assert read_model.workspace_ids_from_scope(["org:x"]) == []


@pytest.mark.asyncio
async def test_sync_heals_a_missed_archive(journal):
    await _materialize("ws1")
    # Archive journaled by a store with no read model: the row lingers.
    legacy = FabricJournalStore(journal)
    legacy.bootstrap()
    await legacy.archive("person-ws1-u1", scope=["workspace:ws1"])
    assert len(await _people("ws1")) == 1

    store = read_model.default_journal_store()
    store.bootstrap()  # pick up the out-of-band archive event
    await store.sync_read_model()
    assert await _people("ws1") == []


@pytest.mark.asyncio
async def test_rescope_moves_the_row_between_workspaces(journal):
    await _materialize("ws1")
    store = read_model.default_journal_store()
    await store.update("person-ws1-u1", {"name": "Moved"}, scope=["workspace:ws2"])

    assert await _people("ws1") == []
    moved = await _people("ws2")
    assert [o.properties["name"] for o in moved] == ["Moved"]


@pytest.mark.asyncio
async def test_stale_upsert_does_not_roll_back_a_newer_row(journal):
    await _materialize("ws1", name="New")
    fs = stores.get_fabric_store(workspace_id="ws1")
    current = (await _people("ws1"))[0]
    stale = current.model_copy(
        update={
            "properties": {**current.properties, "name": "Old"},
            "updated_at": datetime(2020, 1, 1, tzinfo=UTC),
        }
    )
    assert await fs.upsert_object(stale, workspace_id="ws1") is False
    assert (await _people("ws1"))[0].properties["name"] == "New"
