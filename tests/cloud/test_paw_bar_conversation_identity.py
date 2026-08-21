# tests/cloud/test_paw_bar_conversation_identity.py — a Paw Bar visitor may hold
# MORE THAN ONE conversation.
#
# Created 2026-08-19 as the reproduction for the reported bug: "multiple sessions
# from paw-bar are treated as a single session by the backend." They were. The
# Paw Bar had no conversation identity at all — every read, write, run
# ``session_key`` and ledger id was keyed on the pair ``(widget_id,
# customer_ref)``:
#
#   * ``store.py`` put ``UNIQUE (widget_id, customer_ref)`` on
#     ``paw_bar_conversations``, so one browser could only ever own ONE row per
#     widget, for the life of that browser's localStorage.
#   * ``router.py`` built ``session_key=f"cloud:concierge:{pocket}:{ref}:{agent}"``
#     — no conversation component, so every turn a visitor ever sent shared one
#     agent session.
#   * ``ledger.conversation_id()`` DERIVED its id as ``f"{widget}:{ref}"`` rather
#     than reporting a real one.
#   * ``Conversation.id`` (``ppc_…``) existed on the model and was vestigial:
#     nothing read it as identity.
#
# The visible harm is the third layer, not the first: because
# ``_load_concierge_history`` scoped its replay to the VISITOR, a visitor who
# started over still had their abandoned thread replayed into the agent's
# context, and the owner's inbox showed one endless conversation instead of the
# several the visitor actually had.
#
# Layers here:
#   * Store: ``open_conversation`` mints a genuinely new row; the visitor's turns
#     land on the ACTIVE one; ``list_conversations_for_visitor`` returns them
#     newest-first; a legacy row (pre-``id``-as-identity) keeps working and
#     becomes that visitor's first conversation.
#   * Router: ``session_key`` carries the conversation, not the visitor; history
#     is scoped to the conversation so a fresh one starts cold; a request that
#     omits ``conversation_id`` still works (cached widget bundles in the wild).
#
# All of these FAIL before the identity fix and pass after it.

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_VALID_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
_REF = "cust-0001"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _widget(**ov) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=PawBarSpec(widget_id="w-1", pocket_id="pocket-1"),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
        rate_limit_per_min=60,
        per_customer_limit_per_min=30,
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _site(**ov):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_VALID_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


class _FakeExecutor:
    """Captures every submitted spec; writes a canned reply so the SSE tail ends."""

    def __init__(self, transport) -> None:
        self.transport = transport
        self.submitted: list = []

    async def submit(self, spec) -> None:
        self.submitted.append(spec)
        await self.transport.append_event(
            spec.run_id, "chunk", {"content": "We open at 8am!", "type": "text"}
        )
        await self.transport.append_event(
            spec.run_id, "stream_end", {"assistant_message_id": "m1", "cancelled": False}
        )


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "identity.db")


@pytest_asyncio.fixture
async def chat(tmp_path, mongo_db, store, monkeypatch):
    """A public client for POST /paw-bar/chat backed by a tmp store + Beanie.

    Yields ``(client, store, executor, widget)``. Unlike the sibling suite's
    helper this keeps ONE widget across turns — the whole point here is what
    happens on a visitor's SECOND conversation with the SAME bar.
    """
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)

    transport = InMemoryStreamTransport()
    executor = _FakeExecutor(transport)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: executor)

    async def _fake_create_run(spec):
        return SimpleNamespace(run_id=spec.run_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)

    await _site()
    widget = await store.create_widget(_widget())

    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c, store, executor, widget


async def _seed_run(*, session_key: str, user_text: str, partial_text: str) -> Any:
    """Insert one completed concierge turn — the row conversation memory reads back.

    Written directly rather than dispatched: the ``chat`` fixture stubs
    ``create_run``, so a turn sent through the endpoint leaves no doc behind and a
    history assertion against it would be measuring an empty collection.
    """
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    doc = ChatRunDoc(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key=session_key,
        user_id=_REF,
        agent_id="agent-xyz",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        status="completed",
        user_text=user_text,
        partial_text=partial_text,
    )
    await doc.insert()
    return doc


async def _say(client, widget_id: str, message: str, **ov) -> Any:
    payload = dict(
        widget_id=widget_id,
        signed_key=_VALID_KEY,
        customer_ref=_REF,
        message=message,
    )
    payload.update(ov)
    res = await client.post("/paw-bar/chat", json=payload, headers={"Origin": _ORIGIN})
    assert res.status_code == 200, res.text
    return res


