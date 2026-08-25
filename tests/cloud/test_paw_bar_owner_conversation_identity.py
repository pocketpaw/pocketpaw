# tests/cloud/test_paw_bar_owner_conversation_identity.py — the OWNER half of
# conversation identity.
#
# Created 2026-08-19 as the reproduction for the second half of the reported
# session bug. The VISITOR half shipped (see
# test_paw_bar_conversation_identity.py): a visitor may now hold several
# conversations, each with its own id, its own ``session_key`` and its own
# history replay. The owner half never moved. Every owner-facing seam still keys
# on ``customer_ref`` — the VISITOR — so the product disagrees with itself about
# what a conversation is:
#
#   * ``_list_conversations`` dedupes the run scan by ``run.user_id``, keeping
#     "the most-recent run per customer". A visitor with four conversations is
#     ONE row in the owner's inbox, showing only their latest sentence. The other
#     three are unreachable — not filtered, not collapsed behind a disclosure:
#     absent.
#   * ``paw_bar_owner_messages`` has no ``conversation_id`` column, so an owner's
#     reply cannot be addressed to a conversation even in principle. It is
#     appended to the visitor.
#   * ``POST …/conversations/{customer_ref}/reply`` and
#     ``PATCH …/conversations/{customer_ref}`` both resolve through
#     ``get_conversation(widget, ref)``, which returns the ACTIVE row. An owner
#     answering the question a visitor asked yesterday writes into whatever the
#     visitor happens to be saying today.
#   * ``GET /paw-bar/messages/{widget}/{ref}`` serves every owner line the
#     visitor has ever been sent, into whichever conversation is on screen.
#
# The last two are the ones that hurt: taking over a conversation mutes the bot
# for that visitor EVERYWHERE, and a reply meant for one thread surfaces in
# another. That is a support tool answering the wrong customer question in front
# of the customer.
#
# Every test here fails before the owner-side fix and passes after it.

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarBlock, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore

_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
_REF = "cust-0001"
_USER_ID = PydanticObjectId()


# --------------------------------------------------------------------------- #
# Builders — same shapes as test_paw_bar_conversations.py
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
            pocket_id="pocket-1",
            blocks=[PawBarBlock(type="text", content="Hi")],
        ),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
    )
    d.update(ov)
    return PawBarWidget(**d)


def _skey(conversation_id: str) -> str:
    """The run session_key the chat endpoint builds for a conversation."""
    return f"cloud:concierge:pocket-1:{conversation_id}:agent-xyz"


