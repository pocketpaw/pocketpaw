# tests/cloud/test_paw_bar_conversations.py — the owner inbox's conversation
# STATE row (slice 1). Created 2026-07-30: covers the paw_bar_conversations table,
# the lazy upsert on a visitor turn, and the three owner endpoints that turn the
# concierge log into a queue. Layers:
#   * Migration: a LEGACY paw_bar.db (pre-conversations, and one carrying a
#     stripped-down conversations table) gains the table / the missing columns via
#     the additive _migrate_columns ALTER path — the same trap that bit widgets
#     and decisions.
#   * Upsert: creates on first visitor turn, touches (unread++) after, AUTO-REOPENS
#     a closed / snoozed row, and leaves needs_human alone.
#   * Snooze: an expired snooze reads as open everywhere (row, list filter, counts)
#     with no sweeper; a future one stays snoozed; an open-ended one never expires.
#   * Owner reads: the ?state= filter, the unfiltered counts, and a LEGACY
#     conversation with no row still listing with safe defaults.
#   * PATCH: each field, the note APPEND (never replace), 422 on a bad state /
#     snooze / tag flood, 404 on a ref with no conversation, and lazy row creation
#     on a legacy conversation's first owner action.
#   * Agent-scoped union: two widgets of ONE agent union into one list carrying
#     site_id/site_name, another agent's widget is excluded, an unbound agent gets
#     a 200 with widget_count=0, and a cross-workspace agent id 404s.
#   * Tenancy: every new read and write refuses a cross-workspace caller.
#   * Failure-soft: a store error in the upsert never breaks the visitor's chat.

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import (
    ConversationState,
    DecisionState,
    DecisionStatus,
    PawBarBlock,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_KEY = "site_key_" + "a" * 24
_REF = "cust-0001"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        script_name="",
        signed_key=_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


def _widget(**ov: Any) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=PawBarSpec(
            widget_id="pp_seed",
            pocket_id=str(ov.get("pocket_id", "pocket-1")),
            blocks=[PawBarBlock(type="text", content="Hi")],
        ),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _mk_run(**ov: Any):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    d = dict(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key="cloud:concierge:pocket-1:cust-0001:agent-xyz",
        user_id=_REF,
        agent_id="agent-xyz",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        status="completed",
        partial_text="We open at 8.",
    )
    d.update(ov)
    doc = ChatRunDoc(**d)
    await doc.insert()
    return doc


async def _mk_decision(store: PawBarStore, widget_id: str, **ov: Any) -> DecisionStatus:
    d = dict(
        customer_ref=_REF,
        event_type="paw_bar_action:checkout",
        instinct_action_id="act-" + uuid.uuid4().hex[:8],
        workspace_id="user:maya",
        state=DecisionState.PENDING,
    )
    d.update(ov)
    return await store.create_decision(DecisionStatus(widget_id=widget_id, **d))


def _fake_user(role: str, workspace_id: str = "ws-1", user_id: str = "u1") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


def _build_app(store, monkeypatch, *, role: str = "admin", workspace_id: str = "ws-1"):
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[current_active_user] = lambda: _fake_user(role, workspace_id)
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    return app


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "conv.db")


@pytest_asyncio.fixture
async def client(mongo_db, store, monkeypatch):
    """ADMIN client in ws-1, backed by the tmp paw_bar store + Beanie."""
    app = _build_app(store, monkeypatch, role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


def _future(minutes: int = 60) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).isoformat()