# --------------------------------------------------------------------------- #
# Layer 1 — the store: a visitor may own more than one conversation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_opening_a_conversation_mints_a_genuinely_new_row(store):
    """The reported bug, at its root.

    ``UNIQUE (widget_id, customer_ref)`` meant a visitor's second conversation
    overwrote their first. Starting over must mint a NEW row with its own id,
    leaving the old one intact and readable.
    """
    first = await store.open_conversation("w-1", _REF, workspace_id="ws-1")
    second = await store.open_conversation("w-1", _REF, workspace_id="ws-1")

    assert first.id != second.id, "starting over must mint a new conversation, not reuse the old"
    # Both survive — the first is history, not a row that got clobbered.
    assert await store.get_conversation_by_id(first.id) is not None
    assert await store.get_conversation_by_id(second.id) is not None


@pytest.mark.asyncio
async def test_visitor_turns_land_on_the_active_conversation(store):
    """Two turns inside ONE conversation still share one row — the fix must not
    swing the other way and mint a conversation per message.

    A RETIRED conversation is present on purpose: with only one row in the table
    every read returns it and this passes even unscoped. The retired row is what
    makes "the active one" a claim with teeth.
    """
    retired = await store.open_conversation("w-1", _REF, workspace_id="ws-1")
    current = await store.open_conversation("w-1", _REF, workspace_id="ws-1")

    a = await store.upsert_conversation_on_visitor_turn("w-1", _REF, "ws-1")
    b = await store.upsert_conversation_on_visitor_turn("w-1", _REF, "ws-1")

    assert a.id == b.id == current.id
    assert b.unread_for_owner == 2
    # The finished conversation saw neither turn.
    assert (await store.get_conversation_by_id(retired.id)).unread_for_owner == 0


@pytest.mark.asyncio
async def test_a_retired_conversation_is_not_the_one_in_progress(store):
    """ "The conversation in progress" must mean exactly that.

    Every other read is masked by the fact that the newest conversation is also
    the active one, so an unscoped lookup accidentally agrees. The state that
    separates them is a visitor whose only conversation has been retired — what a
    close path or a TTL sweep leaves behind — and there the answer must be "none
    in progress", not the finished one. The takeover mute reads this: a retired
    row's stale ``bot_paused`` would silence the bot in a conversation nobody is
    holding.
    """
    import aiosqlite

    retired = await store.open_conversation("w-1", _REF, workspace_id="ws-1")
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("UPDATE paw_bar_conversations SET active = 0 WHERE id = ?", (retired.id,))
        await db.commit()

    assert await store.get_conversation("w-1", _REF, workspace_id="ws-1") is None
    # It is still the visitor's history, and still reachable by its own id.
    assert (await store.get_conversation_by_id(retired.id)) is not None


@pytest.mark.asyncio
async def test_visitor_conversations_list_newest_first(store):
    """What the Messages tab reads. Newest-first is the order every messenger in
    this class uses, and the order the list is indexed for."""
    first = await store.open_conversation("w-1", _REF, workspace_id="ws-1")
    second = await store.open_conversation("w-1", _REF, workspace_id="ws-1")

    rows = await store.list_conversations_for_visitor("w-1", _REF, workspace_id="ws-1")

    assert [r.id for r in rows] == [second.id, first.id]
    # A sibling visitor's conversations are never in this list.
    await store.open_conversation("w-1", "cust-other", workspace_id="ws-1")
    rows = await store.list_conversations_for_visitor("w-1", _REF, workspace_id="ws-1")
    assert [r.id for r in rows] == [second.id, first.id]


@pytest.mark.asyncio
async def test_legacy_row_becomes_the_visitors_first_conversation(store):
    """Migration is free: a row written before this change already carries a
    ``ppc_…`` id, so it simply becomes that visitor's first conversation. A
    visitor mid-thread when the fix deploys must not lose it."""
    legacy = await store.upsert_conversation_on_visitor_turn("w-1", _REF, "ws-1")

    rows = await store.list_conversations_for_visitor("w-1", _REF, workspace_id="ws-1")
    assert [r.id for r in rows] == [legacy.id]

    # And the next turn still lands on it rather than starting a stranger.
    again = await store.upsert_conversation_on_visitor_turn("w-1", _REF, "ws-1")
    assert again.id == legacy.id


