# tests/cloud/test_paw_bar_inbox_freshness.py — the owner inbox tells the truth
# about what was said LAST, and says WHO said it.
#
# Created 2026-08-24. Two defects, one root cause: the conversation list is built
# purely from ``ChatRunDoc`` (see ``_list_conversations``), and the lines that have
# no run — an owner's takeover reply, and a visitor's answer that arrived while the
# bot was muted — live in ``paw_bar_owner_messages`` instead. So the moment a human
# stepped in, the row froze: it kept showing the bot's last sentence, stamped at
# the bot's last timestamp, and sank down a list ordered by run recency while the
# actual conversation was the most active one on the site.
#
# Layers:
#   * Freshness — an owner reply and a muted visitor line each become the row's
#     preview + last_message_at; a stale run never wins over a newer line.
#   * Ordering — rows sort by the latest LINE, not the latest run.
#   * Identity — an owner transcript line carries who typed it (name + avatar),
#     resolved from the stored author id, on the transcript AND on the reply echo.
#   * Privacy — the VISITOR's public poll still carries no operator identity.

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import (
    OwnerMessageRole,
    PawBarBlock,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_KEY = "site_key_" + "a" * 24
_REF = "cust-0001"
_REF_2 = "cust-0002"


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
            pocket_id="pocket-1",
            blocks=[PawBarBlock(type="text", content="Hi")],
        ),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _mk_run(ref: str = _REF, **ov: Any):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    d = dict(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key=f"cloud:concierge:pocket-1:{ref}:agent-xyz",
        user_id=ref,
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


_USER_ID = PydanticObjectId()


async def _mk_user(user_id: PydanticObjectId = _USER_ID, **ov: Any):
    """A real ``User`` document, so the author lookup has something to resolve."""
    from pocketpaw_ee.cloud.models.user import User

    d = dict(
        id=user_id,
        email="maya@brewco.com",
        hashed_password="x",
        full_name="Maya Oyelaran",
        avatar="/uploads/avatars/maya.png",
    )
    d.update(ov)
    u = User(**d)
    await u.insert()
    return u


def _fake_user(role: str = "admin", workspace_id: str = "ws-1", user_id: Any = _USER_ID):
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
    return PawBarStore(tmp_path / "freshness.db")


@pytest_asyncio.fixture
async def client(mongo_db, store, monkeypatch):
    app = _build_app(store, monkeypatch, role="admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


def _row_for(body: dict, ref: str = _REF) -> dict:
    for item in body["items"]:
        if item["customer_ref"] == ref:
            return item
    raise AssertionError(f"no row for {ref} in {body['items']}")


# --------------------------------------------------------------------------- #
# Layer 1 — freshness: the row shows what was actually said last
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_owner_reply_becomes_the_row_preview(client):
    """The owner takes over; the list row must follow the conversation.

    Before: the row kept the bot's "We open at 8." at the bot's timestamp, so an
    inbox open next to the thread disagreed with it about what had been said.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    reply = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "Maya here, we open at 7 on Saturdays.", "conversation_id": conversation.id},
    )
    assert reply.status_code == 200, reply.text

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    assert res.status_code == 200
    row = _row_for(res.json())
    assert row["preview"] == "Maya here, we open at 7 on Saturdays."
    assert row["last_message_at"] >= reply.json()["message"]["created_at"]


@pytest.mark.asyncio
async def test_muted_visitor_line_becomes_the_row_preview(client):
    """A visitor answering a human dispatches no run — the row must still move."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.add_owner_message(
        widget.id, _REF, "Maya here.", conversation_id=conversation.id, workspace_id="ws-1"
    )
    await store.add_owner_message(
        widget.id,
        _REF,
        "Perfect, see you at 7.",
        conversation_id=conversation.id,
        role=OwnerMessageRole.VISITOR,
        workspace_id="ws-1",
    )

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    row = _row_for(res.json())
    assert row["preview"] == "Perfect, see you at 7."


@pytest.mark.asyncio
async def test_a_newer_run_still_wins_over_an_older_owner_line(client):
    """The merge takes the LATEST line, whichever store it came from."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.add_owner_message(
        widget.id,
        _REF,
        "An hour ago I said this.",
        conversation_id=conversation.id,
        workspace_id="ws-1",
    )
    # The run is stamped AFTER the owner line, so the run's text is the newest.
    await _mk_run(
        partial_text="The bot answered after that.",
        createdAt=datetime.now(UTC) + timedelta(minutes=5),
    )

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    row = _row_for(res.json())
    assert row["preview"] == "The bot answered after that."


@pytest.mark.asyncio
async def test_system_lines_never_become_the_row_preview(client):
    """The bot handing itself back is the product narrating, not a party speaking.

    Letting it preview the row replaces what a person actually said with
    boilerplate — and re-sorts the whole queue on an automatic event.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.add_owner_message(
        widget.id,
        _REF,
        "See you Saturday.",
        conversation_id=conversation.id,
        workspace_id="ws-1",
    )
    await store.add_owner_message(
        widget.id,
        _REF,
        "The assistant is answering again.",
        conversation_id=conversation.id,
        role=OwnerMessageRole.SYSTEM,
        workspace_id="ws-1",
    )

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    assert _row_for(res.json())["preview"] == "See you Saturday."


@pytest.mark.asyncio
async def test_each_row_previews_its_own_visitors_line(client):
    """Two live conversations, two different last sentences. Neither borrows the
    other's — the batched read is one query for a page, not one bucket."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run(_REF, createdAt=datetime.now(UTC) - timedelta(hours=1))
    await _mk_run(_REF_2, createdAt=datetime.now(UTC) - timedelta(hours=1))
    first = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    second = await store.upsert_conversation_on_visitor_turn(widget.id, _REF_2, "ws-1")
    await store.add_owner_message(
        widget.id, _REF, "Told to 0001.", conversation_id=first.id, workspace_id="ws-1"
    )
    await store.add_owner_message(
        widget.id, _REF_2, "Told to 0002.", conversation_id=second.id, workspace_id="ws-1"
    )

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()
    assert _row_for(body, _REF)["preview"] == "Told to 0001."
    assert _row_for(body, _REF_2)["preview"] == "Told to 0002."


@pytest.mark.asyncio
async def test_a_busy_widget_does_not_starve_the_page_of_its_own_lines(client, monkeypatch):
    """The visitor filter is what keeps the bounded read ON the page.

    Without it the scan cap is spent on whoever spoke most recently ANYWHERE on
    the widget, and the rows actually being listed silently fall back to the run
    preview — the exact staleness this whole merge exists to remove, returning
    only on the busy sites where it matters most. The cap is patched down rather
    than writing 400 rows: the interaction is the point, not the number.
    """
    from pocketpaw_ee.paw_bar import router as paw_bar_router

    monkeypatch.setattr(paw_bar_router, "_OUT_OF_BAND_SCAN_CAP", 2)
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    # Only _REF has runs, so only _REF is on the page.
    await _mk_run(_REF, createdAt=datetime.now(UTC) - timedelta(hours=1))
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.add_owner_message(
        widget.id,
        _REF,
        "The line this row should show.",
        conversation_id=conversation.id,
        workspace_id="ws-1",
    )
    # Newer chatter from visitors who are NOT on this page — enough to fill the cap.
    for i in range(3):
        noisy = f"cust-noise-{i}"
        other = await store.upsert_conversation_on_visitor_turn(widget.id, noisy, "ws-1")
        await store.add_owner_message(
            widget.id, noisy, f"noise {i}", conversation_id=other.id, workspace_id="ws-1"
        )

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    assert _row_for(res.json())["preview"] == "The line this row should show."


# --------------------------------------------------------------------------- #
# Layer 2 — ordering follows the latest line
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rows_sort_by_the_latest_line_not_the_latest_run(client):
    """A human-held conversation is the most active one — it must sort first.

    Ordering by run recency buried exactly the conversations a human was working,
    because taking over is what stops the runs.
    """
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())

    # cust-0001: an OLD run, then a fresh owner reply.
    await _mk_run(_REF, createdAt=datetime.now(UTC) - timedelta(hours=2))
    older = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.add_owner_message(
        widget.id, _REF, "Still with you.", conversation_id=older.id, workspace_id="ws-1"
    )

    # cust-0002: a run from an hour ago and nothing since.
    await _mk_run(_REF_2, createdAt=datetime.now(UTC) - timedelta(hours=1))
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF_2, "ws-1")

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    refs = [item["customer_ref"] for item in res.json()["items"]]
    assert refs[0] == _REF, "the conversation a human is holding sank below a quiet one"


# --------------------------------------------------------------------------- #
# Layer 3 — who typed it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transcript_owner_line_names_the_human_who_typed_it(client):
    """A team inbox needs more than "someone replied" — the thread says who."""
    c, store = client
    await _mk_user()
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    posted = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "On it.", "conversation_id": conversation.id},
    )
    assert posted.status_code == 200, posted.text

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": conversation.id},
    )
    assert res.status_code == 200
    owner_lines = [m for m in res.json()["messages"] if m["role"] == "owner"]
    assert owner_lines, "the owner's reply is missing from the transcript"
    assert owner_lines[0]["author_name"] == "Maya Oyelaran"
    assert owner_lines[0]["author_avatar"] == "/uploads/avatars/maya.png"
    assert owner_lines[0]["author_id"] == str(_USER_ID)


@pytest.mark.asyncio
async def test_reply_echo_carries_the_author_identity(client):
    """The composer's own echo renders identically to the refetched line."""
    c, store = client
    await _mk_user()
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")

    res = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "On it.", "conversation_id": conversation.id},
    )
    message = res.json()["message"]
    assert message["author_name"] == "Maya Oyelaran"
    assert message["author_avatar"] == "/uploads/avatars/maya.png"


