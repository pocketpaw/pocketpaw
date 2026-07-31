# tests/cloud/test_paw_bar_escape_hatch.py — "talk to a human" (owner inbox, slice 3).
# Created 2026-07-31: the handoffs endpoint has existed since D2 and has always
# returned []. These tests are the proof that it doesn't anymore, and that the
# escape hatch behind it holds in the states where support products usually lose
# the customer. Layers, in the order the feature fails if any one is wrong:
#   * The PRODUCER: one call writes the ``_paw_handoffs`` Fabric object the
#     existing read consumes AND flips the conversation to needs_human, records
#     an audit marker, and notifies the owner. Partial failure is covered too — a
#     dead Fabric store still escalates, and both dead is the only refusal.
#   * The always-available path: POST /paw-bar/request-human works while the bot
#     is answering confidently AND while a human holds the thread, and the owner
#     read returns the row that came out of it.
#   * The AGENT path: a built-in ``pawbar_request_human`` tool exists on every
#     concierge run bound to a widget (declared actions or not), calls the same
#     producer, and CANNOT perform any tenant-scoped effect through it.
#   * Notifications: all three triggers fire to the workspace owner, and a
#     notifier that raises never costs a visitor their turn.
#   * Tenancy: every new surface refuses cross-workspace.

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.paw_bar.handoff import (
    HANDOFF_MARKER_TYPE,
    HANDOFFS_PER_MIN,
    PAW_HANDOFFS_TYPE,
    raise_handoff,
)

from pocketpaw.paw_bar.models import (
    ConversationState,
    PawBarActionSpec,
    PawBarBlock,
    PawBarCatalogItem,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_KEY = "site_key_" + "b" * 24
_ORIGIN = "https://brewco.com"
_REF = "cust-0001"
_OWNER_USER = "user-owner-1"


# --------------------------------------------------------------------------- #
# Builders
#
# Unlike the sibling paw-bar suites, this file uses a REAL ``Workspace`` document
# and its real ObjectId string as the workspace id everywhere. The owner
# notification resolves its recipient by loading that document, so a hand-picked
# "ws-1" handle would silently resolve to nobody and every notification assertion
# below would pass by testing nothing.
# --------------------------------------------------------------------------- #


async def _workspace(owner: str = _OWNER_USER):
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="Brew & Co", slug=f"brewco-{uuid.uuid4().hex[:8]}", owner=owner)
    await ws.insert()
    return ws


async def _site(workspace_id: str, **ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace=workspace_id,
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


def _widget(workspace_id: str, **ov: Any) -> PawBarWidget:
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
        workspace_id=workspace_id,
    )
    d.update(ov)
    return PawBarWidget(**d)


def _selling_widget(workspace_id: str) -> PawBarWidget:
    """A widget that DOES declare actions — the C1 shape, so the escape hatch is
    proven to coexist with declared verbs rather than only existing without them."""
    return _widget(
        workspace_id,
        spec=PawBarSpec(
            widget_id="pp_seed",
            pocket_id="pocket-1",
            blocks=[PawBarBlock(type="text", content="Hi")],
            actions=[
                PawBarActionSpec(verb="add_to_cart", policy="auto", args={"product_id": "str"}),
                PawBarActionSpec(verb="book_table", policy="gated", args={"name": "str"}),
            ],
            catalog=[PawBarCatalogItem(id="espresso", name="Espresso", price_cents=350)],
        ),
    )


def _fake_user(role: str, workspace_id: str, user_id: str = "u1") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


def _admin_app(store, fabric, monkeypatch, *, role: str = "admin", workspace_id: str):
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
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    return app


def _public_app(store, fabric, monkeypatch):
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    return app


def _request_human(widget_id: str, **ov) -> dict:
    p = dict(key=_KEY, w=widget_id, customer_ref=_REF, message="Can I speak to someone?")
    p.update(ov)
    return p


def _chat_payload(widget_id: str, **ov) -> dict:
    p = dict(widget_id=widget_id, signed_key=_KEY, customer_ref=_REF, message="Are you open?")
    p.update(ov)
    return p


class _FakeExecutor:
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