@pytest.mark.asyncio
async def test_owner_actions_touch_only_the_conversation_in_progress(store):
    """The blast-radius guard.

    ``update_conversation`` is keyed by (widget, visitor), which addressed exactly
    one row while a visitor could only have one. Now that they can have many, the
    same statement would close, snooze, tag or annotate a visitor's ENTIRE history
    at once unless it is scoped to the active row.
    """
    from pocketpaw.paw_bar.models import ConversationState

    retired = await store.open_conversation("w-1", _REF, workspace_id="ws-1")
    current = await store.open_conversation("w-1", _REF, workspace_id="ws-1")

    await store.update_conversation(
        "w-1", _REF, workspace_id="ws-1", state=ConversationState.CLOSED.value
    )

    assert (await store.get_conversation_by_id(current.id)).state is ConversationState.CLOSED
    # The finished conversation is untouched — it is the visitor's record.
    assert (await store.get_conversation_by_id(retired.id)).state is ConversationState.OPEN


# --------------------------------------------------------------------------- #
# Layer 1b — the migration: a DEPLOYED db sheds the old pair UNIQUE
#
# The riskiest code in this change. SQLite cannot ALTER a constraint away, so a
# deployed paw_bar.db needs a table REBUILD, and every test above runs against a
# fresh schema that never exercises it. These build the old table by hand.
# --------------------------------------------------------------------------- #


_LEGACY_CONVERSATIONS_SQL = """
CREATE TABLE paw_bar_conversations (
    id TEXT PRIMARY KEY,
    widget_id TEXT NOT NULL,
    customer_ref TEXT NOT NULL,
    workspace_id TEXT DEFAULT '',
    state TEXT DEFAULT 'open',
    bot_paused INTEGER DEFAULT 0,
    snooze_until TEXT DEFAULT '',
    assignee TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    notes TEXT DEFAULT '[]',
    contact_email TEXT DEFAULT '',
    last_visitor_at TEXT DEFAULT '',
    last_owner_at TEXT DEFAULT '',
    bot_paused_at TEXT DEFAULT '',
    unread_for_owner INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (widget_id, customer_ref)
);
"""


@pytest.mark.asyncio
async def test_legacy_db_sheds_the_pair_unique_and_keeps_its_row(tmp_path):
    """A deployed DB carrying the old ``UNIQUE (widget_id, customer_ref)`` must
    end up able to hold a visitor's SECOND conversation, without losing the row
    it already had."""
    import aiosqlite

    db_path = tmp_path / "legacy.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_LEGACY_CONVERSATIONS_SQL)
        await db.execute(
            "INSERT INTO paw_bar_conversations"
            " (id, widget_id, customer_ref, workspace_id, state, unread_for_owner,"
            " created_at, updated_at)"
            " VALUES ('ppc_legacy', 'w-1', ?, 'ws-1', 'needs_human', 3, ?, ?)",
            (_REF, "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
        )
        await db.commit()

    st = PawBarStore(db_path)

    # The pre-existing row survived the rebuild INTACT — id, state and counters.
    legacy = await st.get_conversation_by_id("ppc_legacy")
    assert legacy is not None
    assert legacy.state.value == "needs_human"
    assert legacy.unread_for_owner == 3
    assert legacy.active is True

    # And the constraint that caused the bug is gone: a second one now fits.
    second = await st.open_conversation("w-1", _REF, workspace_id="ws-1")
    rows = await st.list_conversations_for_visitor("w-1", _REF, workspace_id="ws-1")
    assert {r.id for r in rows} == {"ppc_legacy", second.id}
    assert [r.active for r in rows] == [True, False]


@pytest.mark.asyncio
async def test_the_rebuild_is_idempotent(tmp_path):
    """A second open of the same DB must not rebuild again (and must not lose
    anything if it somehow did). Guards the detection predicate: the REPLACEMENT
    index is unique too, so a name-based or unique-only check would loop."""
    import aiosqlite

    db_path = tmp_path / "twice.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_LEGACY_CONVERSATIONS_SQL)
        await db.commit()

    first = await PawBarStore(db_path).open_conversation("w-1", _REF, workspace_id="ws-1")
    # A brand-new store object over the same file re-runs _ensure_schema.
    second = await PawBarStore(db_path).open_conversation("w-1", _REF, workspace_id="ws-1")

    rows = await PawBarStore(db_path).list_conversations_for_visitor(
        "w-1", _REF, workspace_id="ws-1"
    )
    assert {r.id for r in rows} == {first.id, second.id}


@pytest.mark.asyncio
async def test_the_active_invariant_survives_the_rebuild(tmp_path):
    """The partial unique index must be REAL after a rebuild, not just intended:
    two active conversations for one visitor is the state that would let the
    original bug back in through a different door."""
    import aiosqlite

    db_path = tmp_path / "invariant.db"
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_LEGACY_CONVERSATIONS_SQL)
        await db.commit()

    st = PawBarStore(db_path)
    await st.open_conversation("w-1", _REF, workspace_id="ws-1")

    with pytest.raises(aiosqlite.IntegrityError):
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO paw_bar_conversations"
                " (id, widget_id, customer_ref, workspace_id, state, active,"
                " created_at, updated_at)"
                " VALUES ('ppc_second_active', 'w-1', ?, 'ws-1', 'open', 1, ?, ?)",
                (_REF, "2026-08-19T00:00:00", "2026-08-19T00:00:00"),
            )
            await db.commit()


