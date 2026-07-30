# tests/cloud/test_paw_bar_takeover.py — type-to-takeover (owner inbox, slice 2).
# Created 2026-07-30: the owner types, the bot shuts up, the visitor sees a human.
# Layers, in the order the feature fails if any one of them is wrong:
#   * Owner reply: persists an owner line, mutes the bot (stamped), clears the
#     unread counter, reopens a closed/snoozed thread, refuses empty / oversized
#     text, refuses a ref with no conversation, and refuses a cross-workspace
#     caller.
#   * THE MUTE (centrepiece): a visitor turn into a paused conversation emits
#     ``human_replying``, emits NO chunk frames, dispatches NO run, and — the
#     billing-safety proof — creates NO ChatRunDoc. It still keeps the visitor's
#     line (under the site's retention toggle) and escalates to needs_human.
#   * Visitor poll: owner/system lines oldest-first, strict ``after`` filtering,
#     the EXACT public key allowlist (no notes / tags / assignee / email / state),
#     a visitor's own stored line never echoed back, and every refusal code on the
#     front-gate chain (400 → 404 → 429 → 401 → 403 origin, 403 binding).
#   * Idle auto-resume: a mute with no owner activity for 4h reads as un-paused,
#     materializes once with a system message, and a FRESH pause does not resume.
#   * Transcript merge: all four roles interleaved strictly by timestamp, an
#     assistant-only thread still renders, and a muted-turn visitor line appears as
#     "user" even though it never had a run.

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import (
    ConversationState,
    OwnerMessageRole,
    PawBarBlock,
    PawBarEvent,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import (
    BOT_PAUSE_IDLE_HOURS,
    BOT_RESUME_SYSTEM_MESSAGE,
    PawBarStore,
)

_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"
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


async def _run_count() -> int:
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    return len(await ChatRunDoc.find_all().to_list())


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
    return PawBarStore(tmp_path / "takeover.db")


@pytest_asyncio.fixture
async def client(mongo_db, store, monkeypatch):
    """ADMIN client in ws-1 (the owner side), backed by the tmp store + Beanie."""
    app = _build_app(store, monkeypatch, role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


@pytest_asyncio.fixture
async def public(mongo_db, store, monkeypatch):
    """PUBLIC client (no auth overrides) for the visitor-facing endpoints."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


class _FakeExecutor:
    """Captures submitted specs; writes a canned reply so a real turn terminates."""

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


def _stub_run_dispatch(monkeypatch, *, real_create_run: bool = False) -> _FakeExecutor:
    """Wire the in-memory transport + a capturing executor, and stub create_run.

    Returns the executor so a test can assert what was dispatched — or, for the
    mute, that NOTHING was. ``real_create_run=True`` lets the REAL ``create_run``
    write this turn's ChatRunDoc: that is what makes "a paused turn creates no run
    doc" a proof rather than a tautology, because the same call with the bot
    un-paused demonstrably DOES write one.
    """
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

    transport = InMemoryStreamTransport()
    fake_exec = _FakeExecutor(transport)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: fake_exec)

    if not real_create_run:

        async def _fake_create_run(spec):
            return SimpleNamespace(run_id=spec.run_id)

        monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)
    return fake_exec


def _chat_payload(widget_id: str, **ov) -> dict:
    p = dict(
        widget_id=widget_id,
        signed_key=_KEY,
        customer_ref=_REF,
        message="Are you there?",
    )
    p.update(ov)
    return p


def _poll_params(**ov) -> dict:
    p = dict(signed_key=_KEY)
    p.update(ov)
    return p


async def _paused_conversation(store, widget_id: str, workspace_id: str = "ws-1"):
    """A conversation an owner has taken over, the state the mute reads."""
    await store.upsert_conversation_on_visitor_turn(widget_id, _REF, workspace_id)
    return await store.update_conversation(
        widget_id,
        _REF,
        workspace_id=workspace_id,
        bot_paused=True,
        last_owner_at=datetime.now().isoformat(),
    )


def _age_the_pause(store, widget_id: str, hours: int, workspace_id: str = "ws-1"):
    """Backdate a mute so the idle window has (or hasn't) elapsed."""
    stamp = (datetime.now() - timedelta(hours=hours)).isoformat()
    return store.update_conversation(
        widget_id,
        _REF,
        workspace_id=workspace_id,
        bot_paused_at=stamp,
        last_owner_at=stamp,
    )


# --------------------------------------------------------------------------- #
# Layer 1 — the owner's reply
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reply_persists_pauses_and_clears_unread(client):
    """One call does all of it: the line lands, the bot mutes, the badge clears."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "  Hi! I'm Maya, I can help.  "},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    # The echo is a transcript message — the shape the thread already renders.
    assert body["message"] == {
        "role": "owner",
        "content": "Hi! I'm Maya, I can help.",
        "created_at": body["message"]["created_at"],
    }
    assert body["message"]["created_at"]
    row = body["conversation"]
    assert row["bot_paused"] is True
    assert row["bot_paused_at"], "the mute is stamped, or the idle clock never starts"
    assert row["last_owner_at"]
    assert row["unread_for_owner"] == 0

    stored = await store.list_owner_messages(widget.id, _REF, workspace_id="ws-1")
    assert [(m.role.value, m.content, m.author) for m in stored] == [
        ("owner", "Hi! I'm Maya, I can help.", "u1")
    ]


@pytest.mark.asyncio
async def test_reply_never_creates_a_run_doc(client):
    """An owner reply is not agent compute: no ChatRunDoc, so the metering sweeper
    can never bill the owner for typing their own sentence."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run()
    before = await _run_count()

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "On it."}
    )

    assert res.status_code == 200
    assert await _run_count() == before


@pytest.mark.parametrize("filed", ["closed", "snoozed"])
@pytest.mark.asyncio
async def test_reply_reopens_a_filed_conversation(client, filed):
    """An owner replying is engagement — a filed thread comes back to the queue."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.update_conversation(
        widget.id,
        _REF,
        workspace_id="ws-1",
        state=filed,
        snooze_until=(datetime.now() + timedelta(hours=2)).isoformat(),
    )

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "Sorry — here!"}
    )

    assert res.status_code == 200
    assert res.json()["conversation"]["state"] == "open"
    assert res.json()["conversation"]["snooze_until"] == ""


@pytest.mark.asyncio
async def test_reply_on_a_needs_human_thread_keeps_that_state(client):
    """needs_human is the top of the queue; replying doesn't demote it."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.update_conversation(widget.id, _REF, workspace_id="ws-1", state="needs_human")

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "Looking now."}
    )

    assert res.json()["conversation"]["state"] == "needs_human"


