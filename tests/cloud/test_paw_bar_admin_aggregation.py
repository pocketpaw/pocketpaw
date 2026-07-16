# tests/cloud/test_paw_bar_admin_aggregation.py — Paw Bar owner aggregation reads
# (D2). Created 2026-07-16: covers the four per-site Concierge dashboard reads under
# /paw-bar/admin/site/{site_id}/* — overview, conversations, decisions, handoffs.
# Layers:
#   * Auth: require_scope("admin") — an unauthenticated caller gets 403 (enforce_scope).
#   * Tenancy: a cross-tenant / malformed site id → 404 (never leaks existence).
#   * CROSS-SITE ISOLATION (the key security test): a second site + widget in the
#     SAME workspace is absent from this site's decisions, conversations, and
#     handoffs — each read is bound to THIS site's widget/pocket, never pocket-wide
#     or workspace-wide.
#   * Overview shape + counts (pending decisions from the DecisionStatus table,
#     conversations from ChatRunDoc, handoffs from Fabric).
#   * Decisions filtered to the widget + mapped to {id, verb_or_kind, summary,
#     status, created_at}.
#   * Handoffs empty-but-well-shaped in v1 (no producer), and isolated by widget.
#   * Conversations list (LISTABLE — grouped by customer_ref, unsupported=False).

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import (
    DecisionState,
    DecisionStatus,
    PawBarBlock,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_KEY = "site_key_" + "a" * 24


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


async def _site(**ov: Any):
    from pocketpaw_ee.cloud.models.site import Site

    d = dict(
        workspace="ws-1",
        pocket_id="pocket-1",
        owner="user:maya",
        script_name="",
        signed_key=_KEY,
        allowed_origins=["brewco.com"],
    )
    d.update(ov)
    s = Site(**d)
    await s.insert()
    return s


def _spec(pocket_id: str = "pocket-1", widget_id: str = "pp_seed") -> PawBarSpec:
    return PawBarSpec(
        widget_id=widget_id,
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Hi from Brew & Co")],
    )


def _widget(**ov: Any) -> PawBarWidget:
    d = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=_spec(),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
        rate_limit_per_min=60,
        per_customer_limit_per_min=10,
    )
    d.update(ov)
    return PawBarWidget(**d)


async def _mk_decision(store: PawBarStore, widget_id: str, **ov: Any) -> DecisionStatus:
    d = dict(
        customer_ref="cust-0001",
        event_type="paw_bar_action:checkout",
        instinct_action_id="act-" + uuid.uuid4().hex[:8],
        # The row's workspace_id column stores the widget OWNER, not the physical
        # workspace — mirror that here so the tests exercise the real shape.
        workspace_id="user:maya",
        state=DecisionState.PENDING,
        reply="",
    )
    d.update(ov)
    return await store.create_decision(DecisionStatus(widget_id=widget_id, **d))


async def _mk_run(**ov: Any):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    d = dict(
        run_id=uuid.uuid4().hex,
        workspace="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key="cloud:concierge:pocket-1:cust-0001:agent-xyz",
        user_id="cust-0001",
        agent_id="agent-xyz",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        status="completed",
        partial_text="Hello, what time do you open?",
    )
    d.update(ov)
    doc = ChatRunDoc(**d)
    await doc.insert()
    return doc


async def _mk_handoff(fabric, widget_id: str, **props: Any):
    type_ = await fabric.get_type_by_name("_paw_handoffs", workspace_id="ws-1")
    p = dict(
        widget_id=widget_id,
        contact="visitor@brewco.com",
        question="Can I book?",
        transcript_ref="tr-1",
    )
    p.update(props)
    return await fabric.create_object(type_id=type_.id, properties=p, workspace_id="ws-1")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def store(tmp_path):
    return PawBarStore(tmp_path / "agg.db")


@pytest_asyncio.fixture
async def fabric(tmp_path):
    from pocketpaw.fabric.store import FabricStore

    fs = FabricStore(tmp_path / "fabric.db")
    # Empty-schema type accepts any properties (declared-only validation).
    await fs.define_type("_paw_handoffs", properties=[], workspace_id="ws-1")
    return fs


@pytest_asyncio.fixture
async def client(mongo_db, store, fabric, monkeypatch):
    """One admin app client backed by the tmp paw_bar store (widget + decisions),
    Beanie (Site + ChatRunDoc), and a tmp Fabric store (handoffs).
    ``current_workspace_id`` is pinned to ws-1. Yields ``(client, store, fabric)``.
    """
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"

    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, store, fabric


