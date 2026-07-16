# tests/cloud/test_paw_bar_actions.py — Paw Bar action registry (C1), end to end.
# Created 2026-07-16: covers the visitor commerce loop + the concierge tool
# injection. Layers:
#   * Spec validation — bad action/catalog/checkout declarations are rejected.
#   * The shared executor — add_to_cart (happy / unknown product / qty caps),
#     checkout (with / without items), and the gated path (raises a WORKSPACE-
#     scoped Instinct proposal + parks a decision, executes NOTHING; approval is
#     delivered back on the SAME decision poll — reusing the decision-loop path).
#   * The public endpoints (httpx) — POST /paw-bar/action + GET /paw-bar/cart share
#     the concierge armor: bad key 401, wrong origin 403, over-limit 429, a widget
#     bound to a sibling pocket/workspace 403, and a happy add→cart round-trip.
#   * PATCH /paw-bar/widgets/{id} — admin agent_id update + cross-tenant 404.
#   * Tool injection — the pawbar_actions MCP server + the concierge allow-list
#     surface tools ONLY when the widget declares actions (deny-all otherwise).
#   * The session_key interlock — the concierge dispatch's session_key starts
#     "cloud:concierge:" (a sibling PR gates soul learning on that prefix).

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from pocketpaw.instinct.store import InstinctStore
from pocketpaw.paw_bar.models import (
    DecisionState,
    PawBarBlock,
    PawBarEvent,
    PawBarSpec,
    PawBarWidget,
)
from pocketpaw.paw_bar.store import PawBarStore

_VALID_KEY = "site_key_" + "a" * 24
_ORIGIN = "https://brewco.com"


def _actions_spec(pocket_id: str = "pocket-1", **ov: Any) -> PawBarSpec:
    """A spec that declares the full action vocabulary + a catalog + checkout."""
    data: dict[str, Any] = dict(
        widget_id="pp_seed",
        pocket_id=pocket_id,
        blocks=[PawBarBlock(type="text", content="Brew & Co")],
        actions=[
            {"verb": "add_to_cart", "policy": "auto", "args": {"product_id": "str", "qty": "int"}},
            {"verb": "checkout", "policy": "auto", "args": {}},
            {
                "verb": "book_table",
                "policy": "gated",
                "args": {"date": "str", "party_size": "int"},
                "label": "Book a table",
            },
        ],
        catalog=[{"id": "espresso", "name": "Espresso", "price_cents": 350, "currency": "USD"}],
        checkout_url="https://brewco.com/checkout?cart={cart_ref}",
    )
    data.update(ov)
    return PawBarSpec(**data)


def _widget(**ov: Any) -> PawBarWidget:
    d: dict[str, Any] = dict(
        pocket_id="pocket-1",
        owner="user:maya",
        name="Brew & Co",
        spec=_actions_spec(),
        allowed_domains=["brewco.com"],
        agent_id="agent-xyz",
        workspace_id="ws-1",
    )
    d.update(ov)
    return PawBarWidget(**d)


# --------------------------------------------------------------------------- #
# Layer 1 — spec validation rejects bad declarations
# --------------------------------------------------------------------------- #


class TestSpecValidation:
    def test_valid_spec_round_trips(self) -> None:
        spec = _actions_spec()
        assert [a.verb for a in spec.actions] == ["add_to_cart", "checkout", "book_table"]
        assert spec.catalog[0].id == "espresso"

    @pytest.mark.parametrize(
        "bad",
        [
            {"actions": [{"verb": "Add_To_Cart"}]},  # not snake_case
            {"actions": [{"verb": "x"}, {"verb": "x"}]},  # duplicate verb
            {"actions": [{"verb": "x", "policy": "sometimes"}]},  # bad policy
            {"actions": [{"verb": "x", "args": {"q": "list"}}]},  # non-flat arg type
            {"actions": [{"verb": "subscribe", "policy": "auto"}]},  # SS-2: non-cart auto
            {"catalog": [{"id": "a", "name": "A", "price_cents": -1}]},  # negative price
            {"catalog": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]},  # dup id
            {"checkout_url": "ftp://nope"},  # not http(s)
        ],
    )
    def test_bad_declarations_rejected(self, bad: dict[str, Any]) -> None:
        with pytest.raises(Exception):
            PawBarSpec(widget_id="w", pocket_id="p", **bad)