@pytest.mark.asyncio
async def test_reply_mints_a_row_for_a_legacy_conversation(client):
    """A conversation that predates the state table is replyable — the row is minted
    on the first owner action rather than 404ing."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()  # runs exist, no state row

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "Hello!"}
    )

    assert res.status_code == 200
    assert (await store.get_conversation(widget.id, _REF, "ws-1")).bot_paused is True


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
@pytest.mark.asyncio
async def test_reply_refuses_empty_text(client, text):
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run()
    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": text}
    )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_reply_refuses_an_oversized_message(client):
    """Refused, never silently truncated — a customer-facing sentence that ends
    mid-word without anyone saying so is worse than an error the owner can act on."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "x" * 4001}
    )
    assert res.status_code == 422
    assert await store.list_owner_messages(widget.id, _REF) == []


@pytest.mark.asyncio
async def test_reply_to_an_unknown_ref_is_404(client):
    """A ref with no concierge runs here isn't a conversation, it's a guess."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/cust-9999/reply", json={"text": "hi"}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_reply_refuses_a_malformed_ref(client):
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.post(f"/paw-bar/admin/site/{site.id}/conversations/no/reply", json={"text": "hi"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_reply_requires_an_admin_role(mongo_db, store, monkeypatch):
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run()
    app = _build_app(store, monkeypatch, role="member")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.post(
            f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "hi"}
        )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_reply_is_cross_workspace_refused(mongo_db, store, monkeypatch):
    """A ws-2 admin cannot answer a ws-1 visitor — the site 404s first."""
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    app = _build_app(store, monkeypatch, role="admin", workspace_id="ws-2")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.post(
            f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply", json={"text": "hi"}
        )

    assert res.status_code == 404
    assert await store.list_owner_messages(widget.id, _REF) == []
    assert (await store.get_conversation(widget.id, _REF, "ws-1")).bot_paused is False


# --------------------------------------------------------------------------- #
# Layer 2 — THE MUTE. The bot does not talk over a human.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_paused_visitor_turn_emits_human_replying_and_dispatches_nothing(public, monkeypatch):
    """THE centrepiece. A visitor typing into a taken-over conversation gets a
    distinct non-answer frame — never a bot reply, never an empty stream — and the
    turn dispatches no run at all."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    # The REAL create_run — an un-paused turn through this exact path writes a run
    # doc (the control test below proves it), so "none was written" means the mute
    # stopped it, not that the test stubbed it away.
    fake_exec = _stub_run_dispatch(monkeypatch, real_create_run=True)
    await _paused_conversation(store, widget.id)
    before = await _run_count()

    res = await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert res.status_code == 200
    body = res.text
    assert "event: human_replying" in body
    assert '"message": "Someone from the team is replying' in body
    assert "event: stream_end" in body
    # No bot voice, in any form.
    assert "event: chunk" not in body
    assert "event: message.persisted" not in body
    # THE BILLING-SAFETY PROOF: no run doc was created, so the metering sweeper
    # (which bills every unbilled terminal run) has nothing to charge for a turn
    # the agent never took.
    assert await _run_count() == before
    assert fake_exec.submitted == [], "no run was dispatched to the executor"