@pytest.mark.asyncio
async def test_only_owner_lines_are_attributed(client):
    """A visitor's muted line carries no author and a system line was authored by
    nobody. Stamping either with whoever is in the map invents a speaker."""
    c, store = client
    await _mk_user()
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    # An OWNER line first, so the resolved-author map is actually populated —
    # without one there is no identity in scope and the gate has nothing to leak.
    posted = await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "Maya here.", "conversation_id": conversation.id},
    )
    assert posted.status_code == 200, posted.text
    # Written with an author on purpose: the role, not the column, is the gate.
    await store.add_owner_message(
        widget.id,
        _REF,
        "I asked this while a human was holding the thread.",
        conversation_id=conversation.id,
        role=OwnerMessageRole.VISITOR,
        author=str(_USER_ID),
        workspace_id="ws-1",
    )
    await store.add_owner_message(
        widget.id,
        _REF,
        "The assistant is answering again.",
        conversation_id=conversation.id,
        role=OwnerMessageRole.SYSTEM,
        author=str(_USER_ID),
        workspace_id="ws-1",
    )

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": conversation.id},
    )
    for message in res.json()["messages"]:
        if message["role"] == "owner":
            continue
        assert message["author_id"] == ""
        assert message["author_name"] == ""
        assert message["author_avatar"] == ""