def _past(minutes: int = 60) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------------- #
# Layer 1 — schema migration on a legacy DB
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_conversations_table_is_created_on_a_legacy_db(tmp_path):
    """A paw_bar.db from before this slice gains the table (and keeps the older
    additive ALTERs working) on the next _ensure_schema."""
    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as db:
        # A pre-W4a widgets table: no workspace_id, no agent_id.
        await db.execute(
            "CREATE TABLE paw_bar_widgets (id TEXT PRIMARY KEY, pocket_id TEXT NOT NULL,"
            " owner TEXT NOT NULL, name TEXT DEFAULT '', spec TEXT NOT NULL,"
            " allowed_domains TEXT DEFAULT '[]', access_token TEXT NOT NULL,"
            " rate_limit_per_min INTEGER DEFAULT 60, per_customer_limit_per_min INTEGER DEFAULT 10,"
            " event_mapping TEXT DEFAULT '{}', created_at TEXT, updated_at TEXT)"
        )
        await db.commit()

    st = PawBarStore(db_path)
    conversation = await st.upsert_conversation_on_visitor_turn("w-legacy", _REF, "ws-1")
    assert conversation.state is ConversationState.OPEN

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(paw_bar_conversations)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        async with db.execute("PRAGMA table_info(paw_bar_widgets)") as cur:
            widget_cols = {row[1] for row in await cur.fetchall()}
    assert {"state", "bot_paused", "unread_for_owner", "snooze_until", "tags", "notes"} <= cols
    # The pre-existing ALTER path still ran alongside the new table.
    assert {"workspace_id", "agent_id"} <= widget_cols


@pytest.mark.asyncio
async def test_conversations_columns_are_added_to_a_stale_table(tmp_path):
    """A DEPLOYED conversations table missing later columns is ALTERed additively —
    otherwise the SCHEMA_SQL index over (widget_id, state, updated_at) would fail
    with 'no such column', exactly as it did for widgets and decisions."""
    db_path = tmp_path / "stale.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE paw_bar_conversations (id TEXT PRIMARY KEY, widget_id TEXT NOT NULL,"
            " customer_ref TEXT NOT NULL, UNIQUE (widget_id, customer_ref))"
        )
        await db.commit()

    st = PawBarStore(db_path)
    conversation = await st.upsert_conversation_on_visitor_turn("w-stale", _REF, "ws-1")
    assert conversation.unread_for_owner == 1

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(paw_bar_conversations)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
    assert {
        "workspace_id",
        "state",
        "bot_paused",
        "snooze_until",
        "assignee",
        "tags",
        "notes",
        "contact_email",
        "last_visitor_at",
        "last_owner_at",
        "unread_for_owner",
        "created_at",
        "updated_at",
    } <= cols


# --------------------------------------------------------------------------- #
# Layer 2 — the lazy upsert on a visitor turn
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_upsert_creates_then_touches(store):
    first = await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    assert first.state is ConversationState.OPEN
    assert first.unread_for_owner == 1
    assert first.last_visitor_at

    second = await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    assert second.id == first.id, "the same conversation, not a second row"
    assert second.unread_for_owner == 2
    assert second.last_visitor_at >= first.last_visitor_at


@pytest.mark.parametrize("filed", ["closed", "snoozed"])
@pytest.mark.asyncio
async def test_upsert_auto_reopens_a_filed_conversation(store, filed):
    """The universal inbox behaviour: a visitor who comes back re-opens the thread.
    Its absence reads as a lost customer."""
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    await store.update_conversation(
        "w1", _REF, workspace_id="ws-1", state=filed, snooze_until=_future()
    )

    reopened = await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    assert reopened.state is ConversationState.OPEN
    # A reopened snooze forgets its deadline — it is live again, not merely due.
    assert reopened.snooze_until == ""


@pytest.mark.asyncio
async def test_upsert_leaves_needs_human_alone(store):
    """needs_human is already the top of the queue — 'reopening' it is a demotion."""
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    await store.update_conversation("w1", _REF, workspace_id="ws-1", state="needs_human")

    after = await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    assert after.state is ConversationState.NEEDS_HUMAN
    assert after.unread_for_owner == 2, "the unread counter still advances"


@pytest.mark.asyncio
async def test_upsert_keeps_visitors_separate(store):
    await store.upsert_conversation_on_visitor_turn("w1", "cust-a", "ws-1")
    await store.upsert_conversation_on_visitor_turn("w1", "cust-b", "ws-1")
    await store.upsert_conversation_on_visitor_turn("w1", "cust-b", "ws-1")

    a = await store.get_conversation("w1", "cust-a", "ws-1")
    b = await store.get_conversation("w1", "cust-b", "ws-1")
    assert (a.unread_for_owner, b.unread_for_owner) == (1, 2)