async def _mk_run(**ov: Any):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    d = dict(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key=_skey(_REF),
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


def _fake_user(role: str, workspace_id: str = "ws-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=_USER_ID,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "owner-conv.db")


@pytest_asyncio.fixture
async def client(mongo_db, store, monkeypatch):
    """ADMIN client — the owner's inbox."""
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[current_active_user] = lambda: _fake_user("admin")
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


@pytest_asyncio.fixture
async def public(mongo_db, store, monkeypatch):
    """PUBLIC client — the visitor's own poll."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


async def _two_threads(store):
    """One visitor, two conversations. Store-only — no run docs, so this is
    usable without Beanie.

    ``retired`` is the older one the visitor has moved on from; ``current`` is
    the one in progress. The owner must be able to address either.
    """
    widget = await store.create_widget(_widget())
    retired = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    current = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    return widget, retired, current


async def _two_conversations(store):
    """``_two_threads`` plus a concierge turn in each, so the run-backed inbox
    read has something to list. Needs Beanie (the ``mongo_db`` fixture)."""
    widget, retired, current = await _two_threads(store)
    await _mk_run(session_key=_skey(retired.id), partial_text="We open at 8.")
    await _mk_run(session_key=_skey(current.id), partial_text="Yes, we ship to Berlin.")
    return widget, retired, current


# --------------------------------------------------------------------------- #
# Layer 1 — the store: an owner line belongs to a CONVERSATION
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_owner_reply_belongs_to_one_conversation(store):
    """``paw_bar_owner_messages`` has no conversation column, so today an owner
    reply is addressed to the visitor and appears in every thread they own."""
    widget, retired, current = await _two_threads(store)

    await store.add_owner_message(
        widget.id,
        _REF,
        "Sorry for the wait — 8am, yes.",
        conversation_id=retired.id,
        workspace_id="ws-1",
    )

    in_retired = await store.list_owner_messages(
        widget.id, _REF, workspace_id="ws-1", conversation_id=retired.id
    )
    assert [m.content for m in in_retired] == ["Sorry for the wait — 8am, yes."]
    # The conversation the visitor is actually looking at must not show it.
    in_current = await store.list_owner_messages(
        widget.id, _REF, workspace_id="ws-1", conversation_id=current.id
    )
    assert in_current == []


@pytest.mark.asyncio
async def test_a_legacy_owner_line_still_reads_on_the_visitors_thread(store):
    """Backfill-free: lines written before the column existed carry no
    conversation, and must stay readable rather than vanishing from history."""
    widget, _retired, _current = await _two_threads(store)
    await store.add_owner_message(widget.id, _REF, "From before", workspace_id="ws-1")

    unscoped = await store.list_owner_messages(widget.id, _REF, workspace_id="ws-1")
    assert [m.content for m in unscoped] == ["From before"]


# --------------------------------------------------------------------------- #
# Layer 2 — the inbox lists CONVERSATIONS
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_inbox_lists_each_conversation_not_each_visitor(client):
    """The reported bug, at the owner's screen: a visitor's several
    conversations are deduped into one row keyed on their handle."""
    c, store = client
    site = await _site()
    _w, retired, current = await _two_conversations(store)

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()

    assert len(body["items"]) == 2
    assert {i["conversation_id"] for i in body["items"]} == {retired.id, current.id}
    # Each row shows its OWN last sentence, not the visitor's latest overall.
    previews = {i["conversation_id"]: i["preview"] for i in body["items"]}
    assert previews[retired.id] == "We open at 8."
    assert previews[current.id] == "Yes, we ship to Berlin."
    # The visitor is still named on every row — one person, several threads.
    assert {i["customer_ref"] for i in body["items"]} == {_REF}


@pytest.mark.asyncio
async def test_a_conversation_spanning_the_migration_is_ONE_row(client):
    """The common shape on the day this deploys, not an exotic edge.

    A visitor mid-thread when conversation identity shipped has runs of BOTH
    spellings on the SAME conversation: the older ones carry their handle in the
    session_key's conversation slot, the newer ones carry the real id. Grouped by
    those raw tokens they are two rows — and both resolve to the same
    conversation, so the pair share an id. The owner sees the thread twice, and
    a client keying its list on conversation_id has duplicate keys.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    conversation = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    # Pre-identity: the visitor's handle sat in the conversation slot.
    await _mk_run(session_key=_skey(_REF), partial_text="We open at 8.")
    # Post-identity: the same thread, now carrying its real id.
    await _mk_run(session_key=_skey(conversation.id), partial_text="Yes, we ship to Berlin.")

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()

    assert len(body["items"]) == 1
    assert body["items"][0]["conversation_id"] == conversation.id
    # Newest-first, so the row shows the latest thing said in it.
    assert body["items"][0]["preview"] == "Yes, we ship to Berlin."


# --------------------------------------------------------------------------- #
# Layer 3 — intervening addresses ONE conversation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_owner_reply_lands_on_the_conversation_it_names(client):
    """Answering yesterday's question must not appear inside today's thread."""
    c, store = client
    site = await _site()
    widget, retired, current = await _two_threads(store)

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "Sorry for the wait — 8am, yes.", "conversation_id": retired.id},
    )

    assert res.status_code == 200
    # The echoed row identifies itself by ``id`` — it always did; nothing ever
    # read it as identity. The LIST row is the one that needed a new field, since
    # it has no id of its own and ``customer_ref`` names the person, not the row.
    assert res.json()["conversation"]["id"] == retired.id
    # BOTH halves, deliberately. Asserting only that the reply is absent from the
    # wrong thread passes when it is stored with NO conversation at all — which
    # is precisely the pre-fix behaviour, and is how this test first escaped its
    # own mutation (drop the reply's ``conversation_id=`` and watch it).
    in_retired = await store.list_owner_messages(
        widget.id, _REF, workspace_id="ws-1", conversation_id=retired.id
    )
    assert [m.content for m in in_retired] == ["Sorry for the wait — 8am, yes."]
    in_current = await store.list_owner_messages(
        widget.id, _REF, workspace_id="ws-1", conversation_id=current.id
    )
    assert in_current == []