@pytest.mark.asyncio
async def test_an_unresolvable_author_degrades_to_a_blank_name(client):
    """A deleted teammate (or a legacy line with no author) renders anonymously."""
    c, store = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await store.add_owner_message(
        widget.id,
        _REF,
        "Written before authors were stored.",
        conversation_id=conversation.id,
        workspace_id="ws-1",
    )

    res = await c.get(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}",
        params={"conversation_id": conversation.id},
    )
    owner_lines = [m for m in res.json()["messages"] if m["role"] == "owner"]
    assert owner_lines[0]["author_name"] == ""
    assert owner_lines[0]["author_avatar"] == ""


# --------------------------------------------------------------------------- #
# Layer 4 — the visitor still learns nothing about the operator
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_visitor_poll_never_carries_the_operator_identity(client):
    """The public read stays role + content + time. Widening it here would leak a
    staff name and photo to every stranger on the internet."""
    c, store = client
    await _mk_user()
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_run()
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, "ws-1")
    await c.post(
        f"/paw-bar/admin/site/{site.id}/conversations/{_REF}/reply",
        json={"text": "Maya here.", "conversation_id": conversation.id},
    )

    res = await c.get(
        f"/paw-bar/messages/{widget.id}/{_REF}",
        params={"signed_key": _KEY},
        headers={"Origin": "https://brewco.com"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["messages"], "the owner line should reach the visitor"
    for message in res.json()["messages"]:
        assert set(message) == {"role", "content", "at"}