def _stub_run_dispatch(monkeypatch) -> _FakeExecutor:
    from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

    transport = InMemoryStreamTransport()
    fake_exec = _FakeExecutor(transport)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: fake_exec)

    async def _fake_create_run(spec):
        return SimpleNamespace(run_id=spec.run_id)

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)
    return fake_exec


async def _notifications(workspace_id: str) -> list:
    from pocketpaw_ee.cloud.models.notification import Notification

    return await Notification.find(Notification.workspace == workspace_id).to_list()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "handoff.db")


@pytest_asyncio.fixture
async def fabric(tmp_path):
    from pocketpaw.fabric.store import FabricStore

    # Deliberately EMPTY — no ``_paw_handoffs`` type is pre-defined. The producer
    # has to mint it, which is what a first handoff on a real tenant does.
    return FabricStore(tmp_path / "fabric.db")


@pytest_asyncio.fixture
async def ws(mongo_db):
    return await _workspace()


@pytest_asyncio.fixture
async def public(mongo_db, store, fabric, monkeypatch):
    """PUBLIC client (no auth overrides) for the visitor-facing endpoints."""
    app = _public_app(store, fabric, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


@pytest_asyncio.fixture
async def owner(mongo_db, store, fabric, monkeypatch, ws):
    """ADMIN client in the real workspace — the owner side of the same data."""
    app = _admin_app(store, fabric, monkeypatch, workspace_id=str(ws.id))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, store


# --------------------------------------------------------------------------- #
# Layer 1 — the PRODUCER. One call, both owner-visible surfaces.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_producer_writes_the_object_the_read_consumes_and_escalates(
    mongo_db, store, fabric, monkeypatch, ws
):
    """THE centrepiece. ``raise_handoff`` writes a ``_paw_handoffs`` object with
    exactly the four properties ``get_site_handoffs`` projects, and flips the
    conversation to needs_human. Read back through the STORE's own query (not a
    hand-built object) so producer and consumer are proven to agree on the shape."""
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, str(ws.id))

    outcome = await raise_handoff(
        widget=widget,
        workspace_id=str(ws.id),
        customer_ref=_REF,
        question="I need to change my booking",
        contact="visitor@brewco.com",
        store=store,
    )

    assert outcome.ok is True
    assert outcome.escalated is True
    assert outcome.handoff_id

    from pocketpaw.fabric.models import FabricQuery

    result = await fabric.query(
        FabricQuery(type_name=PAW_HANDOFFS_TYPE, filters={"widget_id": widget.id}, limit=10),
        workspace_id=str(ws.id),
    )
    assert len(result.objects) == 1
    props = result.objects[0].properties
    assert props["widget_id"] == widget.id
    assert props["contact"] == "visitor@brewco.com"
    assert props["question"] == "I need to change my booking"
    # The transcript endpoint is keyed by customer_ref, so this is a working link.
    assert props["transcript_ref"] == _REF

    conversation = await store.get_conversation(widget.id, _REF, str(ws.id))
    assert conversation.state is ConversationState.NEEDS_HUMAN
    assert conversation.contact_email == "visitor@brewco.com"


@pytest.mark.asyncio
async def test_producer_escalates_a_legacy_conversation_with_no_row(
    mongo_db, store, fabric, monkeypatch, ws
):
    """A handoff can be the FIRST thing that happens on a conversation that
    predates the state table. ``update_conversation`` writes nothing without a
    row, so the producer must mint one — otherwise the queue never sees it."""
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))
    assert await store.get_conversation(widget.id, _REF, str(ws.id)) is None

    outcome = await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store
    )

    assert outcome.ok is True
    conversation = await store.get_conversation(widget.id, _REF, str(ws.id))
    assert conversation is not None
    assert conversation.state is ConversationState.NEEDS_HUMAN
    # Minted without touching the unread counter — a handoff is not a new message.
    assert conversation.unread_for_owner == 0


@pytest.mark.asyncio
async def test_producer_records_an_audit_marker(mongo_db, store, fabric, monkeypatch, ws):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))

    await raise_handoff(widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store)

    events = await store.recent_events(widget.id, limit=10)
    assert [e.type for e in events] == [HANDOFF_MARKER_TYPE]
    assert events[0].payload == {"source": "visitor"}