# --------------------------------------------------------------------------- #
# Layer 2 — the router: the agent session follows the CONVERSATION
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_session_key_is_scoped_to_the_conversation_not_the_visitor(chat):
    """The bug as the agent experienced it.

    ``session_key`` was ``cloud:concierge:{pocket}:{customer_ref}:{agent}`` — no
    conversation component — so every conversation a visitor ever had was one
    agent session. Two conversations must produce two keys.
    """
    client, store, executor, widget = chat

    await _say(client, widget.id, "What time do you open?")
    first_key = executor.submitted[-1].session_key

    opened = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _say(client, widget.id, "Different question", conversation_id=opened.id)
    second_key = executor.submitted[-1].session_key

    assert first_key != second_key, "a new conversation must be a new agent session"
    assert opened.id in second_key


@pytest.mark.asyncio
async def test_a_fresh_conversation_starts_cold(chat):
    """The visible harm. ``_load_concierge_history`` replayed by VISITOR, so a
    visitor who started over still had the abandoned thread in the agent's
    context. A fresh conversation must carry none of it.

    The abandoned turn is seeded as a real run doc rather than sent through the
    endpoint: the fixture stubs ``create_run``, so a dispatched turn writes
    nothing and this would pass against an empty collection no matter how the
    history read is scoped.
    """
    client, store, executor, widget = chat

    abandoned = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _seed_run(
        session_key=f"cloud:concierge:pocket-1:{abandoned.id}:agent-xyz",
        user_text="My order number is 4417",
        partial_text="Order 4417 ships Tuesday.",
    )

    # Prove the seed IS readable on its own conversation, so the assertion below
    # is isolation working rather than an empty collection.
    await _say(client, widget.id, "Any update?", conversation_id=abandoned.id)
    assert "4417" in str(executor.submitted[-1].history)

    opened = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _say(client, widget.id, "Do you ship overseas?", conversation_id=opened.id)

    replayed = " ".join(m.get("content", "") for m in (executor.submitted[-1].history or []))
    assert "4417" not in replayed, "the abandoned conversation leaked into the new one"