# --------------------------------------------------------------------------- #
# Layer 3 — snooze expiry is computed on READ (no sweeper)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_expired_snooze_reads_as_open_everywhere(store):
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    await store.update_conversation(
        "w1", _REF, workspace_id="ws-1", state="snoozed", snooze_until=_past()
    )

    row = await store.get_conversation("w1", _REF, "ws-1")
    assert row.state is ConversationState.OPEN
    assert row.snooze_until, "the lapsed deadline is kept, so the UI can explain it"
    assert await store.conversation_counts("w1", "ws-1") == {
        "open": 1,
        "needs_human": 0,
        "snoozed": 0,
        "closed": 0,
    }
    assert [c.customer_ref for c in await store.list_conversations("w1", "ws-1", state="open")] == [
        _REF
    ]
    assert await store.list_conversations("w1", "ws-1", state="snoozed") == []


@pytest.mark.asyncio
async def test_future_snooze_stays_snoozed(store):
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    await store.update_conversation(
        "w1", _REF, workspace_id="ws-1", state="snoozed", snooze_until=_future()
    )

    assert (await store.get_conversation("w1", _REF, "ws-1")).state is ConversationState.SNOOZED
    assert (await store.conversation_counts("w1", "ws-1"))["snoozed"] == 1


@pytest.mark.asyncio
async def test_open_ended_snooze_never_expires(store):
    """No deadline means 'until I say so' — an empty snooze_until must not read as
    an already-lapsed one."""
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    await store.update_conversation("w1", _REF, workspace_id="ws-1", state="snoozed")

    assert (await store.get_conversation("w1", _REF, "ws-1")).state is ConversationState.SNOOZED


# --------------------------------------------------------------------------- #
# Layer 4 — store-level tenancy
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_store_reads_and_writes_refuse_another_workspace(store):
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")

    assert await store.get_conversation("w1", _REF, "ws-other") is None
    assert await store.list_conversations("w1", "ws-other") == []
    assert await store.conversation_counts("w1", "ws-other") == {
        "open": 0,
        "needs_human": 0,
        "snoozed": 0,
        "closed": 0,
    }
    assert (
        await store.update_conversation("w1", _REF, workspace_id="ws-other", state="closed") is None
    )
    # The real owner's row is untouched by the refused write.
    assert (await store.get_conversation("w1", _REF, "ws-1")).state is ConversationState.OPEN


@pytest.mark.asyncio
async def test_update_appends_notes_and_replaces_tags(store):
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    await store.update_conversation(
        "w1", _REF, workspace_id="ws-1", note={"author": "u1", "text": "first"}, tags=["vip"]
    )
    row = await store.update_conversation(
        "w1", _REF, workspace_id="ws-1", note={"author": "u1", "text": "second"}, tags=["lead"]
    )

    assert [n.text for n in row.notes] == ["first", "second"]
    assert row.tags == ["lead"]
    assert all(n.at for n in row.notes), "every note is timestamped for the thread order"


@pytest.mark.asyncio
async def test_update_ignores_unknown_fields(store):
    """An unknown key is dropped, never interpolated into the UPDATE."""
    await store.upsert_conversation_on_visitor_turn("w1", _REF, "ws-1")
    before = await store.get_conversation("w1", _REF, "ws-1")
    row = await store.update_conversation(
        "w1", _REF, workspace_id="ws-1", id="hijacked", created_at="1999-01-01", nonsense=1
    )
    assert (row.id, row.customer_ref, row.widget_id) == (before.id, _REF, "w1")
    assert row.created_at == before.created_at


@pytest.mark.asyncio
async def test_ensure_conversation_does_not_count_as_visitor_activity(store):
    """The owner opening a legacy conversation mints its row WITHOUT faking an
    unread message or a visitor timestamp."""
    row = await store.ensure_conversation("w1", _REF, "ws-1")
    assert (row.unread_for_owner, row.last_visitor_at) == (0, "")
    assert (await store.ensure_conversation("w1", _REF, "ws-1")).id == row.id