# --------------------------------------------------------------------------- #
# Layer 2 — the shared executor
# --------------------------------------------------------------------------- #


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Isolated PawBar + Instinct stores, patched onto the singletons the
    executor + decision-loop lazy-import (mirrors the decision-loop test)."""
    pp_store = PawBarStore(tmp_path / "pb_actions.db")
    instinct_store = InstinctStore(tmp_path / "instinct_actions.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: instinct_store)
    monkeypatch.setattr("pocketpaw.stores.get_paw_bar_store", lambda: pp_store)
    return pp_store, instinct_store


class TestExecutorAuto:
    async def test_add_to_cart_happy_path(self, stores) -> None:
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        out = await execute_action(widget, "ws-1", "c1", "add_to_cart", {"product_id": "espresso"})
        assert out.ok
        assert out.result == {"added": "espresso", "qty": 1}
        assert out.cart["total_cents"] == 350
        assert out.cart["items"][0]["id"] == "espresso"
        # checkout_url is rendered with the opaque cart ref (no raw customer_ref).
        assert "{cart_ref}" not in out.cart["checkout_url"]
        assert "c1" not in out.cart["checkout_url"]

    async def test_add_to_cart_unknown_product_rejected(self, stores) -> None:
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        out = await execute_action(widget, "ws-1", "c1", "add_to_cart", {"product_id": "nope"})
        assert not out.ok
        assert out.error == "unknown_product"
        assert out.http_status == 422

    async def test_undeclared_verb_and_unknown_arg_rejected(self, stores) -> None:
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        undeclared = await execute_action(widget, "ws-1", "c1", "wipe_db", {})
        assert not undeclared.ok and undeclared.error == "verb_not_declared"
        bad_arg = await execute_action(widget, "ws-1", "c1", "add_to_cart", {"evil": "x"})
        assert not bad_arg.ok and bad_arg.error.startswith("unknown_arg")

    async def test_qty_is_clamped_to_range(self, stores) -> None:
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        over = await execute_action(
            widget, "ws-1", "c1", "add_to_cart", {"product_id": "espresso", "qty": 5000}
        )
        assert over.result["qty"] == 99
        under = await execute_action(
            widget, "ws-1", "c2", "add_to_cart", {"product_id": "espresso", "qty": 0}
        )
        assert under.result["qty"] == 1

    async def test_checkout_empty_cart_is_conflict(self, stores) -> None:
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        out = await execute_action(widget, "ws-1", "c1", "checkout", {})
        assert not out.ok
        assert out.error == "empty_cart"
        assert out.http_status == 409

    async def test_checkout_with_items_renders_link(self, stores) -> None:
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        await execute_action(widget, "ws-1", "c1", "add_to_cart", {"product_id": "espresso"})
        out = await execute_action(widget, "ws-1", "c1", "checkout", {})
        assert out.ok
        assert out.result["checkout_url"].startswith("https://brewco.com/checkout?cart=")
        assert out.result["cart_ref"] and "{cart_ref}" not in out.result["checkout_url"]


class TestExecutorGated:
    async def test_gated_verb_raises_scoped_proposal_and_executes_nothing(self, stores) -> None:
        """A gated verb NEVER executes an effect: it raises a WORKSPACE-scoped
        Instinct proposal (kind paw_bar_action) + parks a PENDING decision, and
        touches no visitor cart."""
        from pocketpaw_ee.paw_bar.actions import execute_action

        pp_store, instinct_store = stores
        widget = await pp_store.create_widget(_widget())
        out = await execute_action(
            widget, "ws-1", "c1", "book_table", {"date": "Fri", "party_size": 4}
        )
        assert out.ok
        assert out.result["status"] == "pending"
        action_id = out.result["instinct_action_id"]
        assert action_id

        # The proposal is in the widget's workspace, and NOT in another tenant's.
        pending = await instinct_store.pending(workspace_id="ws-1")
        assert len(pending) == 1 and pending[0].id == action_id
        blob = pending[0].parameters["_customer_reply"]
        assert blob["kind"] == "paw_bar_action"
        assert blob["verb"] == "book_table"
        assert blob["args"] == {"date": "Fri", "party_size": 4}
        assert await instinct_store.pending(workspace_id="ws-other") == []

        # A decision row was parked, and NO cart was created (executed nothing).
        parked = await pp_store.get_latest_decision(widget.id, "c1")
        assert parked is not None and parked.state == DecisionState.PENDING
        assert await pp_store.get_cart(widget.id, "c1") is None

    async def test_gated_approval_delivers_back_to_customer_poll(self, stores) -> None:
        """Approving the gated proposal delivers the reply on the SAME decision
        poll the ingest loop uses (the reused delivery path)."""
        from pocketpaw_ee.paw_bar.actions import execute_action
        from pocketpaw_ee.paw_bar.decision_loop import deliver_customer_decision

        pp_store, instinct_store = stores
        widget = await pp_store.create_widget(_widget())
        out = await execute_action(
            widget, "ws-1", "c1", "book_table", {"date": "Fri", "party_size": 4}
        )
        action_id = out.result["instinct_action_id"]

        approved = await instinct_store.approve(action_id, approver="user:maya")
        await deliver_customer_decision(approved, declined=False)

        decision = await pp_store.get_latest_decision(widget.id, "c1")
        assert decision.state == DecisionState.DELIVERED
        assert decision.reply
        assert decision.decided_by == "user:maya"


# --------------------------------------------------------------------------- #
# Layer 3 — the public endpoints (front-gate + shared executor)
# --------------------------------------------------------------------------- #


async def _site(**ov: Any):
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


@pytest_asyncio.fixture
async def action_client(tmp_path, mongo_db):
    """Public app client for POST /paw-bar/action + GET /paw-bar/cart, backed by a
    tmp store (widget) + Beanie (Site). Yields (client, store)."""
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    store = PawBarStore(tmp_path / "endpoint.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, store


def _action_body(widget_id: str, **ov: Any) -> dict[str, Any]:
    b = dict(
        key=_VALID_KEY,
        w=widget_id,
        customer_ref="cust-1",
        verb="add_to_cart",
        args={"product_id": "espresso"},
    )
    b.update(ov)
    return b


class TestActionEndpoint:
    async def test_add_then_cart_round_trip(self, action_client) -> None:
        client, store = action_client
        await _site()
        widget = await store.create_widget(_widget())

        res = await client.post(
            "/paw-bar/action", json=_action_body(widget.id), headers={"Origin": _ORIGIN}
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] and body["result"]["added"] == "espresso"
        assert body["cart"]["total_cents"] == 350

        cart = await client.get(
            "/paw-bar/cart",
            params={"key": _VALID_KEY, "w": widget.id, "customer_ref": "cust-1"},
            headers={"Origin": _ORIGIN},
        )
        assert cart.status_code == 200
        assert cart.json()["items"][0]["id"] == "espresso"
        assert cart.json()["total_cents"] == 350

    async def test_bad_key_is_401(self, action_client) -> None:
        client, store = action_client
        await _site()
        widget = await store.create_widget(_widget())
        res = await client.post(
            "/paw-bar/action",
            json=_action_body(widget.id, key="short"),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 401

    async def test_wrong_origin_is_403(self, action_client) -> None:
        client, store = action_client
        await _site()
        widget = await store.create_widget(_widget())
        res = await client.post(
            "/paw-bar/action",
            json=_action_body(widget.id),
            headers={"Origin": "https://evil.example"},
        )
        assert res.status_code == 403

    async def test_widget_bound_to_sibling_pocket_is_403(self, action_client) -> None:
        client, store = action_client
        await _site(pocket_id="pocket-A")
        widget = await store.create_widget(
            _widget(pocket_id="pocket-B", spec=_actions_spec(pocket_id="pocket-B"))
        )
        res = await client.post(
            "/paw-bar/action", json=_action_body(widget.id), headers={"Origin": _ORIGIN}
        )
        assert res.status_code == 403

    async def test_rate_limit_is_429(self, action_client) -> None:
        client, store = action_client
        await _site()
        widget = await store.create_widget(_widget(per_customer_limit_per_min=2))
        for _ in range(2):
            await store.record_event(
                PawBarEvent(widget_id=widget.id, type="pawbar_action:x", customer_ref="cust-1")
            )
        res = await client.post(
            "/paw-bar/action", json=_action_body(widget.id), headers={"Origin": _ORIGIN}
        )
        assert res.status_code == 429

    async def test_unknown_product_surfaces_422(self, action_client) -> None:
        client, store = action_client
        await _site()
        widget = await store.create_widget(_widget())
        res = await client.post(
            "/paw-bar/action",
            json=_action_body(widget.id, args={"product_id": "ghost"}),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 422

    async def test_empty_cart_returns_empty_shape(self, action_client) -> None:
        client, store = action_client
        await _site()
        widget = await store.create_widget(_widget())
        res = await client.get(
            "/paw-bar/cart",
            params={"key": _VALID_KEY, "w": widget.id, "customer_ref": "brand-new"},
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["items"] == [] and body["total_cents"] == 0
        assert body["checkout_url"].startswith("https://brewco.com/checkout?cart=")


# --------------------------------------------------------------------------- #
# Layer 4 — PATCH /paw-bar/widgets/{id} (agent_id + fields), workspace-scoped
# --------------------------------------------------------------------------- #


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """TestClient with the admin CRUD routes' workspace dep pinned (like the
    decision-loop test), backed by a tmp store."""
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.paw_bar.router import router

    pp_store = PawBarStore(tmp_path / "admin.db")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_workspace_id] = lambda: "ws-1"
    monkeypatch.setattr("pocketpaw_ee.paw_bar.router._store", lambda *a, **k: pp_store)
    return TestClient(app), pp_store


class TestWidgetPatch:
    def test_patch_updates_agent_id_and_fields(self, admin_client) -> None:
        client, store = admin_client
        import asyncio

        widget = asyncio.get_event_loop().run_until_complete(
            store.create_widget(_widget(agent_id="", workspace_id="ws-1"))
        )
        res = client.patch(
            f"/paw-bar/widgets/{widget.id}",
            json={"agent_id": "agent-new", "name": "Renamed"},
            headers={"X-Paw-Bar-Token": widget.access_token},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["agent_id"] == "agent-new"
        assert body["name"] == "Renamed"
        # The token must never leak in the response (PawBarWidgetPublic).
        assert "access_token" not in body

    def test_patch_null_agent_id_unbinds(self, admin_client) -> None:
        client, store = admin_client
        import asyncio

        widget = asyncio.get_event_loop().run_until_complete(
            store.create_widget(_widget(agent_id="agent-xyz", workspace_id="ws-1"))
        )
        res = client.patch(
            f"/paw-bar/widgets/{widget.id}",
            json={"agent_id": None},
            headers={"X-Paw-Bar-Token": widget.access_token},
        )
        assert res.status_code == 200
        assert res.json()["agent_id"] == ""

    def test_patch_cross_tenant_is_404(self, admin_client, monkeypatch) -> None:
        client, store = admin_client
        import asyncio

        # A widget owned by ws-other must 404 for the ws-1 admin session.
        widget = asyncio.get_event_loop().run_until_complete(
            store.create_widget(_widget(workspace_id="ws-other"))
        )
        res = client.patch(
            f"/paw-bar/widgets/{widget.id}",
            json={"agent_id": "x"},
            headers={"X-Paw-Bar-Token": widget.access_token},
        )
        assert res.status_code == 404


# --------------------------------------------------------------------------- #
# Layer 5 — tool injection: tools only when the widget declares actions
# --------------------------------------------------------------------------- #


@pytest.fixture
def pawbar_ctx():
    """Bind/clear the per-run Paw Bar action ContextVar around a test.

    Teardown clears the var to None rather than resetting by token: an async test
    runs in a different ``contextvars`` context than this fixture, so a
    token-based reset would raise "created in a different Context". Production
    (run_core) binds + resets in the SAME finally, so it uses the strict reset."""
    from pocketpaw_ee.cloud.chat.agent_service import bind_pawbar_run

    def _bind(run):
        bind_pawbar_run(run)

    yield _bind
    bind_pawbar_run(None)


class TestToolInjection:
    _ACTIONS = [
        {"verb": "add_to_cart", "policy": "auto", "args": {"product_id": "str"}, "label": "Add"},
        {"verb": "book_table", "policy": "gated", "args": {"date": "str"}, "label": "Book"},
    ]

    def test_deny_all_when_no_actions(self, pawbar_ctx) -> None:
        from pocketpaw_ee.agent.mcp_servers.pawbar import (
            build_pawbar_actions_server,
            pawbar_tool_ids,
        )
        from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
        from pocketpaw_ee.cloud.surface.service import resolve_profile

        # No context bound → server builds nothing, no ids.
        assert build_pawbar_actions_server() is None
        assert pawbar_tool_ids() == ()
        # And the concierge profile stays deny-all (empty allow-list).
        prof = resolve_profile(SurfaceKind.CONCIERGE, SurfaceMeta())
        assert prof.allow_mcp_tool_ids == frozenset()

    def test_tools_built_and_allowlisted_when_declared(self, pawbar_ctx) -> None:
        from pocketpaw_ee.agent.mcp_servers.pawbar import (
            build_pawbar_actions_server,
            pawbar_tool_id,
            pawbar_tool_ids,
        )
        from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
        from pocketpaw_ee.cloud.surface.service import resolve_profile

        pawbar_ctx({"widget_id": "pp-1", "actions": self._ACTIONS})
        built = build_pawbar_actions_server()
        assert built is not None and built[0] == "pawbar_actions"
        ids = set(pawbar_tool_ids())
        assert pawbar_tool_id("add_to_cart") in ids
        assert pawbar_tool_id("book_table") in ids

        # The concierge allow-list widens to EXACTLY these verbs, deny set intact.
        prof = resolve_profile(SurfaceKind.CONCIERGE, SurfaceMeta(pawbar_actions=self._ACTIONS))
        assert prof.allow_mcp_tool_ids == frozenset(
            {pawbar_tool_id("add_to_cart"), pawbar_tool_id("book_table")}
        )
        assert {"WebSearch", "Bash"} <= prof.deny_mcp_tool_ids

    async def test_tool_handler_runs_shared_executor(self, pawbar_ctx, stores, monkeypatch) -> None:
        """A built tool's handler re-loads the live widget and calls the shared
        executor — so the tool path and the endpoint path converge."""
        from pocketpaw_ee.agent.mcp_servers import pawbar as pawbar_mod

        pp_store, _ = stores
        widget = await pp_store.create_widget(_widget())
        pawbar_ctx({"widget_id": widget.id, "actions": self._ACTIONS})
        # The handler resolves identity via these ContextVars.
        monkeypatch.setattr(pawbar_mod, "_run_context", lambda: {"widget_id": widget.id})
        monkeypatch.setattr(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", lambda: "ws-1"
        )
        monkeypatch.setattr(
            "pocketpaw_ee.cloud.chat.agent_service.current_user_id", lambda: "cust-9"
        )
        result = await pawbar_mod._run_verb("add_to_cart", {"product_id": "espresso"})
        assert result.get("is_error") is not True
        # The shared executor ran: the visitor's cart now holds the item.
        cart = await pp_store.get_cart(widget.id, "cust-9")
        assert cart is not None and cart.items[0].id == "espresso"


# --------------------------------------------------------------------------- #
# Layer 6 — the session_key interlock (pins the concierge dispatch seam)
# --------------------------------------------------------------------------- #


class _CaptureExecutor:
    def __init__(self, transport) -> None:
        self.transport = transport
        self.submitted: list = []

    async def submit(self, spec) -> None:
        self.submitted.append(spec)
        await self.transport.append_event(spec.run_id, "chunk", {"content": "hi", "type": "text"})
        await self.transport.append_event(
            spec.run_id, "stream_end", {"assistant_message_id": "m1", "cancelled": False}
        )


@pytest_asyncio.fixture
async def concierge_client(tmp_path, mongo_db):
    from unittest.mock import patch

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    store = PawBarStore(tmp_path / "concierge_actions.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=store):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client, store


class TestSessionKeyInterlock:
    async def test_dispatch_session_key_prefix_and_actions_threaded(
        self, concierge_client, monkeypatch
    ) -> None:
        """The concierge dispatch produces a session_key starting
        'cloud:concierge:' (a sibling PR gates soul learning on that prefix), and
        the widget's declared actions ride surface_meta so the run can inject the
        per-verb tools."""
        client, store = concierge_client
        await _site()
        widget = await store.create_widget(_widget(agent_id="agent-xyz"))

        from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

        transport = InMemoryStreamTransport()
        fake_exec = _CaptureExecutor(transport)
        monkeypatch.setattr(
            "pocketpaw_ee.cloud.chat.runs.transport.get_stream_transport", lambda: transport
        )
        monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.executor.get_executor", lambda: fake_exec)

        async def _fake_create_run(spec):
            return SimpleNamespace(run_id=spec.run_id)

        monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.create_run", _fake_create_run)

        res = await client.post(
            "/paw-bar/chat",
            json={
                "widget_id": widget.id,
                "signed_key": _VALID_KEY,
                "customer_ref": "cust-1",
                "message": "hi",
            },
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200
        assert len(fake_exec.submitted) == 1
        spec = fake_exec.submitted[0]
        assert spec.session_key.startswith("cloud:concierge:")
        # The action declarations were threaded so the run can inject tools.
        verbs = [a["verb"] for a in spec.surface_meta["pawbar_actions"]]
        assert verbs == ["add_to_cart", "checkout", "book_table"]
        assert spec.surface_meta["widget_id"] == widget.id