@pytest.mark.asyncio
async def test_an_unpaused_turn_does_create_a_run_doc(public, monkeypatch):
    """The control for the proof above: the same call, the same real create_run,
    with the bot un-paused — one run doc. Without this, 'no run doc' proves nothing."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_dispatch(monkeypatch, real_create_run=True)
    before = await _run_count()

    await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert await _run_count() == before + 1


@pytest.mark.asyncio
async def test_paused_turn_keeps_the_visitor_line_and_escalates(public, monkeypatch):
    """The owner must be able to READ what the visitor said while they were typing,
    and the thread must show that someone is waiting on a person."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_dispatch(monkeypatch)
    await _paused_conversation(store, widget.id)

    await c.post(
        "/paw-bar/chat",
        json=_chat_payload(widget.id, message="Is anyone there?"),
        headers={"Origin": _ORIGIN},
    )

    stored = await store.list_owner_messages(widget.id, _REF, workspace_id="ws-1")
    assert [(m.role.value, m.content) for m in stored] == [("visitor", "Is anyone there?")]
    conversation = await store.get_conversation(widget.id, _REF, "ws-1")
    assert conversation.state is ConversationState.NEEDS_HUMAN
    assert conversation.unread_for_owner == 2, "the visitor's new message is unread"


@pytest.mark.asyncio
async def test_paused_turn_honours_the_retention_toggle(public, monkeypatch):
    """Muting is not a back door around the owner's privacy choice: with transcript
    storage off, the visitor's line is not kept here either."""
    c, store = public
    await _site(concierge_store_transcripts=False)
    widget = await store.create_widget(_widget())
    _stub_run_dispatch(monkeypatch)
    await _paused_conversation(store, widget.id)

    res = await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert "event: human_replying" in res.text
    assert await store.list_owner_messages(widget.id, _REF, workspace_id="ws-1") == []
    # The escalation still happens — that is metadata, not content.
    assert (
        await store.get_conversation(widget.id, _REF, "ws-1")
    ).state is ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_unpaused_visitor_turn_still_gets_the_bot(public, monkeypatch):
    """The mute is the exception, not the rule: a normal conversation is untouched."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    fake_exec = _stub_run_dispatch(monkeypatch)

    res = await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert "event: chunk" in res.text
    assert "event: human_replying" not in res.text
    assert len(fake_exec.submitted) == 1


@pytest.mark.asyncio
async def test_a_mute_on_another_widget_does_not_mute_this_one(public, monkeypatch):
    """The mute is keyed by (widget, visitor). A sibling site taking over its own
    visitor of the same handle must not silence this site's bot."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    fake_exec = _stub_run_dispatch(monkeypatch)
    await store.upsert_conversation_on_visitor_turn("other-widget", _REF, "ws-2")
    await store.update_conversation(
        "other-widget",
        _REF,
        workspace_id="ws-2",
        bot_paused=True,
        last_owner_at=datetime.now().isoformat(),
    )

    res = await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert "event: chunk" in res.text
    assert len(fake_exec.submitted) == 1


