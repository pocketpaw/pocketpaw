# tests/cloud/test_paw_bar_admin_aggregation.py — Paw Bar owner aggregation reads
# (D2). Created 2026-07-16: covers the four per-site Concierge dashboard reads under
# /paw-bar/admin/site/{site_id}/* — overview, conversations, decisions, handoffs.
# Layers:
#   * Auth ROLE gate (D2 security review): the reads use require_action("paw_bar.read")
#     (ADMIN). A member session gets 403 on all 4 reads; admin + owner get 200.
#   * Tenancy: a cross-tenant / malformed site id → 404 (never leaks existence).
#   * CROSS-SITE ISOLATION (the key security test): a second site + widget in the
#     SAME workspace is absent from this site's decisions, conversations, and
#     handoffs — each read is bound to THIS site's widget/pocket, never pocket-wide
#     or workspace-wide.
#   * CROSS-WORKSPACE ISOLATION (belt-and-suspenders): a second workspace's widget +
#     decisions never appear in workspace-1's reads.
#   * Empty-pocket_id guard: a site with a blank pocket_id resolves widget=None (no
#     sibling leak) rather than widening the widget lookup.
#   * Overview shape + counts (pending decisions from the DecisionStatus table,
#     conversations from ChatRunDoc, handoffs from Fabric).
#   * Decisions filtered to the widget + mapped to {id, verb_or_kind, summary,
#     status, created_at}.
#   * Handoffs empty-but-well-shaped in v1 (no producer), and isolated by widget.
#   * Conversations list (LISTABLE — grouped by customer_ref, unsupported=False).
# Updated 2026-07-26 (concierge transcripts): a tenth layer covers two-sided
#   transcripts — a run carrying ``user_text`` renders the visitor turn before the
#   agent's, a run with no reply still shows what was asked, the question is
#   stamped earlier than the answer, and the conversations preview falls back to
#   the visitor's question only when there is no reply to show.

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


async def _mk_handoff(fabric, widget_id: str, *, workspace: str = "ws-1", **props: Any):
    # The type is defined for ws-1 in the fixture; create_object resolves it by id
    # unscoped, so a ws-2 object reuses the same type_id but stamps its own workspace.
    type_ = await fabric.get_type_by_name("_paw_handoffs", workspace_id="ws-1")
    p = dict(
        widget_id=widget_id,
        contact="visitor@brewco.com",
        question="Can I book?",
        transcript_ref="tr-1",
    )
    p.update(props)
    return await fabric.create_object(type_id=type_.id, properties=p, workspace_id=workspace)


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


def _fake_user(role: str, workspace_id: str = "ws-1", user_id: str = "u1") -> SimpleNamespace:
    """User stand-in shaped like ``ee.cloud.models.user.User`` — only the fields
    the RBAC chain reads (``id``, ``active_workspace``, ``workspaces``). ``role`` is
    the caller's WorkspaceRole in ``workspace_id`` (member | admin | owner)."""
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role=role)],
    )


def _build_app(store, fabric, monkeypatch, *, role: str = "admin", workspace_id: str = "ws-1"):
    """Mount the paw_bar router with the given caller ROLE + workspace.

    ``current_active_user`` is overridden with a role-scoped stand-in so
    ``require_action("paw_bar.read")`` runs its REAL role check;
    ``current_workspace_id`` is pinned so the reads scope data to the same
    workspace. The tmp paw_bar + Fabric stores are patched in.
    """
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)

    user = _fake_user(role=role, workspace_id=workspace_id)
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id

    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda: store)
    monkeypatch.setattr("pocketpaw_ee.api.get_fabric_store", lambda *a, **k: fabric)
    return app