# --------------------------------------------------------------------------- #
# Layer 1 — auth: require_scope("admin")
# --------------------------------------------------------------------------- #


@pytest.mark.enforce_scope
@pytest.mark.asyncio
async def test_overview_requires_admin(mongo_db, tmp_path, monkeypatch):
    """An unauthenticated caller is 403'd before any resolution (require_scope)."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)

    @app.middleware("http")
    async def _no_auth(request, call_next):
        return await call_next(request)  # stamps nothing → unauthenticated

    app.include_router(router)
    monkeypatch.setattr(
        "pocketpaw_ee.paw_bar.router._store",
        lambda: PawBarStore(tmp_path / "auth.db"),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        for path in ("overview", "conversations", "decisions", "handoffs"):
            res = await c.get(f"/paw-bar/admin/site/000000000000000000000001/{path}")
            assert res.status_code == 403, f"{path}: {res.status_code}"


# --------------------------------------------------------------------------- #
# Layer 2 — tenancy: cross-tenant / malformed id → 404
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cross_tenant_site_is_404(client):
    """A site owned by another workspace 404s for the ws-1 admin (never leaks)."""
    c, _store, _fabric = client
    site = await _site(workspace="ws-other")
    for path in ("overview", "conversations", "decisions", "handoffs"):
        res = await c.get(f"/paw-bar/admin/site/{site.id}/{path}")
        assert res.status_code == 404, f"{path}: {res.status_code}"


@pytest.mark.asyncio
async def test_malformed_site_id_is_404(client):
    c, _store, _fabric = client
    res = await c.get("/paw-bar/admin/site/not-an-objectid/overview")
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Layer 3 — overview shape + counts
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_overview_shape_and_counts(client):
    c, store, fabric = client
    site = await _site()
    widget = await store.create_widget(_widget())
    # Two pending decisions + one delivered (only pending counts).
    await _mk_decision(store, widget.id, state=DecisionState.PENDING)
    await _mk_decision(store, widget.id, state=DecisionState.PENDING)
    await _mk_decision(store, widget.id, state=DecisionState.DELIVERED, reply="Done")
    # Two runs, one customer each → two conversations.
    await _mk_run(user_id="cust-A")
    await _mk_run(user_id="cust-B")
    await _mk_handoff(fabric, widget.id)

    res = await c.get(f"/paw-bar/admin/site/{site.id}/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["widget"]["id"] == widget.id
    assert body["widget"]["agent_id"] == "agent-xyz"
    assert "spec" in body["widget"]
    assert body["enabled"] is True
    assert body["greeting"] == ""
    assert body["counts"]["pending_decisions"] == 2
    assert body["counts"]["conversations"] == 2
    assert body["counts"]["handoffs"] == 1


@pytest.mark.asyncio
async def test_overview_no_widget_degrades(client):
    """A site with no paw-bar widget yet still renders (widget=null, counts 0)."""
    c, _store, _fabric = client
    site = await _site(pocket_id="pocket-empty")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["widget"] is None
    assert body["enabled"] is True
    assert body["counts"] == {"conversations": 0, "pending_decisions": 0, "handoffs": 0}


@pytest.mark.asyncio
async def test_overview_reflects_kill_switch_and_greeting(client):
    c, store, _fabric = client
    site = await _site(concierge_enabled=False, concierge_greeting="Back at 9am")
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/overview")
    body = res.json()
    assert body["enabled"] is False
    assert body["greeting"] == "Back at 9am"


# --------------------------------------------------------------------------- #
# Layer 4 — decisions filtered to the widget
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decisions_shape_and_verb_mapping(client):
    c, store, _fabric = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_decision(
        store,
        widget.id,
        event_type="paw_bar_action:checkout",
        instinct_action_id="act-checkout",
        state=DecisionState.PENDING,
    )
    await _mk_decision(
        store,
        widget.id,
        event_type="contact_form",
        instinct_action_id="act-reply",
        state=DecisionState.DELIVERED,
        reply="Thanks!",
    )
    res = await c.get(f"/paw-bar/admin/site/{site.id}/decisions")
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 2
    by_id = {i["id"]: i for i in items}
    # Gated action → verb; the action id is the Tray join key.
    assert by_id["act-checkout"]["verb_or_kind"] == "checkout"
    assert by_id["act-checkout"]["status"] == "pending"
    assert "created_at" in by_id["act-checkout"]
    # Non-action event → customer_reply; delivered → summary is the reply.
    assert by_id["act-reply"]["verb_or_kind"] == "customer_reply"
    assert by_id["act-reply"]["status"] == "delivered"
    assert by_id["act-reply"]["summary"] == "Thanks!"


@pytest.mark.asyncio
async def test_decisions_empty_without_widget(client):
    c, _store, _fabric = client
    site = await _site(pocket_id="pocket-empty")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/decisions")
    assert res.status_code == 200
    assert res.json()["items"] == []


# --------------------------------------------------------------------------- #
# Layer 5 — handoffs empty-but-well-shaped (v1, no producer)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handoffs_empty_v1(client):
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/handoffs")
    assert res.status_code == 200, res.text
    assert res.json() == {"items": []}


@pytest.mark.asyncio
async def test_handoffs_shape_when_present(client):
    """When a handoff object exists it maps to the frozen item shape."""
    c, store, fabric = client
    site = await _site()
    widget = await store.create_widget(_widget())
    await _mk_handoff(
        fabric, widget.id, contact="a@b.com", question="Book a table?", transcript_ref="tr-9"
    )
    res = await c.get(f"/paw-bar/admin/site/{site.id}/handoffs")
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["contact"] == "a@b.com"
    assert items[0]["question"] == "Book a table?"
    assert items[0]["transcript_ref"] == "tr-9"
    assert items[0]["created_at"]  # ISO timestamp present


# --------------------------------------------------------------------------- #
# Layer 6 — conversations list (LISTABLE)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_conversations_lists_grouped_by_customer(client):
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    now = datetime.now(UTC)
    # cust-A has two runs; the newer one wins the preview + timestamp.
    await _mk_run(user_id="cust-A", partial_text="older", createdAt=now - timedelta(minutes=10))
    await _mk_run(user_id="cust-A", partial_text="newer", createdAt=now, ended_at=now)
    await _mk_run(user_id="cust-B", partial_text="hi from B", createdAt=now - timedelta(minutes=5))

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["unsupported"] is False
    refs = [i["customer_ref"] for i in body["items"]]
    assert refs == ["cust-A", "cust-B"]  # newest-first, deduped
    a = next(i for i in body["items"] if i["customer_ref"] == "cust-A")
    assert a["preview"] == "newer"


@pytest.mark.asyncio
async def test_conversations_empty_well_shaped(client):
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["unsupported"] is False


# --------------------------------------------------------------------------- #
# Layer 7 — CROSS-SITE ISOLATION (the key security test)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sibling_site_absent_from_all_reads(client):
    """A second site + widget in the SAME workspace must never bleed into this
    site's decisions, conversations, or handoffs. This is the leak surface the
    security review checks — the reads bind to THIS widget / pocket only.
    """
    c, store, fabric = client

    # This site: pocket-1 / widget A.
    site_a = await _site(pocket_id="pocket-1")
    widget_a = await store.create_widget(
        _widget(pocket_id="pocket-1", spec=_spec("pocket-1", "pp_a"))
    )
    # Sibling site in the SAME workspace: pocket-2 / widget B.
    _site_b = await _site(pocket_id="pocket-2")
    widget_b = await store.create_widget(
        _widget(pocket_id="pocket-2", spec=_spec("pocket-2", "pp_b"))
    )

    # Seed data for BOTH widgets/pockets.
    await _mk_decision(store, widget_a.id, instinct_action_id="a-dec")
    await _mk_decision(store, widget_b.id, instinct_action_id="b-dec")
    await _mk_run(scope_id="pocket-1", user_id="cust-A", partial_text="for A")
    await _mk_run(scope_id="pocket-2", user_id="cust-B", partial_text="for B")
    await _mk_handoff(fabric, widget_a.id, contact="a-contact")
    await _mk_handoff(fabric, widget_b.id, contact="b-contact")

    # Decisions: only widget A's.
    decisions = (await c.get(f"/paw-bar/admin/site/{site_a.id}/decisions")).json()["items"]
    assert [d["id"] for d in decisions] == ["a-dec"]

    # Conversations: only pocket-1's customer.
    convos = (await c.get(f"/paw-bar/admin/site/{site_a.id}/conversations")).json()["items"]
    assert [c_["customer_ref"] for c_ in convos] == ["cust-A"]

    # Handoffs: only widget A's.
    handoffs = (await c.get(f"/paw-bar/admin/site/{site_a.id}/handoffs")).json()["items"]
    assert [h["contact"] for h in handoffs] == ["a-contact"]

    # Overview counts for site A never include B's row.
    overview = (await c.get(f"/paw-bar/admin/site/{site_a.id}/overview")).json()
    assert overview["counts"]["pending_decisions"] == 1
    assert overview["counts"]["conversations"] == 1
    assert overview["counts"]["handoffs"] == 1