@pytest.mark.asyncio
async def test_taking_over_one_conversation_leaves_the_others_bot_live(client):
    """Muting the assistant is per-conversation. Today ``bot_paused`` is written
    to the visitor's active row, so taking over ANY thread silences the bot on
    the one the visitor is currently typing into."""
    c, store = client
    site = await _site()
    _w, retired, current = await _two_conversations(store)

    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        json={"bot_paused": True, "conversation_id": retired.id},
    )

    assert res.status_code == 200
    assert (await store.get_conversation_by_id(retired.id)).bot_paused is True
    assert (await store.get_conversation_by_id(current.id)).bot_paused is False


@pytest.mark.asyncio
async def test_the_transcript_reads_one_conversation_not_the_visitors_history(client):
    """The drill-in the owner actually reads before replying.

    Both sources have to narrow together. Narrowing only the runs would be worse
    than narrowing neither: one conversation's questions would render interleaved
    with every reply a human ever sent that visitor, and the timestamps would
    make it look like a coherent exchange.
    """
    c, store = client
    site = await _site()
    widget, retired, current = await _two_conversations(store)
    await store.add_owner_message(
        widget.id, _REF, "About your 8am question", conversation_id=retired.id, workspace_id="ws-1"
    )
    await store.add_owner_message(
        widget.id, _REF, "About Berlin", conversation_id=current.id, workspace_id="ws-1"
    )

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": current.id},
    )

    assert res.status_code == 200
    contents = [m["content"] for m in res.json()["messages"]]
    assert "Yes, we ship to Berlin." in contents
    assert "About Berlin" in contents
    # Neither half of the other conversation crosses over.
    assert "We open at 8." not in contents
    assert "About your 8am question" not in contents


@pytest.mark.asyncio
async def test_an_owner_cannot_address_another_visitors_conversation(client):
    """``conversation_id`` is an address the client supplies, so it is checked
    against the visitor in the path rather than trusted. Otherwise the owner's
    own inbox becomes a way to write into any conversation on the site by id."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    mine = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    theirs = await store.open_conversation(widget.id, "cust-0002", workspace_id="ws-1")

    res = await c.patch(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        json={"state": "closed", "conversation_id": theirs.id},
    )

    assert res.status_code == 404
    assert (await store.get_conversation_by_id(theirs.id)).state.value == "open"
    assert (await store.get_conversation_by_id(mine.id)).state.value == "open"


# --------------------------------------------------------------------------- #
# Layer 4 — the visitor's poll is scoped to the conversation on screen
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_visitor_poll_only_returns_this_conversations_lines(public):
    """The delivery seam. Without it the owner-side fix is invisible: the widget
    would still drop every owner line into whichever thread is open."""
    c, store = public
    await _site()
    widget, retired, current = await _two_threads(store)
    await store.add_owner_message(
        widget.id, _REF, "About your 8am question", conversation_id=retired.id, workspace_id="ws-1"
    )
    await store.add_owner_message(
        widget.id, _REF, "About Berlin", conversation_id=current.id, workspace_id="ws-1"
    )

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params={"signed_key": _KEY, "conversation_id": current.id},
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200
    assert [m["content"] for m in res.json()["messages"]] == ["About Berlin"]


# --------------------------------------------------------------------------- #
# Layer 5 — the row the LIST shows must OPEN
#
# Added 2026-08-26. Layer 2 proved the list folds a pre-identity run into the
# conversation row it belongs to. Nothing proved the drill-in agrees, and it did
# not: the transcript rebuilt an exact ``session_key`` from the conversation id
# and the widget's CURRENT agent, then filtered runs on equality. A run written
# under either an older key spelling or an earlier bound agent matched nothing,
# ``_load_transcript`` returned None, and the endpoint 404'd — so a row an owner
# could see in their inbox opened as "Nothing stored for this visitor yet."
#
# The rule these tests pin is one rule, not two: a run belongs to the
# conversation the LIST would file it under. Anything else is the product
# disagreeing with itself about what a conversation is, which is the same bug
# this file was opened for.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_conversation_spanning_the_migration_opens_whole(client):
    """The row Layer 2 merges must open with BOTH halves in it.

    Same shape as ``test_a_conversation_spanning_the_migration_is_ONE_row``, one
    step further in: the owner clicks the row. The pre-identity turn and the
    unmigrated owner line are part of this thread — the list already says so —
    so the transcript that opens has to contain them.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    conversation = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _mk_run(session_key=_skey(_REF), partial_text="We open at 8.")
    await _mk_run(session_key=_skey(conversation.id), partial_text="Yes, we ship to Berlin.")
    await store.add_owner_message(
        widget.id, _REF, "Sorry for the wait.", workspace_id="ws-1"
    )

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": conversation.id},
    )

    assert res.status_code == 200
    contents = [m["content"] for m in res.json()["messages"]]
    assert "We open at 8." in contents
    assert "Yes, we ship to Berlin." in contents
    assert "Sorry for the wait." in contents