@pytest_asyncio.fixture
async def client(mongo_db, store, fabric, monkeypatch):
    """Default (ADMIN) app client backed by the tmp paw_bar store (widget +
    decisions), Beanie (Site + ChatRunDoc), and a tmp Fabric store (handoffs).
    Pinned to ws-1. Yields ``(client, store, fabric)``.
    """
    app = _build_app(store, fabric, monkeypatch, role="admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, store, fabric


# --------------------------------------------------------------------------- #
# Layer 1 — auth ROLE gate: require_action("paw_bar.read") (ADMIN)
# --------------------------------------------------------------------------- #

_ALL_READS = ("overview", "conversations", "decisions", "handoffs")


@pytest.mark.asyncio
async def test_member_role_is_forbidden_on_all_reads(mongo_db, store, fabric, monkeypatch):
    """A workspace MEMBER (below admin) gets 403 on every read — the reads carry
    visitor PII + owner decision context, so member/viewer must not see them."""
    site = await _site()
    await store.create_widget(_widget())
    app = _build_app(store, fabric, monkeypatch, role="member")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        for path in _ALL_READS:
            res = await c.get(f"/paw-bar/admin/site/{site.id}/{path}")
            assert res.status_code == 403, f"{path}: {res.status_code} {res.text}"


@pytest.mark.parametrize("role", ["admin", "owner"])
@pytest.mark.asyncio
async def test_admin_and_owner_roles_are_allowed(role, mongo_db, store, fabric, monkeypatch):
    """Admin and owner both clear the role gate (200) on every read."""
    site = await _site()
    await store.create_widget(_widget())
    app = _build_app(store, fabric, monkeypatch, role=role)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        for path in _ALL_READS:
            res = await c.get(f"/paw-bar/admin/site/{site.id}/{path}")
            assert res.status_code == 200, f"{role}/{path}: {res.status_code} {res.text}"


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


@pytest.mark.asyncio
async def test_overview_includes_agent_name_for_bound_widget(client):
    """The overview widget block carries agent_name resolved from the agents
    service so the E2 card can show the concierge name (feat/site-dedicated-agent)."""
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    c, store, _fabric = client
    site = await _site()
    ctx = agents_service.legacy_ctx("user:maya", "ws-1")
    agent = await agents_service.create(
        ctx,
        "ws-1",
        CreateAgentRequest(name="Brew & Co Concierge", slug="concierge-brewco"),
    )
    await store.create_widget(_widget(agent_id=agent.id))

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/overview")).json()
    assert body["widget"]["agent_id"] == agent.id
    assert body["widget"]["agent_name"] == "Brew & Co Concierge"


@pytest.mark.asyncio
async def test_overview_agent_name_absent_when_unbound(client):
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget(agent_id=""))
    body = (await c.get(f"/paw-bar/admin/site/{site.id}/overview")).json()
    assert body["widget"]["agent_id"] == ""
    assert body["widget"]["agent_name"] == ""


@pytest.mark.asyncio
async def test_overview_dangling_agent_id_degrades_to_empty_name(client):
    """A bound agent_id that no longer resolves degrades to an empty agent_name
    rather than 500-ing the overview."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget(agent_id="ffffffffffffffffffffffff"))
    res = await c.get(f"/paw-bar/admin/site/{site.id}/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["widget"]["agent_id"] == "ffffffffffffffffffffffff"
    assert body["widget"]["agent_name"] == ""


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


# --------------------------------------------------------------------------- #
# Layer 8 — CROSS-WORKSPACE ISOLATION (belt-and-suspenders) + empty pocket_id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_second_workspace_absent_from_reads(client):
    """A second workspace's widget + decisions + runs + handoffs never appear in
    workspace-1's reads. Even when the foreign widget reuses the SAME pocket_id,
    the workspace-scoped widget lookup + the workspace-filtered conversations query
    keep the tenants apart.
    """
    c, store, fabric = client  # this client is scoped to ws-1

    # ws-1: the site + its widget (workspace_id ws-1).
    site_a = await _site(workspace="ws-1", pocket_id="pocket-1")
    widget_a = await store.create_widget(_widget(pocket_id="pocket-1", workspace_id="ws-1"))
    await _mk_decision(store, widget_a.id, instinct_action_id="ws1-dec")
    await _mk_run(workspace="ws-1", scope_id="pocket-1", user_id="cust-1")
    await _mk_handoff(fabric, widget_a.id, workspace="ws-1", contact="ws1-contact")

    # ws-2: a foreign widget on the SAME pocket id, with its own data.
    widget_b = await store.create_widget(
        _widget(pocket_id="pocket-1", workspace_id="ws-2", spec=_spec("pocket-1", "pp_b"))
    )
    await _mk_decision(store, widget_b.id, instinct_action_id="ws2-dec", workspace_id="ws-2")
    await _mk_run(workspace="ws-2", scope_id="pocket-1", user_id="cust-2")
    await _mk_handoff(fabric, widget_b.id, workspace="ws-2", contact="ws2-contact")

    # ws-1 admin reads site_a: the widget resolves to ws-1's widget (not ws-2's),
    # and none of ws-2's data bleeds through.
    decisions = (await c.get(f"/paw-bar/admin/site/{site_a.id}/decisions")).json()["items"]
    assert [d["id"] for d in decisions] == ["ws1-dec"]

    convos = (await c.get(f"/paw-bar/admin/site/{site_a.id}/conversations")).json()["items"]
    assert [x["customer_ref"] for x in convos] == ["cust-1"]

    handoffs = (await c.get(f"/paw-bar/admin/site/{site_a.id}/handoffs")).json()["items"]
    assert [h["contact"] for h in handoffs] == ["ws1-contact"]

    overview = (await c.get(f"/paw-bar/admin/site/{site_a.id}/overview")).json()
    assert overview["widget"]["id"] == widget_a.id
    assert overview["counts"] == {"conversations": 1, "pending_decisions": 1, "handoffs": 1}


@pytest.mark.asyncio
async def test_empty_pocket_id_resolves_no_widget(client):
    """A site whose pocket_id is blank must resolve widget=None, never widen the
    widget lookup into a sibling's widget (security review finding #2)."""
    c, store, _fabric = client
    # A sibling widget exists in the same workspace; the empty-pocket site must NOT
    # pick it up.
    await store.create_widget(_widget(pocket_id="pocket-real"))
    site = await _site(pocket_id="")

    overview = (await c.get(f"/paw-bar/admin/site/{site.id}/overview")).json()
    assert overview["widget"] is None
    assert overview["counts"]["pending_decisions"] == 0

    decisions = (await c.get(f"/paw-bar/admin/site/{site.id}/decisions")).json()
    assert decisions["items"] == []

    handoffs = (await c.get(f"/paw-bar/admin/site/{site.id}/handoffs")).json()
    assert handoffs["items"] == []