# --------------------------------------------------------------------------- #
# Layer 5 — the owner list read (join, filter, counts, legacy defaults)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_joins_the_state_row(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.update_conversation(
        widget.id, _REF, workspace_id="ws-1", state="needs_human", tags=["vip"], bot_paused=True
    )
    await _mk_decision(store, widget.id, state=DecisionState.PENDING)

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["customer_ref"] == _REF
    assert item["preview"] == "We open at 8."
    assert item["state"] == "needs_human"
    assert item["bot_paused"] is True
    assert item["unread_for_owner"] == 1
    assert item["tags"] == ["vip"]
    assert item["has_pending_action"] is True
    assert item["display_name"] == f"visitor-{_REF[:6]}"
    assert body["counts"] == {"open": 0, "needs_human": 1, "snoozed": 0, "closed": 0}


@pytest.mark.asyncio
async def test_list_renders_a_legacy_conversation_with_defaults(client):
    """The backfill-free contract: a conversation from before the table still
    lists, reading as a plain open conversation with nothing pending."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run()

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()
    item = body["items"][0]
    assert (item["state"], item["bot_paused"], item["unread_for_owner"]) == ("open", False, 0)
    assert (item["tags"], item["snooze_until"], item["contact_email"]) == ([], "", "")
    assert item["has_pending_action"] is False
    # Counts report only rows that EXIST — a legacy conversation is never invented
    # into a total, so the chips can sum to less than the listed rows.
    assert body["counts"] == {"open": 0, "needs_human": 0, "snoozed": 0, "closed": 0}


@pytest.mark.asyncio
async def test_list_state_filter(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run(user_id="cust-open")
    await _mk_run(user_id="cust-closed")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-open", "ws-1")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-closed", "ws-1")
    await store.update_conversation(widget.id, "cust-closed", workspace_id="ws-1", state="closed")

    listed = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations?state=closed")).json()
    assert [i["customer_ref"] for i in listed["items"]] == ["cust-closed"]
    # The chips stay UNFILTERED while a filter is applied.
    assert listed["counts"] == {"open": 1, "needs_human": 0, "snoozed": 0, "closed": 1}

    opened = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations?state=open")).json()
    assert [i["customer_ref"] for i in opened["items"]] == ["cust-open"]

    bad = await c.get(f"/paw-bar/admin/site/{site.id}/conversations?state=banana")
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_list_shows_the_captured_email_as_the_display_name(client):
    """A visitor who left an email during a decision capture is named by it — the
    email is READ from the decision row, never copied onto the conversation."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await _mk_decision(store, widget.id, contact_email="ada@brewco.com")

    item = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()["items"][0]
    assert item["contact_email"] == "ada@brewco.com"
    assert item["display_name"] == "ada@brewco.com"


@pytest.mark.asyncio
async def test_list_is_cross_workspace_refused(client):
    c, store = client
    site = await _site(workspace="ws-other")
    await store.create_widget(_widget(workspace_id="ws-other"))
    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_list_state_rows_do_not_cross_sites(client):
    """A sibling site's conversation row never joins onto this site's list."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    other = await store.create_widget(_widget(pocket_id="pocket-2"))
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(other.id, _REF, "ws-1")
    await store.update_conversation(other.id, _REF, workspace_id="ws-1", state="closed")

    item = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()["items"][0]
    assert item["state"] == "open", "the sibling widget's closed row must not leak"
    assert widget.id != other.id


# --------------------------------------------------------------------------- #
# Layer 6 — the PATCH
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_patch_each_field(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    url = f"/paw-bar/admin/site/{site.id}/conversations/{_REF}"
    snooze = _future()
    res = await c.patch(
        url,
        json={
            "state": "snoozed",
            "snooze_until": snooze,
            "tags": ["vip", "billing"],
            "bot_paused": True,
        },
    )
    assert res.status_code == 200, res.text
    row = res.json()["conversation"]
    assert res.json()["ok"] is True
    assert row["state"] == "snoozed"
    assert row["snooze_until"] == snooze
    assert row["tags"] == ["vip", "billing"]
    assert row["bot_paused"] is True
    assert row["customer_ref"] == _REF and row["widget_id"] == widget.id
    assert row["unread_for_owner"] == 1, "filing a conversation is not reading it"


@pytest.mark.asyncio
async def test_patch_note_appends_and_is_attributed_to_the_caller(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    url = f"/paw-bar/admin/site/{site.id}/conversations/{_REF}"
    await c.patch(url, json={"note": "called them back"})
    row = (await c.patch(url, json={"note": "left a voicemail", "state": "closed"})).json()[
        "conversation"
    ]

    assert [n["text"] for n in row["notes"]] == ["called them back", "left a voicemail"]
    assert {n["author"] for n in row["notes"]} == {"u1"}
    assert row["state"] == "closed"


@pytest.mark.asyncio
async def test_patch_echo_names_the_visitor_the_same_way_the_list_does(client):
    """A client re-rendering a row from the PATCH echo must not watch a named
    visitor turn back into an anonymous handle."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await _mk_decision(store, widget.id, contact_email="ada@brewco.com")

    listed = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()["items"][0]
    patched = (
        await c.patch(
            f"/paw-bar/admin/site/{site.id}/conversations/{_REF}", json={"state": "closed"}
        )
    ).json()["conversation"]

    assert patched["display_name"] == listed["display_name"] == "ada@brewco.com"
    assert patched["contact_email"] == "ada@brewco.com"