# --------------------------------------------------------------------------- #
# Layer 3 — the visitor's poll
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_poll_returns_owner_and_system_lines_oldest_first(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.add_owner_message(widget.id, _REF, "First", workspace_id="ws-1")
    await store.add_owner_message(
        widget.id, _REF, "Second", role=OwnerMessageRole.SYSTEM, workspace_id="ws-1"
    )

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200
    body = res.json()
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("owner", "First"),
        ("system", "Second"),
    ]
    assert body["bot_paused"] is False


@pytest.mark.asyncio
async def test_poll_response_keys_are_exactly_the_allowlist(public):
    """The ONLY public read of a conversation. Its shape is the boundary: no notes,
    no tags, no assignee, no contact_email, no queue state, no author."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.update_conversation(
        widget.id,
        _REF,
        workspace_id="ws-1",
        state="needs_human",
        tags=["vip", "refund"],
        note={"author": "u1", "text": "known chargeback risk", "at": ""},
        contact_email="visitor@example.com",
        assignee="u1",
        bot_paused=True,
        last_owner_at=datetime.now().isoformat(),
    )
    await store.add_owner_message(widget.id, _REF, "On it", author="u1", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    body = res.json()
    assert set(body) == {"messages", "bot_paused"}
    assert set(body["messages"][0]) == {"role", "content", "at"}
    # None of the owner-only data appears anywhere in the serialized response.
    raw = res.text
    for leak in ("vip", "refund", "chargeback", "visitor@example.com", "needs_human", "u1"):
        assert leak not in raw, f"{leak!r} leaked to a public read"


@pytest.mark.asyncio
async def test_poll_never_echoes_the_visitors_own_line(public):
    """A muted-turn visitor line lives in the same table; the visitor already has
    their own words, and a public read that serves stored visitor content is one
    bug away from serving someone else's."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.add_owner_message(
        widget.id, _REF, "my own question", role=OwnerMessageRole.VISITOR, workspace_id="ws-1"
    )
    await store.add_owner_message(widget.id, _REF, "the answer", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    assert [m["content"] for m in res.json()["messages"]] == ["the answer"]


@pytest.mark.asyncio
async def test_poll_after_is_strictly_greater_than(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    first = await store.add_owner_message(widget.id, _REF, "one", workspace_id="ws-1")
    await store.add_owner_message(widget.id, _REF, "two", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(after=first.created_at),
        headers={"Origin": _ORIGIN},
    )

    assert [m["content"] for m in res.json()["messages"]] == ["two"]


@pytest.mark.asyncio
async def test_poll_accepts_a_javascript_style_cursor(public):
    """A browser round-trips the timestamp through Date.toISOString() and hands
    back a '…Z' spelling. Compared raw, 'Z' sorts after a fractional second and the
    poll would skip the very message it just delivered."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    first = await store.add_owner_message(widget.id, _REF, "one", workspace_id="ws-1")
    await store.add_owner_message(widget.id, _REF, "two", workspace_id="ws-1")
    js_cursor = (
        datetime.fromisoformat(first.created_at).astimezone(UTC).isoformat().replace("+00:00", "Z")
    )

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(after=js_cursor),
        headers={"Origin": _ORIGIN},
    )

    assert [m["content"] for m in res.json()["messages"]] == ["two"]


@pytest.mark.asyncio
async def test_poll_ignores_a_malformed_cursor(public):
    """A bad cursor costs a duplicate render, never a stalled thread."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.add_owner_message(widget.id, _REF, "one", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(after="not-a-timestamp"),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200
    assert [m["content"] for m in res.json()["messages"]] == ["one"]


@pytest.mark.asyncio
async def test_poll_caps_the_page(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    for i in range(55):
        await store.add_owner_message(widget.id, _REF, f"line {i}", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    messages = res.json()["messages"]
    assert len(messages) == 50
    # The newest window, still oldest-first inside it.
    assert messages[0]["content"] == "line 5"
    assert messages[-1]["content"] == "line 54"


@pytest.mark.asyncio
async def test_poll_reports_the_mute(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await _paused_conversation(store, widget.id)

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    assert res.json()["bot_paused"] is True


@pytest.mark.asyncio
async def test_poll_does_not_cross_to_a_sibling_visitor(public):
    """Another visitor's thread on the same widget never appears in this one."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.add_owner_message(widget.id, "cust-0002", "not yours", workspace_id="ws-1")
    await store.add_owner_message(widget.id, _REF, "yours", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    assert [m["content"] for m in res.json()["messages"]] == ["yours"]


@pytest.mark.asyncio
async def test_poll_malformed_ref_is_400(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    res = await c.get(
        f"/paw-bar/messages/{widget.id}/no",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_poll_unknown_widget_is_404(public):
    c, _store = public
    await _site()
    res = await c.get(
        f"/paw-bar/messages/nope/{_REF}", params=_poll_params(), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_poll_rate_limit_is_429(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget(rate_limit_per_min=2))
    for _ in range(2):
        await store.record_event(
            PawBarEvent(widget_id=widget.id, type="concierge_message", customer_ref=_REF)
        )
    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_poll_bad_key_is_401(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(signed_key="short"),
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_poll_wrong_origin_is_403(public):
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_poll_widget_bound_to_a_sibling_pocket_is_403(public):
    """A key for pocket A must not drive a widget for pocket B."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget(pocket_id="pocket-2"))
    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_poll_from_our_own_frame_without_an_origin_header(public):
    """Same-origin GETs carry no Origin header — the frame's own poll must work, or
    the visitor never sees the owner's reply (found live on the 2026-07-30 rig)."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.add_owner_message(widget.id, _REF, "hello", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Sec-Fetch-Site": "same-origin"},
    )

    assert res.status_code == 200
    assert [m["content"] for m in res.json()["messages"]] == ["hello"]


@pytest.mark.asyncio
async def test_poll_is_cross_workspace_scoped(public, monkeypatch):
    """A row stamped with a foreign tenant never surfaces on this key's poll."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await store.add_owner_message(widget.id, _REF, "foreign", workspace_id="ws-2")
    await store.add_owner_message(widget.id, _REF, "ours", workspace_id="ws-1")

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    assert [m["content"] for m in res.json()["messages"]] == ["ours"]


# --------------------------------------------------------------------------- #
# Layer 4 — idle auto-resume (4h, computed on read)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_idle_mute_reads_as_unpaused(store):
    """No sweeper: the row itself reports the hand-back the moment it is due."""
    await _paused_conversation(store, "w1")
    assert (await store.get_conversation("w1", _REF, "ws-1")).bot_paused is True

    await _age_the_pause(store, "w1", BOT_PAUSE_IDLE_HOURS + 1)

    assert (await store.get_conversation("w1", _REF, "ws-1")).bot_paused is False


@pytest.mark.asyncio
async def test_a_fresh_mute_does_not_resume(store):
    """The guard must not fire while the owner is mid-conversation."""
    await _paused_conversation(store, "w1")
    await _age_the_pause(store, "w1", BOT_PAUSE_IDLE_HOURS - 1)

    assert (await store.get_conversation("w1", _REF, "ws-1")).bot_paused is True
    assert await store.auto_resume_bot_if_idle("w1", _REF, "ws-1") is None


@pytest.mark.asyncio
async def test_auto_resume_writes_one_system_message(store):
    """The thread explains itself — and says it once, however many readers race."""
    await _paused_conversation(store, "w1")
    await _age_the_pause(store, "w1", BOT_PAUSE_IDLE_HOURS + 1)

    first = await store.auto_resume_bot_if_idle("w1", _REF, "ws-1")
    second = await store.auto_resume_bot_if_idle("w1", _REF, "ws-1")

    assert first is not None
    assert first.role is OwnerMessageRole.SYSTEM
    assert first.content == BOT_RESUME_SYSTEM_MESSAGE
    assert second is None
    lines = await store.list_owner_messages("w1", _REF, workspace_id="ws-1")
    assert [m.content for m in lines] == [BOT_RESUME_SYSTEM_MESSAGE]
    stored = await store.get_conversation("w1", _REF, "ws-1")
    assert stored.bot_paused is False
    assert stored.bot_paused_at == ""


@pytest.mark.asyncio
async def test_owner_activity_restarts_the_idle_clock(client):
    """Replying again is owner activity — the window measures from the last one."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await _paused_conversation(store, widget.id)
    await _age_the_pause(store, widget.id, BOT_PAUSE_IDLE_HOURS + 1)

    await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "Still here."},
    )

    assert (await store.get_conversation(widget.id, _REF, "ws-1")).bot_paused is True


@pytest.mark.asyncio
async def test_the_bot_answers_again_after_the_idle_window(public, monkeypatch):
    """End to end: the visitor who comes back after 4h of silence gets the bot, and
    the thread carries the line explaining why."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    fake_exec = _stub_run_dispatch(monkeypatch)
    await _paused_conversation(store, widget.id)
    await _age_the_pause(store, widget.id, BOT_PAUSE_IDLE_HOURS + 1)

    res = await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert "event: chunk" in res.text
    assert "event: human_replying" not in res.text
    assert len(fake_exec.submitted) == 1
    system = [
        m
        for m in await store.list_owner_messages(widget.id, _REF, workspace_id="ws-1")
        if m.role is OwnerMessageRole.SYSTEM
    ]
    assert [m.content for m in system] == [BOT_RESUME_SYSTEM_MESSAGE]


@pytest.mark.asyncio
async def test_the_poll_agrees_with_chat_about_an_expired_mute(public):
    """Both surfaces apply the same rule, so the visitor is never told a human is
    replying by one endpoint and served a bot by the other."""
    c, store = public
    await _site()
    widget = await store.create_widget(_widget())
    await _paused_conversation(store, widget.id)
    await _age_the_pause(store, widget.id, BOT_PAUSE_IDLE_HOURS + 1)

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params=_poll_params(),
        headers={"Origin": _ORIGIN},
    )

    body = res.json()
    assert body["bot_paused"] is False
    assert [m["content"] for m in body["messages"]] == [BOT_RESUME_SYSTEM_MESSAGE]


# --------------------------------------------------------------------------- #
# Layer 5 — the merged transcript
# --------------------------------------------------------------------------- #


async def _transcript(c, site_id: str) -> list[tuple[str, str]]:
    res = await c.get(f"/paw-bar/admin/site/{site_id}/conversations/{_REF}")
    assert res.status_code == 200
    return [(m["role"], m["content"]) for m in res.json()["messages"]]


@pytest.mark.asyncio
async def test_transcript_interleaves_all_four_roles_by_timestamp(client):
    """The whole value of this view is seeing WHEN the human stepped in relative to
    what the bot had been saying."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    base = datetime.now(UTC) - timedelta(hours=1)
    await _mk_run(
        createdAt=base,
        ended_at=base + timedelta(seconds=5),
        user_text="Do you deliver?",
        partial_text="We do!",
    )

    async def _line(offset_s: int, content: str, role):
        message = await store.add_owner_message(
            widget.id, _REF, content, role=role, workspace_id="ws-1"
        )
        # Rewrite the stamp so the ordering under test is deterministic rather
        # than a race against wall-clock microseconds.
        import aiosqlite

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE paw_bar_owner_messages SET created_at = ? WHERE id = ?",
                ((base + timedelta(seconds=offset_s)).isoformat(), message.id),
            )
            await db.commit()

    await _line(10, "When?", OwnerMessageRole.VISITOR)
    await _line(20, "Tomorrow — I'll sort it.", OwnerMessageRole.OWNER)
    await _line(30, BOT_RESUME_SYSTEM_MESSAGE, OwnerMessageRole.SYSTEM)

    assert await _transcript(c, str(site.id)) == [
        ("user", "Do you deliver?"),
        ("assistant", "We do!"),
        ("user", "When?"),
        ("owner", "Tomorrow — I'll sort it."),
        ("system", BOT_RESUME_SYSTEM_MESSAGE),
    ]


@pytest.mark.asyncio
async def test_transcript_still_renders_an_assistant_only_thread(client):
    """Landmine 3: a site with transcript storage off is a NORMAL state, and the
    merge must not have made it a broken one."""
    c, store = client
    site = await _site(concierge_store_transcripts=False)
    await store.create_widget(_widget())
    await _mk_run(user_text="", partial_text="We open at 8.")

    assert await _transcript(c, str(site.id)) == [("assistant", "We open at 8.")]


@pytest.mark.asyncio
async def test_transcript_messages_carry_no_ids(client):
    """Landmine 4: run-derived messages have no id, so the DTO must not grow one —
    half the thread could never satisfy it."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.add_owner_message(widget.id, _REF, "hi", workspace_id="ws-1")

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_REF}")

    for message in res.json()["messages"]:
        assert set(message) == {"role", "content", "created_at"}


@pytest.mark.asyncio
async def test_transcript_does_not_show_another_visitors_owner_lines(client):
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.add_owner_message(widget.id, "cust-0002", "for someone else", workspace_id="ws-1")
    await store.add_owner_message(widget.id, _REF, "for you", workspace_id="ws-1")

    assert ("owner", "for someone else") not in await _transcript(c, str(site.id))
    assert ("owner", "for you") in await _transcript(c, str(site.id))


@pytest.mark.asyncio
async def test_transcript_is_cross_workspace_refused(mongo_db, store, monkeypatch):
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    await store.add_owner_message(widget.id, _REF, "private", workspace_id="ws-1")

    app = _build_app(store, monkeypatch, role="admin", workspace_id="ws-2")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_REF}")

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_transcript_survives_an_owner_message_store_failure(client, monkeypatch):
    """Best-effort on the new half: the run-derived transcript still renders."""
    c, store = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run()

    async def _boom(*_a, **_kw):
        raise RuntimeError("sqlite is having a moment")

    monkeypatch.setattr(store, "list_owner_messages", _boom)

    assert await _transcript(c, str(site.id)) == [("assistant", "We open at 8.")]