@pytest.mark.asyncio
async def test_producer_sanitizes_visitor_text(mongo_db, store, fabric, monkeypatch, ws):
    """The question lands on a surface a human reads. Control characters are
    stripped and whitespace collapsed before it is stored, the same
    defense-in-depth the decision loop applies to proposals."""
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))

    await raise_handoff(
        widget=widget,
        workspace_id=str(ws.id),
        customer_ref=_REF,
        question="help\x00\x1b[2J  me   \n now",
        store=store,
    )

    from pocketpaw.fabric.models import FabricQuery

    result = await fabric.query(
        FabricQuery(type_name=PAW_HANDOFFS_TYPE, filters={"widget_id": widget.id}, limit=1),
        workspace_id=str(ws.id),
    )
    assert result.objects[0].properties["question"] == "help[2J me now"


@pytest.mark.asyncio
async def test_producer_still_escalates_when_fabric_is_down(mongo_db, store, monkeypatch, ws):
    """Partial failure is a success. The queue and the handoffs list are two
    separate owner-visible surfaces; telling a visitor nobody can be reached while
    their conversation sits escalated in the inbox would be a lie."""

    class _DeadFabric:
        async def get_type_by_name(self, *a, **k):
            raise RuntimeError("fabric down")

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: _DeadFabric())
    widget = await store.create_widget(_widget(str(ws.id)))

    outcome = await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store
    )

    assert outcome.ok is True
    assert outcome.escalated is True
    assert outcome.handoff_id == ""
    assert (
        await store.get_conversation(widget.id, _REF, str(ws.id))
    ).state is ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_producer_refuses_only_when_both_surfaces_fail(mongo_db, store, monkeypatch, ws):
    """The control for the test above: with the state row ALSO unwritable there is
    nothing recording that the visitor asked, so the call reports failure."""

    class _DeadFabric:
        async def get_type_by_name(self, *a, **k):
            raise RuntimeError("fabric down")

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: _DeadFabric())
    widget = await store.create_widget(_widget(str(ws.id)))

    async def _boom(*a, **k):
        raise RuntimeError("sqlite down")

    monkeypatch.setattr(store, "ensure_conversation", _boom)

    outcome = await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store
    )

    assert outcome.ok is False
    assert outcome.error == "handoff_unavailable"
    assert outcome.http_status == 503


@pytest.mark.asyncio
async def test_producer_rate_limits_a_flood(mongo_db, store, fabric, monkeypatch, ws):
    """A handoff writes a durable record AND pings the owner, so the abuse shape
    is notification spam. The dedicated cap counts handoffs alone."""
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))

    for _ in range(HANDOFFS_PER_MIN):
        assert (
            await raise_handoff(
                widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store
            )
        ).ok is True

    refused = await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store
    )
    assert refused.ok is False
    assert refused.error == "handoff_rate_limit"
    assert refused.http_status == 429


# --------------------------------------------------------------------------- #
# Layer 2 — the OWNER READ. It no longer returns [].
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handoffs_read_returns_a_produced_row(owner, fabric, ws):
    """End to end through the two halves that used to be disconnected: the
    producer writes, the frozen D2 read projects it unchanged."""
    c, store = owner
    site = await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    await raise_handoff(
        widget=widget,
        workspace_id=str(ws.id),
        customer_ref=_REF,
        question="Can someone call me?",
        contact="visitor@brewco.com",
        store=store,
    )

    res = await c.get(f"/paw-bar/admin/site/{site.id}/handoffs")

    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["contact"] == "visitor@brewco.com"
    assert items[0]["question"] == "Can someone call me?"
    assert items[0]["transcript_ref"] == _REF
    assert items[0]["created_at"]


@pytest.mark.asyncio
async def test_overview_counts_the_handoff(owner, ws):
    c, store = owner
    site = await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    assert (await c.get(f"/paw-bar/admin/site/{site.id}/overview")).json()["counts"][
        "handoffs"
    ] == 0

    await raise_handoff(widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store)

    res = await c.get(f"/paw-bar/admin/site/{site.id}/overview")
    assert res.json()["counts"]["handoffs"] == 1