@pytest.mark.asyncio
async def test_patch_only_touches_the_fields_sent(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    url = f"/paw-bar/admin/site/{site.id}/conversations/{_REF}"
    await c.patch(url, json={"tags": ["vip"], "state": "needs_human"})
    row = (await c.patch(url, json={"bot_paused": True})).json()["conversation"]

    assert row["tags"] == ["vip"]
    assert row["state"] == "needs_human"


@pytest.mark.asyncio
async def test_patch_rejects_bad_values(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    url = f"/paw-bar/admin/site/{site.id}/conversations/{_REF}"
    assert (await c.patch(url, json={"state": "escalated"})).status_code == 422
    assert (await c.patch(url, json={"snooze_until": "tomorrow-ish"})).status_code == 422
    assert (await c.patch(url, json={"tags": [f"t{i}" for i in range(40)]})).status_code == 422
    # Nothing was written by any of the refusals.
    assert (await store.get_conversation(widget.id, _REF, "ws-1")).state is ConversationState.OPEN


@pytest.mark.asyncio
async def test_patch_mints_a_row_for_a_legacy_conversation(client):
    """A conversation that predates the table is filed on the owner's first action
    — the row is created lazily rather than 404ing."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    assert await store.get_conversation(widget.id, _REF, "ws-1") is None

    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}", json={"state": "closed"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["conversation"]["state"] == "closed"
    stored = await store.get_conversation(widget.id, _REF, "ws-1")
    assert stored.state is ConversationState.CLOSED
    assert stored.unread_for_owner == 0


@pytest.mark.asyncio
async def test_patch_404s_on_a_ref_with_no_conversation(client):
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/conversations/cust-nobody", json={"state": "closed"}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_patch_rejects_a_malformed_customer_ref(client):
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/conversations/bad ref!", json={"state": "closed"}
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_patch_is_cross_workspace_refused(mongo_db, store, monkeypatch):
    """A ws-2 admin cannot file a ws-1 conversation — the site 404s first."""
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    app = _build_app(store, monkeypatch, role="admin", workspace_id="ws-2")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.patch(
            f"/paw-bar/admin/site/{site.id}/conversations/{_REF}", json={"state": "closed"}
        )
    assert res.status_code == 404
    assert (await store.get_conversation(widget.id, _REF, "ws-1")).state is ConversationState.OPEN


@pytest.mark.asyncio
async def test_patch_requires_an_admin_role(mongo_db, store, monkeypatch):
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    app = _build_app(store, monkeypatch, role="member")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.patch(
            f"/paw-bar/admin/site/{site.id}/conversations/{_REF}", json={"state": "closed"}
        )
    assert res.status_code == 403


# --------------------------------------------------------------------------- #
# Layer 7 — the agent-scoped union
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_conversations_union_two_sites(client, monkeypatch):
    """One agent, two sites → one list, each item naming the site it came from."""
    c, store = client
    site_a = await _site(name="Brew & Co")
    site_b = await _site(pocket_id="pocket-2", name="Roast House", signed_key=_KEY + "b")
    widget_a = await store.create_widget(_widget())
    widget_b = await store.create_widget(_widget(pocket_id="pocket-2"))
    # A DIFFERENT agent's widget on a third site must never appear.
    await _site(pocket_id="pocket-3", name="Someone Else", signed_key=_KEY + "c")
    other = await store.create_widget(_widget(pocket_id="pocket-3", agent_id="agent-other"))

    await _mk_run(user_id="cust-a", scope_id="pocket-1")
    await _mk_run(user_id="cust-b", scope_id="pocket-2")
    await _mk_run(user_id="cust-other", scope_id="pocket-3")
    await store.upsert_conversation_on_visitor_turn(widget_a.id, "cust-a", "ws-1")
    await store.upsert_conversation_on_visitor_turn(widget_b.id, "cust-b", "ws-1")
    await store.upsert_conversation_on_visitor_turn(other.id, "cust-other", "ws-1")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_workspace",
        _fake_get_workspace({"agent-xyz": "ws-1", "agent-other": "ws-1"}),
    )
    body = (await c.get("/paw-bar/admin/agent/agent-xyz/conversations")).json()

    refs = {i["customer_ref"] for i in body["items"]}
    assert refs == {"cust-a", "cust-b"}, "another agent's widget is excluded"
    by_ref = {i["customer_ref"]: i for i in body["items"]}
    assert by_ref["cust-a"]["site_id"] == str(site_a.id)
    assert by_ref["cust-a"]["site_name"] == "Brew & Co"
    assert by_ref["cust-b"]["site_id"] == str(site_b.id)
    assert by_ref["cust-b"]["site_name"] == "Roast House"
    assert body["widget_count"] == 2
    assert {s["site_name"] for s in body["sites"]} == {"Brew & Co", "Roast House"}
    assert body["counts"]["open"] == 2, "counts sum across the union"


def _fake_get_workspace(mapping: dict[str, str]):
    async def _get_workspace(agent_id: str) -> str | None:
        return mapping.get(agent_id)

    return _get_workspace


@pytest.mark.asyncio
async def test_agent_conversations_state_filter(client, monkeypatch):
    c, store = client
    await _site()
    widget = await store.create_widget(_widget())
    await _mk_run(user_id="cust-a")
    await _mk_run(user_id="cust-b")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-a", "ws-1")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-b", "ws-1")
    await store.update_conversation(widget.id, "cust-b", workspace_id="ws-1", state="needs_human")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_workspace",
        _fake_get_workspace({"agent-xyz": "ws-1"}),
    )
    body = (await c.get("/paw-bar/admin/agent/agent-xyz/conversations?state=needs_human")).json()
    assert [i["customer_ref"] for i in body["items"]] == ["cust-b"]
    assert body["counts"] == {"open": 1, "needs_human": 1, "snoozed": 0, "closed": 0}

    assert (
        await c.get("/paw-bar/admin/agent/agent-xyz/conversations?state=nope")
    ).status_code == 422


@pytest.mark.asyncio
async def test_agent_with_no_widget_is_a_200_with_a_zero_binding_signal(client, monkeypatch):
    """An ordinary agent is not an error — it answers 200 with widget_count=0 so a
    client can hide the Conversations tab instead of guessing from emptiness."""
    c, _store = client
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_workspace",
        _fake_get_workspace({"agent-plain": "ws-1"}),
    )
    res = await c.get("/paw-bar/admin/agent/agent-plain/conversations")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"items": [], "counts": {}, "sites": [], "widget_count": 0}


@pytest.mark.asyncio
async def test_agent_conversations_refuse_another_workspace(client, monkeypatch):
    """A ws-2 agent 404s for the ws-1 admin — never leaks that it exists."""
    c, store = client
    await _site(workspace="ws-2", pocket_id="pocket-9")
    widget = await store.create_widget(
        _widget(pocket_id="pocket-9", workspace_id="ws-2", agent_id="agent-ws2")
    )
    await _mk_run(workspace="ws-2", scope_id="pocket-9", user_id="cust-ws2")
    await store.upsert_conversation_on_visitor_turn(widget.id, "cust-ws2", "ws-2")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_workspace",
        _fake_get_workspace({"agent-ws2": "ws-2"}),
    )
    assert (await c.get("/paw-bar/admin/agent/agent-ws2/conversations")).status_code == 404


@pytest.mark.asyncio
async def test_agent_conversations_require_an_admin_role(mongo_db, store, monkeypatch):
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.agents.service.get_workspace",
        _fake_get_workspace({"agent-xyz": "ws-1"}),
    )
    app = _build_app(store, monkeypatch, role="member")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.get("/paw-bar/admin/agent/agent-xyz/conversations")
    assert res.status_code == 403


# --------------------------------------------------------------------------- #
# Layer 8 — the upsert is failure-soft inside concierge_chat
# --------------------------------------------------------------------------- #


_ORIGIN = "https://brewco.com"


class _FakeExecutor:
    """Writes a canned reply to the transport so the SSE tail terminates without a
    live agent run (borrowed from test_paw_bar_concierge_chat)."""

    def __init__(self, transport) -> None:
        self.transport = transport

    async def submit(self, spec) -> None:
        await self.transport.append_event(
            spec.run_id, "chunk", {"content": "We open at 8am!", "type": "text"}
        )
        await self.transport.append_event(
            spec.run_id, "stream_end", {"assistant_message_id": "m1", "cancelled": False}
        )


def _stub_the_run(monkeypatch) -> None:
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

    transport = InMemoryStreamTransport()
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: _FakeExecutor(transport)
    )

    async def _fake_create_run(spec):
        return SimpleNamespace(run_id=spec.run_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)


async def _chat(c, widget_id: str, customer_ref: str = _REF):
    return await c.post(
        "/paw-bar/chat",
        json={
            "widget_id": widget_id,
            "signed_key": _KEY,
            "customer_ref": customer_ref,
            "message": "What time do you open?",
        },
        headers={"Origin": _ORIGIN},
    )


@pytest.mark.asyncio
async def test_a_visitor_turn_creates_the_conversation_row(client, monkeypatch):
    """The real call site: a live POST /paw-bar/chat mints the state row. This is
    what makes the queue backfill-free — no migration, no separate producer."""
    c, store = client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_the_run(monkeypatch)

    assert (await _chat(c, widget.id)).status_code == 200
    row = await store.get_conversation(widget.id, _REF, "ws-1")
    assert row is not None
    assert (row.state, row.unread_for_owner) == (ConversationState.OPEN, 1)
    assert row.workspace_id == "ws-1", "stamped with the KEY's tenant, not the widget owner"

    assert (await _chat(c, widget.id)).status_code == 200
    assert (await store.get_conversation(widget.id, _REF, "ws-1")).unread_for_owner == 2


@pytest.mark.asyncio
async def test_a_visitor_turn_reopens_a_closed_conversation(client, monkeypatch):
    """End-to-end auto-reopen: the owner closed it, the visitor came back."""
    c, store = client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_the_run(monkeypatch)

    await _chat(c, widget.id)
    await store.update_conversation(widget.id, _REF, workspace_id="ws-1", state="closed")
    await _chat(c, widget.id)

    assert (await store.get_conversation(widget.id, _REF, "ws-1")).state is ConversationState.OPEN


@pytest.mark.asyncio
async def test_upsert_failure_never_breaks_the_visitor_chat(client, monkeypatch):
    """Inbox bookkeeping is the owner's convenience. If the state write throws, the
    visitor still gets their answer — the queue is at worst one message stale."""
    c, store = client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_the_run(monkeypatch)

    async def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "upsert_conversation_on_visitor_turn", _boom)

    res = await _chat(c, widget.id)
    assert res.status_code == 200, res.text
    assert "event: chunk" in res.text
    assert "event: stream_end" in res.text