@pytest.mark.asyncio
async def test_visitor_can_list_and_open_conversations(chat):
    """The Messages tab's contract end to end: send a turn, start over, and the
    visitor sees BOTH conversations with the new one active."""
    client, store, executor, widget = chat

    await _say(client, widget.id, "First question")

    res = await client.post(
        "/paw-bar/conversations",
        json={"key": _VALID_KEY, "w": widget.id, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 200, res.text
    opened = res.json()
    assert opened["active"] is True

    res = await client.get(
        "/paw-bar/conversations",
        params={"w": widget.id, "key": _VALID_KEY, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 200, res.text
    rows = res.json()["conversations"]

    assert len(rows) == 2
    assert rows[0]["id"] == opened["id"]
    # Exactly one conversation is in progress, and it is the new one.
    assert [r["active"] for r in rows] == [True, False]


@pytest.mark.asyncio
async def test_a_visitor_can_read_back_their_own_conversation(chat):
    """The read the widget never had.

    Its thread lived only in the frame's localStorage, so anything that lost that
    storage lost the history — and plenty does: the bar is a third-party iframe
    (Safari blocks its storage, Chrome and Firefox partition it per top-level
    site) and the stored row carries a 7-day TTL. The server had every message the
    whole time; nothing could ask for it.
    """
    client, store, executor, widget = chat
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    conversation = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await ChatRunDoc(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key=f"cloud:concierge:pocket-1:{conversation.id}:agent-xyz",
        user_id=_REF,
        agent_id="agent-xyz",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        status="completed",
        user_text="Do you deliver on Sundays?",
        partial_text="We do, until 4pm.",
    ).insert()

    res = await client.get(
        f"/paw-bar/conversations/{conversation.id}/messages",
        params={"w": widget.id, "key": _VALID_KEY, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["conversation_id"] == conversation.id
    spoken = " ".join(m["content"] for m in body["messages"])
    # BOTH halves: the visitor's own question and the reply. A thread that comes
    # back assistant-only reads as the bar talking to itself.
    assert "Do you deliver on Sundays?" in spoken
    assert "We do, until 4pm." in spoken


@pytest.mark.asyncio
async def test_a_visitor_cannot_read_another_visitors_conversation(chat):
    """Two visitors of the same bar share the widget, the site and the agent —
    only the handle separates them. A conversation id is guessable in a way a
    handle is not, so the id alone must never be enough to read a thread."""
    client, store, executor, widget = chat

    stranger = "cust-stranger-9"
    theirs = await store.open_conversation(widget.id, stranger, workspace_id="ws-1")

    res = await client.get(
        f"/paw-bar/conversations/{theirs.id}/messages",
        params={"w": widget.id, "key": _VALID_KEY, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )

    # 404, not an empty 200: the loader filters on customer_ref so it would answer
    # empty anyway, but that would imply the conversation exists and is silent.
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_an_unknown_conversation_id_is_a_404(chat):
    client, store, executor, widget = chat

    res = await client.get(
        "/paw-bar/conversations/ppc-does-not-exist/messages",
        params={"w": widget.id, "key": _VALID_KEY, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_known_but_empty_conversation_reads_as_empty_not_missing(chat):
    """A conversation the store knows about with nothing said in it yet.

    Distinct from a missing one, and the widget needs the difference: empty means
    "show the greeting", 404 means "this pointer is stale, stop trusting it".
    """
    client, store, executor, widget = chat

    opened = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")

    res = await client.get(
        f"/paw-bar/conversations/{opened.id}/messages",
        params={"w": widget.id, "key": _VALID_KEY, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200, res.text
    assert res.json()["messages"] == []


@pytest.mark.asyncio
async def test_visitor_conversations_never_leak_across_visitors(chat):
    """A sibling visitor of the SAME bar shares the widget, the site and the
    agent — only the handle separates them. Their conversations must not be
    listable by each other."""
    client, store, executor, widget = chat

    stranger = "cust-stranger-9"
    await store.open_conversation(widget.id, stranger, workspace_id="ws-1")
    mine = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")

    res = await client.get(
        "/paw-bar/conversations",
        params={"w": widget.id, "key": _VALID_KEY, "customer_ref": _REF},
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 200, res.text
    ids = [r["id"] for r in res.json()["conversations"]]

    assert ids == [mine.id]


@pytest.mark.asyncio
async def test_a_stranger_conversation_id_falls_back_to_your_own(chat):
    """``conversation_id`` is a client-supplied HINT, never an authority. Naming
    someone else's conversation must not attach your turn to their thread — and
    must not confirm the id was real either."""
    client, store, executor, widget = chat

    stranger = await store.open_conversation(widget.id, "cust-stranger-9", workspace_id="ws-1")
    await _say(client, widget.id, "Whose thread is this?", conversation_id=stranger.id)

    assert stranger.id not in executor.submitted[-1].session_key


@pytest.mark.asyncio
async def test_chat_without_a_conversation_id_still_works(chat):
    """Compat. Widget bundles cached in the wild send no ``conversation_id``;
    they must resolve-or-create the visitor's active conversation rather than
    422. This is what lets the backend ship before the widget does."""
    client, store, executor, widget = chat

    await _say(client, widget.id, "First, from an old bundle")
    await _say(client, widget.id, "Second, from an old bundle")

    # Both turns landed on ONE conversation, and that conversation is real.
    keys = {s.session_key for s in executor.submitted}
    assert len(keys) == 1
    rows = await store.list_conversations_for_visitor(widget.id, _REF, workspace_id="ws-1")
    assert len(rows) == 1