@pytest.mark.asyncio
async def test_handoffs_read_is_cross_workspace_refused(mongo_db, store, fabric, monkeypatch, ws):
    """A sibling tenant's admin sees nothing: the site 404s before the read, and
    the Fabric query is workspace-scoped underneath it anyway."""
    site = await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    await raise_handoff(widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store)

    other = await _workspace(owner="user-owner-2")
    app = _admin_app(store, fabric, monkeypatch, workspace_id=str(other.id))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        res = await c.get(f"/paw-bar/admin/site/{site.id}/handoffs")

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_siblings_handoff_never_appears_on_this_site(owner, fabric, ws):
    """Same tenant, two widgets — the ``widget_id`` property filter is the
    cross-site isolation seam, so one site's escalation stays on that site."""
    c, store = owner
    site = await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    sibling = await store.create_widget(_widget(str(ws.id), pocket_id="pocket-2"))

    await raise_handoff(
        widget=sibling, workspace_id=str(ws.id), customer_ref=_REF, question="other", store=store
    )
    await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, question="mine", store=store
    )

    items = (await c.get(f"/paw-bar/admin/site/{site.id}/handoffs")).json()["items"]
    assert [i["question"] for i in items] == ["mine"]


# --------------------------------------------------------------------------- #
# Layer 3 — the ALWAYS-AVAILABLE path. It does not depend on the agent.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_human_works_while_the_bot_is_unpaused(public, fabric, ws):
    """The rule that makes this an escape hatch rather than a fallback: a visitor
    can reach a person mid-conversation, while the bot is answering confidently."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    conversation = await store.upsert_conversation_on_visitor_turn(widget.id, _REF, str(ws.id))
    assert conversation.bot_paused is False

    res = await c.post(
        "/paw-bar/request-human",
        json=_request_human(widget.id, message="I want to talk to a person"),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["state"] == "needs_human"
    assert body["handoff_id"]
    assert "notified" in body["message"]
    assert (
        await store.get_conversation(widget.id, _REF, str(ws.id))
    ).state is ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_request_human_works_while_the_bot_is_paused(public, fabric, ws):
    """And in the other direction: a conversation a human already took over still
    accepts the request. Reaching a person is never state-dependent."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, str(ws.id))
    await store.update_conversation(widget.id, _REF, workspace_id=str(ws.id), bot_paused=True)

    res = await c.post(
        "/paw-bar/request-human", json=_request_human(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200
    assert res.json()["ok"] is True
    conversation = await store.get_conversation(widget.id, _REF, str(ws.id))
    assert conversation.state is ConversationState.NEEDS_HUMAN
    # The takeover is untouched — asking for a human doesn't un-mute the bot.
    assert conversation.bot_paused is True


@pytest.mark.asyncio
async def test_request_human_accepts_an_empty_message(public, fabric, ws):
    """A visitor who taps the button without typing has still asked."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    res = await c.post(
        "/paw-bar/request-human",
        json=_request_human(widget.id, message=""),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200
    assert res.json()["ok"] is True


@pytest.mark.asyncio
async def test_request_human_screens_injection(public, fabric, monkeypatch, ws):
    """The note reaches a human's screen, so it goes through the same screen a
    chat message does."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    monkeypatch.setattr(
        "pocketpaw_ee.paw_bar.router._screen_message_for_injection",
        lambda *a, **k: _false(),
    )

    res = await c.post(
        "/paw-bar/request-human", json=_request_human(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 400
    assert await store.get_conversation(widget.id, _REF, str(ws.id)) is None


async def _false() -> bool:
    return False


@pytest.mark.asyncio
async def test_request_human_refuses_a_malformed_contact(public, fabric, ws):
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    res = await c.post(
        "/paw-bar/request-human",
        json=_request_human(widget.id, contact="not-an-email"),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 422
    assert await store.get_conversation(widget.id, _REF, str(ws.id)) is None


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"customer_ref": "no"}, 400),
        ({"w": "pp_nope"}, 404),
        ({"key": "site_key_" + "z" * 24}, 401),
    ],
)
@pytest.mark.asyncio
async def test_request_human_front_gate_refusals(public, fabric, ws, payload, status):
    """The shared fail-closed chain, in the order it is checked."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    res = await c.post(
        "/paw-bar/request-human",
        json=_request_human(widget.id, **payload),
        headers={"Origin": _ORIGIN},
    )
    assert res.status_code == status


@pytest.mark.asyncio
async def test_request_human_refuses_a_disallowed_origin(public, fabric, ws):
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    res = await c.post(
        "/paw-bar/request-human",
        json=_request_human(widget.id),
        headers={"Origin": "https://evil.example"},
    )
    assert res.status_code == 403
    assert await store.get_conversation(widget.id, _REF, str(ws.id)) is None


@pytest.mark.asyncio
async def test_request_human_is_cross_workspace_refused(public, fabric, ws):
    """The key belongs to this workspace's site; a widget stamped for a SIBLING
    tenant must not be drivable with it."""
    c, store = public
    await _site(str(ws.id))
    other = await _workspace(owner="user-owner-2")
    widget = await store.create_widget(_widget(str(other.id)))

    res = await c.post(
        "/paw-bar/request-human", json=_request_human(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 403
    assert res.json()["detail"] == "widget_workspace_mismatch"
    assert await store.get_conversation(widget.id, _REF, str(other.id)) is None


@pytest.mark.asyncio
async def test_request_human_respects_the_concierge_kill_switch(public, fabric, ws):
    """An owner who turned the concierge off has turned the whole bar off."""
    c, store = public
    await _site(str(ws.id), concierge_enabled=False)
    widget = await store.create_widget(_widget(str(ws.id)))

    res = await c.post(
        "/paw-bar/request-human", json=_request_human(widget.id), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_visitor_request_shows_up_on_the_owner_read(mongo_db, store, fabric, monkeypatch, ws):
    """The whole loop in one test: the visitor asks over the public endpoint, the
    owner's dashboard read returns it."""
    site = await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))

    public_app = _public_app(store, fabric, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=public_app), base_url="http://t") as vc:
        res = await vc.post(
            "/paw-bar/request-human",
            json=_request_human(widget.id, message="I want to talk to a person"),
            headers={"Origin": _ORIGIN},
        )
    assert res.status_code == 200

    owner_app = _admin_app(store, fabric, monkeypatch, workspace_id=str(ws.id))
    async with AsyncClient(transport=ASGITransport(app=owner_app), base_url="http://t") as oc:
        items = (await oc.get(f"/paw-bar/admin/site/{site.id}/handoffs")).json()["items"]
        conversations = (await oc.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()

    assert [i["question"] for i in items] == ["I want to talk to a person"]
    assert conversations["counts"]["needs_human"] == 1


# --------------------------------------------------------------------------- #
# Layer 4 — the AGENT path. Zero authority, always present.
# --------------------------------------------------------------------------- #


def _concierge_meta(widget_id: str = "pp_1", actions: list[dict] | None = None):
    from pocketpaw_ee.cloud.surface.domain import SurfaceMeta

    return SurfaceMeta(route_path="/paw-bar", widget_id=widget_id, pawbar_actions=actions)


def test_the_handoff_tool_is_allowed_for_a_widget_with_no_actions():
    """The escape hatch does not depend on what the owner configured: a site that
    declares nothing still gets exactly one tool, and it is this one."""
    from pocketpaw_ee.agent.mcp_servers.pawbar import handoff_tool_id
    from pocketpaw_ee.cloud.surface import resolve_profile
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind

    profile = resolve_profile(SurfaceKind.CONCIERGE, _concierge_meta())

    assert profile.allow_mcp_tool_ids == frozenset({handoff_tool_id()})


def test_the_handoff_tool_coexists_with_declared_verbs():
    from pocketpaw_ee.agent.mcp_servers.pawbar import handoff_tool_id, pawbar_tool_id
    from pocketpaw_ee.cloud.surface import resolve_profile
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind

    profile = resolve_profile(
        SurfaceKind.CONCIERGE,
        _concierge_meta(actions=[{"verb": "book_table", "policy": "gated", "args": {}}]),
    )

    assert profile.allow_mcp_tool_ids == frozenset(
        {handoff_tool_id(), pawbar_tool_id("book_table")}
    )


def test_an_unbound_concierge_run_gets_no_tools():
    """No widget means no inbox to escalate into — the surface stays deny-all."""
    from pocketpaw_ee.cloud.surface import resolve_profile
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta

    profile = resolve_profile(SurfaceKind.CONCIERGE, SurfaceMeta(route_path="/paw-bar"))

    assert profile.allow_mcp_tool_ids == frozenset()


def test_the_run_context_carries_the_handoff_for_a_no_actions_concierge_run():
    """``run_core`` used to return None for a concierge widget with no declared
    actions, so no server was built at all. It must now build one."""
    from pocketpaw_ee.cloud.chat.runs.run_core import _pawbar_run_from_ctx
    from pocketpaw_ee.cloud.surface.domain import SurfaceContext, SurfaceKind

    ctx = SimpleNamespace(
        surface_context=SurfaceContext(
            workspace_id="w",
            user_id="u",
            kind=SurfaceKind.CONCIERGE,
            meta=_concierge_meta(),
            preamble="",
        )
    )
    assert _pawbar_run_from_ctx(ctx) == {"widget_id": "pp_1", "actions": [], "handoff": True}


def test_a_non_concierge_run_carries_no_handoff_context():
    """The one guard that keeps this off every other surface."""
    from pocketpaw_ee.cloud.chat.runs.run_core import _pawbar_run_from_ctx
    from pocketpaw_ee.cloud.surface.domain import SurfaceContext, SurfaceKind

    ctx = SimpleNamespace(
        surface_context=SurfaceContext(
            workspace_id="w",
            user_id="u",
            kind=SurfaceKind.POCKET,
            meta=_concierge_meta(),
            preamble="",
        )
    )
    assert _pawbar_run_from_ctx(ctx) is None


def test_the_server_builds_only_the_handoff_tool_for_a_no_actions_run(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import pawbar as pawbar_mcp

    monkeypatch.setattr(
        pawbar_mcp, "_run_context", lambda: {"widget_id": "pp_1", "actions": [], "handoff": True}
    )
    assert pawbar_mcp.pawbar_tool_ids() == (pawbar_mcp.handoff_tool_id(),)
    built = pawbar_mcp.build_pawbar_actions_server()
    assert built is not None
    assert built[0] == pawbar_mcp.SERVER_NAME


def test_a_declared_request_human_verb_is_not_registered_twice(monkeypatch):
    """A widget that (oddly) declared its own ``request_human`` keeps that verb's
    semantics, and the built-in is skipped — one tool name, never two."""
    from pocketpaw_ee.agent.mcp_servers import pawbar as pawbar_mcp

    monkeypatch.setattr(
        pawbar_mcp,
        "_run_context",
        lambda: {
            "widget_id": "pp_1",
            "actions": [{"verb": "request_human", "policy": "gated", "args": {}}],
            "handoff": True,
        },
    )
    ids = pawbar_mcp.pawbar_tool_ids()
    assert ids == (pawbar_mcp.handoff_tool_id(),), "exactly one, not a duplicate pair"


@pytest.mark.asyncio
async def test_the_agent_tool_raises_a_real_handoff(mongo_db, store, fabric, monkeypatch, ws):
    """The agent path and the visitor path are the SAME producer, so the object
    the owner reads is identical whichever one raised it."""
    from pocketpaw_ee.agent.mcp_servers import pawbar as pawbar_mcp

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda *a, **k: store)
    monkeypatch.setattr(
        pawbar_mcp, "_run_context", lambda: {"widget_id": widget.id, "actions": [], "handoff": True}
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", lambda: str(ws.id)
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.current_user_id", lambda: _REF)

    result = await pawbar_mcp._run_handoff({"reason": "wants to speak to the owner"})

    assert result.get("is_error") is not True
    assert "notified" in result["content"][0]["text"]
    from pocketpaw.fabric.models import FabricQuery

    objects = (
        await fabric.query(
            FabricQuery(type_name=PAW_HANDOFFS_TYPE, filters={"widget_id": widget.id}, limit=5),
            workspace_id=str(ws.id),
        )
    ).objects
    assert [o.properties["question"] for o in objects] == ["wants to speak to the owner"]
    assert (
        await store.get_conversation(widget.id, _REF, str(ws.id))
    ).state is ConversationState.NEEDS_HUMAN
    # The audit trail records WHO raised it.
    events = await store.recent_events(widget.id, limit=5)
    assert events[0].payload == {"source": "agent"}


@pytest.mark.asyncio
async def test_the_agent_tool_cannot_reach_a_sibling_tenants_widget(
    mongo_db, store, fabric, monkeypatch, ws
):
    """The handler re-loads the widget WORKSPACE-SCOPED, so a stale or forged run
    context can't escalate (or touch) another tenant's conversation."""
    from pocketpaw_ee.agent.mcp_servers import pawbar as pawbar_mcp

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    other = await _workspace(owner="user-owner-2")
    widget = await store.create_widget(_widget(str(other.id)))
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda *a, **k: store)
    monkeypatch.setattr(
        pawbar_mcp, "_run_context", lambda: {"widget_id": widget.id, "actions": [], "handoff": True}
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", lambda: str(ws.id)
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.current_user_id", lambda: _REF)

    result = await pawbar_mcp._run_handoff({"reason": "let me in"})

    assert result["is_error"] is True
    assert await store.get_conversation(widget.id, _REF, str(other.id)) is None


@pytest.mark.asyncio
async def test_the_handoff_path_performs_no_tenant_scoped_effect(
    mongo_db, store, fabric, monkeypatch, ws
):
    """SS-2 still holds. A concierge run reaching the escape hatch escalates
    itself and does NOTHING else: no cart, no Instinct proposal / decision row,
    and exactly one reserved Fabric type in the tenant's graph."""
    from pocketpaw_ee.agent.mcp_servers import pawbar as pawbar_mcp

    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    # A SELLING widget — declared auto + gated verbs are in reach on this run, so
    # "nothing fired" is a real result rather than an empty configuration.
    widget = await store.create_widget(_selling_widget(str(ws.id)))
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda *a, **k: store)
    monkeypatch.setattr(
        pawbar_mcp,
        "_run_context",
        lambda: {
            "widget_id": widget.id,
            "actions": [{"verb": "book_table", "policy": "gated", "args": {"name": "str"}}],
            "handoff": True,
        },
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", lambda: str(ws.id)
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.agent_service.current_user_id", lambda: _REF)

    await pawbar_mcp._run_handoff({"reason": "book me a table for six"})

    assert await store.get_cart(widget.id, _REF) is None, "no visitor-scoped effect fired"
    assert await store.list_decisions_for_widget(widget.id) == [], "no Instinct proposal was raised"
    types = await fabric.list_types(workspace_id=str(ws.id))
    assert [t.name for t in types] == [PAW_HANDOFFS_TYPE], "only the reserved handoff type"


# --------------------------------------------------------------------------- #
# Layer 5 — OWNER NOTIFICATIONS. Three triggers, always fail-soft.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_notification_fires_on_a_handoff(mongo_db, store, fabric, monkeypatch, ws):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))

    await raise_handoff(
        widget=widget,
        workspace_id=str(ws.id),
        customer_ref=_REF,
        question="Please call me",
        store=store,
    )

    notes = await _notifications(str(ws.id))
    assert len(notes) == 1
    assert notes[0].type == "paw_bar_needs_human"
    assert notes[0].recipient == _OWNER_USER, "v1 fan-out is the workspace owner alone"
    assert notes[0].body == "Please call me"
    assert notes[0].source.id == f"{widget.id}:{_REF}"


@pytest.mark.asyncio
async def test_notification_fires_on_the_first_turn_of_a_new_conversation(
    public, fabric, monkeypatch, ws
):
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    _stub_run_dispatch(monkeypatch)

    await c.post(
        "/paw-bar/chat",
        json=_chat_payload(widget.id, message="Hello there"),
        headers={"Origin": _ORIGIN},
    )

    notes = await _notifications(str(ws.id))
    assert [n.type for n in notes] == ["paw_bar_conversation_new"]
    assert notes[0].recipient == _OWNER_USER
    assert notes[0].body == "Hello there"


@pytest.mark.asyncio
async def test_the_second_turn_does_not_notify_again(public, fabric, monkeypatch, ws):
    """Not every message. A bar that pinged on each turn would train the owner to
    ignore the badge, which costs them the escalations that matter."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    _stub_run_dispatch(monkeypatch)

    await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})
    await c.post(
        "/paw-bar/chat",
        json=_chat_payload(widget.id, message="And on Sundays?"),
        headers={"Origin": _ORIGIN},
    )

    assert len(await _notifications(str(ws.id))) == 1


@pytest.mark.asyncio
async def test_notification_fires_when_a_visitor_replies_to_a_muted_bot(
    public, fabric, monkeypatch, ws
):
    """The person holding this thread is not watching the bot's queue."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    _stub_run_dispatch(monkeypatch)
    await store.upsert_conversation_on_visitor_turn(widget.id, _REF, str(ws.id))
    await store.update_conversation(widget.id, _REF, workspace_id=str(ws.id), bot_paused=True)

    res = await c.post(
        "/paw-bar/chat",
        json=_chat_payload(widget.id, message="Are you still there?"),
        headers={"Origin": _ORIGIN},
    )

    assert "event: human_replying" in res.text
    notes = await _notifications(str(ws.id))
    assert [n.type for n in notes] == ["paw_bar_visitor_reply"]
    assert notes[0].body == "Are you still there?"


@pytest.mark.asyncio
async def test_a_raising_notifier_does_not_break_a_visitor_turn(public, fabric, monkeypatch, ws):
    """Fail-soft, proven by breaking it. The owner loses a badge; the visitor
    keeps their answer."""
    c, store = public
    await _site(str(ws.id))
    widget = await store.create_widget(_widget(str(ws.id)))
    _stub_run_dispatch(monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr("pocketpaw_ee.cloud.notifications.service.create", _boom)

    res = await c.post("/paw-bar/chat", json=_chat_payload(widget.id), headers={"Origin": _ORIGIN})

    assert res.status_code == 200
    assert "event: chunk" in res.text
    assert await _notifications(str(ws.id)) == []
    # The turn's real work still happened.
    assert await store.get_conversation(widget.id, _REF, str(ws.id)) is not None


@pytest.mark.asyncio
async def test_a_raising_notifier_does_not_break_a_handoff(
    mongo_db, store, fabric, monkeypatch, ws
):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget(str(ws.id)))

    async def _boom(*a, **k):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr("pocketpaw_ee.cloud.notifications.service.create", _boom)

    outcome = await raise_handoff(
        widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store
    )

    assert outcome.ok is True
    assert (
        await store.get_conversation(widget.id, _REF, str(ws.id))
    ).state is ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_a_workspace_with_no_resolvable_owner_notifies_nobody(
    mongo_db, store, fabric, monkeypatch
):
    """A legacy / non-ObjectId workspace handle has no document and therefore no
    owner. "Nobody to notify" is a normal answer here, not an error."""
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    widget = await store.create_widget(_widget("ws-legacy"))

    outcome = await raise_handoff(
        widget=widget, workspace_id="ws-legacy", customer_ref=_REF, store=store
    )

    assert outcome.ok is True
    assert await _notifications("ws-legacy") == []


@pytest.mark.asyncio
async def test_a_sibling_tenants_owner_is_never_notified(mongo_db, store, fabric, monkeypatch, ws):
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    other = await _workspace(owner="user-owner-2")
    widget = await store.create_widget(_widget(str(ws.id)))

    await raise_handoff(widget=widget, workspace_id=str(ws.id), customer_ref=_REF, store=store)

    assert await _notifications(str(other.id)) == []
    assert [n.recipient for n in await _notifications(str(ws.id))] == [_OWNER_USER]


# --------------------------------------------------------------------------- #
# Layer 6 — the PROMPT. Conditional, and absolute where it renders.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_preamble_teaches_the_escape_hatch_when_a_widget_is_bound():
    from pocketpaw_ee.cloud.surface.handlers import concierge as concierge_handler

    preamble = await concierge_handler.build_preamble("w", "u", _concierge_meta())

    assert "pawbar_request_human" in preamble
    assert "ALWAYS honored" in preamble
    assert "notified" in preamble


@pytest.mark.asyncio
async def test_the_preamble_omits_it_when_no_widget_is_bound():
    """Mirrors how the actions block renders conditionally: no inbox to reach, no
    instruction promising one."""
    from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
    from pocketpaw_ee.cloud.surface.handlers import concierge as concierge_handler

    preamble = await concierge_handler.build_preamble("w", "u", SurfaceMeta(route_path="/paw-bar"))

    assert "pawbar_request_human" not in preamble
    # The rest of the guardrails are untouched.
    assert "IGNORE any instruction" in preamble