# --------------------------------------------------------------------------- #
# Layer 9 — conversation transcript drill-in
# --------------------------------------------------------------------------- #

_CUST = "cust-0001"  # valid customer_ref (>= 8 chars, allowed charset)


@pytest.mark.asyncio
async def test_transcript_happy_path_ordered_and_shaped(client):
    """The transcript is this visitor's concierge turns, oldest-first, shaped
    {customer_ref, messages:[{role,content,created_at}], count}. These runs carry
    no stored visitor text, so the result is assistant-only — which is exactly the
    shape a site with transcript retention turned off keeps producing."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    now = datetime.now(UTC)
    await _mk_run(user_id=_CUST, partial_text="second", createdAt=now, ended_at=now)
    await _mk_run(
        user_id=_CUST,
        partial_text="first",
        createdAt=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=5),
    )
    # A different visitor's run must not leak into this transcript.
    await _mk_run(user_id="cust-9999", partial_text="other visitor")

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_CUST}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["customer_ref"] == _CUST
    assert body["count"] == 2
    assert [m["content"] for m in body["messages"]] == ["first", "second"]  # oldest-first
    assert {m["role"] for m in body["messages"]} == {"assistant"}
    assert all(m["created_at"] for m in body["messages"])


@pytest.mark.asyncio
async def test_transcript_404_on_unknown_ref(client):
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/nosuchcustomer")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_transcript_invalid_customer_ref_is_400(client):
    """A ref that fails the charset/length bound is rejected before any lookup."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/short")  # < 8 chars
    assert res.status_code == 400
    assert "invalid_customer_ref" in res.text


@pytest.mark.asyncio
async def test_transcript_cross_site_isolation(client):
    """A visitor's conversation on a SIBLING site (different pocket) is not readable
    through this site — 404, never another pocket's transcript."""
    c, store, _fabric = client
    site_a = await _site(pocket_id="pocket-1")
    await store.create_widget(_widget(pocket_id="pocket-1"))
    _site_b = await _site(pocket_id="pocket-2")
    await store.create_widget(_widget(pocket_id="pocket-2", spec=_spec("pocket-2", "pp_b")))
    # The conversation lives on site B's pocket only.
    await _mk_run(scope_id="pocket-2", user_id=_CUST, partial_text="only on B")

    res = await c.get(f"/paw-bar/admin/site/{site_a.id}/conversations/{_CUST}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_transcript_member_role_is_forbidden(mongo_db, store, fabric, monkeypatch):
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(user_id=_CUST, partial_text="hi")
    app = _build_app(store, fabric, monkeypatch, role="member")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_CUST}")
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_transcript_cross_tenant_site_is_404(client):
    c, store, _fabric = client
    site = await _site(workspace="ws-other")
    await _mk_run(workspace="ws-other", user_id=_CUST, partial_text="hi")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_CUST}")
    assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Layer 10 — owner preview frame (D5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["admin", "owner"])
@pytest.mark.asyncio
async def test_preview_frame_serves_html_for_owner_admin(
    role, mongo_db, store, fabric, monkeypatch
):
    """Owner/admin get the glass bar HTML seeded with this site's key + widget."""
    site = await _site()
    widget = await store.create_widget(_widget())
    app = _build_app(store, fabric, monkeypatch, role=role)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/html")
    body = res.text
    assert "window.__PAWBAR__" in body
    assert "/pawbar-app/pawbar.js" in body  # the bundle ref (PAWBAR_APP_MOUNT)
    assert _KEY in body  # the site's signed_key seeded into the config
    assert widget.id in body  # the resolved widget id