@pytest.mark.asyncio
async def test_a_pre_identity_conversation_is_not_an_empty_thread(client):
    """A site whose whole history predates conversation identity.

    Every row in that inbox listed fine and every one of them opened empty,
    which is the report this fix answers. The id under test is taken from the
    LIST rather than hand-built, so the test asks the same question the UI does.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _mk_run(session_key=_skey(_REF), partial_text="We open at 8.")

    listed = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()["items"]
    assert len(listed) == 1

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": listed[0]["conversation_id"]},
    )

    assert res.status_code == 200
    assert [m["content"] for m in res.json()["messages"]] == ["We open at 8."]


@pytest.mark.asyncio
async def test_a_pre_identity_turn_stays_out_of_a_retired_conversation(client):
    """The fix must not widen into "show the owner everything".

    An unattributable turn belongs to the visitor's conversation IN PROGRESS —
    that is where the list files it. Opening the thread they have moved on from
    must not pull it in, or the drill-in re-creates the interleaving that
    conversation identity was built to remove.
    """
    c, store = client
    site = await _site()
    widget, retired, current = await _two_threads(store)
    await _mk_run(session_key=_skey(_REF), partial_text="We open at 8.")
    await _mk_run(session_key=_skey(current.id), partial_text="Yes, we ship to Berlin.")

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": retired.id},
    )

    assert res.status_code == 200
    contents = [m["content"] for m in res.json()["messages"]]
    assert "We open at 8." not in contents
    assert "Yes, we ship to Berlin." not in contents


@pytest.mark.asyncio
async def test_a_transcript_survives_the_widget_being_rebound(client):
    """The agent id is the LAST segment of the key, and it is not stable.

    A widget created unbound gets a dedicated agent provisioned onto it later
    (the E1/E2 hook), so turns answered before that carry a different agent in
    their ``session_key`` than the widget carries today. Rebuilding the key from
    the CURRENT agent and matching on equality loses every one of them. The list
    already reads the token positionally and is unaffected; this pins the
    transcript to the same reading.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    conversation = await store.open_conversation(widget.id, _REF, workspace_id="ws-1")
    await _mk_run(
        session_key=f"cloud:concierge:pocket-1:{conversation.id}:agent-before-rebind",
        partial_text="We open at 8.",
    )

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": conversation.id},
    )

    assert res.status_code == 200
    assert [m["content"] for m in res.json()["messages"]] == ["We open at 8."]