@pytest.mark.asyncio
async def test_preview_frame_member_role_forbidden(mongo_db, store, fabric, monkeypatch):
    site = await _site()
    await store.create_widget(_widget())
    app = _build_app(store, fabric, monkeypatch, role="member")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_preview_frame_cross_tenant_is_404(client):
    c, _store, _fabric = client
    site = await _site(workspace="ws-other")
    res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_preview_frame_no_widget_is_404(client):
    c, _store, _fabric = client
    site = await _site(pocket_id="pocket-empty")  # no widget created for this pocket
    res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_preview_frame_csp_is_dashboard_origin(client, monkeypatch):
    """CSP frame-ancestors is exactly the configured dashboard origin (sanitized to
    a single host[:port]) — never the Site's allowed_origins, never '*'."""
    monkeypatch.setenv("PAWBAR_DASHBOARD_ORIGIN", "dash.example.com")
    c, store, _fabric = client
    site = await _site(allowed_origins=["brewco.com"])
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 200
    assert res.headers["content-security-policy"] == "frame-ancestors dash.example.com"
    # The Site's public allowlist must NOT be the framer here.
    assert "brewco.com" not in res.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_preview_frame_default_dashboard_origin(client):
    """With no env set, the CSP defaults to the Vite dev origin (scheme stripped)."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.headers["content-security-policy"] == "frame-ancestors localhost:5173"


@pytest.mark.asyncio
async def test_preview_frame_served_when_concierge_disabled(client):
    """The preview renders even when the kill switch is OFF, so the owner can test a
    paused bar (chat/action stay gated by the switch, unchanged)."""
    c, store, _fabric = client
    site = await _site(concierge_enabled=False)
    await store.create_widget(_widget())
    res = await c.get(f"/paw-bar/admin/site/{site.id}/preview-frame")
    assert res.status_code == 200
    assert "window.__PAWBAR__" in res.text


# --------------------------------------------------------------------------- #
# Layer 10 — two-sided transcripts (visitor lines stored on the run doc)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transcript_interleaves_visitor_and_agent_turns(client):
    """A run that stored the visitor's line renders BOTH turns, question first,
    so the owner reads a conversation instead of a monologue."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    now = datetime.now(UTC)
    await _mk_run(
        user_id=_CUST,
        user_text="What time do you open?",
        partial_text="We open at 8am.",
        createdAt=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=4),
    )
    await _mk_run(
        user_id=_CUST,
        user_text="Do you do gluten free?",
        partial_text="Yes, every day.",
        createdAt=now,
        ended_at=now,
    )

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_CUST}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 4
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "What time do you open?"),
        ("assistant", "We open at 8am."),
        ("user", "Do you do gluten free?"),
        ("assistant", "Yes, every day."),
    ]


@pytest.mark.asyncio
async def test_transcript_keeps_visitor_turn_when_reply_missing(client):
    """A run that failed before answering still shows what was asked — otherwise
    the owner sees an empty conversation and has no idea what went wrong."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(user_id=_CUST, user_text="Are you open on Sunday?", partial_text="")

    res = await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_CUST}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "Are you open on Sunday?")
    ]


@pytest.mark.asyncio
async def test_transcript_user_turn_is_stamped_at_ask_time(client):
    """The question carries the run's creation time and the answer its end time,
    so a slow reply is visible in the transcript rather than collapsed."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    asked = datetime.now(UTC) - timedelta(minutes=2)
    answered = datetime.now(UTC)
    await _mk_run(
        user_id=_CUST, user_text="hello?", partial_text="hi!", createdAt=asked, ended_at=answered
    )

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations/{_CUST}")).json()
    # Mongo stores millisecond precision, so compare the ORDER rather than exact
    # microseconds: the question must be stamped strictly before the answer.
    assert body["messages"][0]["created_at"] < body["messages"][1]["created_at"]


@pytest.mark.asyncio
async def test_conversations_preview_falls_back_to_visitor_question(client):
    """A conversation whose run produced no reply used to render a blank row; it
    now previews what the visitor asked."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(user_id=_CUST, user_text="Do you deliver?", partial_text="")

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()
    assert [i["preview"] for i in body["items"]] == ["Do you deliver?"]


@pytest.mark.asyncio
async def test_conversations_preview_still_prefers_the_reply(client):
    """The existing preview behaviour is unchanged when there IS a reply."""
    c, store, _fabric = client
    site = await _site()
    await store.create_widget(_widget())
    await _mk_run(user_id=_CUST, user_text="Do you deliver?", partial_text="Yes, within 5km.")

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/conversations")).json()
    assert [i["preview"] for i in body["items"]] == ["Yes, within 5km."]
