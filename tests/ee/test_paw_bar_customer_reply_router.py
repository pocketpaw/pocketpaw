# tests/ee/test_paw_bar_customer_reply_router.py — B0 H2 (four-path tenancy gate).
#
# Updated: 2026-08-05 (integration/growth-v1 rebase) — the harness now seeds the
# ``_customer_reply`` Action via ``store.propose`` directly (production parity
# with ``decision_loop.propose_customer_decision``) instead of over
# ``POST /instinct/actions``: growth's F2 hardening makes the generic propose
# route refuse every reserved gated-blob key, ``_customer_reply`` included.
#
# Created: 2026-07-15 (fix/paw-bar-decision-loop-tenancy). The security gate on
# the paw-bar ``_customer_reply`` Instinct proposal type. Clones the
# cross-workspace-403 discipline of ``tests/ee/test_instinct_rule_router.py``
# (its ``_instinct_rule`` peer) for the customer-decision blob.
#
# THE LOAD-BEARING ASSERTION: a foreign-workspace ``_customer_reply`` Action is
# refused with 403 + ``instinct.cross_workspace_approval`` on ALL FOUR router
# entry points — approve, bulk-approve, reject, bulk-reject. Missing the guard on
# even ONE is a cross-tenant escalation: ``deliver_customer_decision`` routes the
# reply to the blob's ``workspace_id`` (``set_decision(workspace_id=...)``), so a
# ws-A admin approving a ws-B ``_customer_reply`` action delivers a decision into
# ws-B's paw-bar surface. Every OTHER blob kind already has this assert; this one
# did not.
#
# The 403 tests are sync and drive the router over HTTP via ``TestClient`` — they
# 403 BEFORE any delivery/DB touch, so no Beanie is needed.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_paw_bar_customer_reply_router.py -q

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from pocketpaw_ee.paw_bar.decision_loop import CUSTOMER_REPLY_KEY  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

TRIGGER = {"type": "connector", "source": "paw_bar:pp_x", "reason": "customer reply test"}


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so any emit() on the approve/reject path is quiet."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield bus_mod._bus
    bus_mod._bus = prev  # type: ignore[attr-defined]


def _customer_reply_params(workspace_id: str) -> dict:
    """A ``_customer_reply`` blob — the shape ``decision_loop.propose_customer_decision``
    stores. The cross-workspace gate reads the top-level ``workspace_id``."""
    return {
        CUSTOMER_REPLY_KEY: {
            "schema": 1,
            "widget_id": "pp_x",
            "pocket_id": "pocket-1",
            "customer_ref": "cust-1",
            "event_type": "appointment_request",
            "workspace_id": workspace_id,
            "default_reply": "Thanks — we'll follow up shortly.",
            "payload_summary": "{}",
        }
    }


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "customer_reply_router.db")


def _make_app(user: _FakeUser, monkeypatch) -> FastAPI:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


def _make_client(user: _FakeUser, monkeypatch) -> TestClient:
    return TestClient(_make_app(user, monkeypatch))


def _propose_customer_reply(store: InstinctStore, *, workspace_id: str, name: str = "reply") -> str:
    """Seed a pending Action carrying a ``_customer_reply`` blob IN-PROCESS.

    Mirrors production: ``decision_loop.propose_customer_decision`` calls
    ``store.propose`` directly. Seeding over ``POST /instinct/actions`` stopped
    being possible when F2 landed — the generic propose route now REFUSES any
    reserved gated-blob key (422 ``instinct.reserved_parameter_key``), and
    ``_customer_reply`` is one of them. That refusal is the fix working, not a
    regression, so the harness seeds the way the owning surface does.
    """
    import asyncio

    from pocketpaw.instinct.models import ActionTrigger

    action = asyncio.run(
        store.propose(
            pocket_id="pocket-1",
            title=f"customer request {name}",
            description="",
            recommendation="",
            trigger=ActionTrigger.model_validate(TRIGGER),
            parameters=_customer_reply_params(workspace_id),
            # The ROW is stamped with the proposing caller's workspace (ws-A),
            # exactly as the old HTTP seeding did; the BLOB carries the target
            # workspace — the mismatch is the forged scenario under test.
            workspace_id="ws-A",
        )
    )
    return action.id


def _status_of(client: TestClient, action_id: str) -> str:
    resp = client.get("/instinct/actions", params={"limit": 500})
    assert resp.status_code == 200, resp.text
    for action in resp.json()["actions"]:
        if action["id"] == action_id:
            return action["status"]
    raise AssertionError(f"action {action_id} not found")


class TestCustomerReplyCrossWorkspace403:
    """``_assert_customer_reply_workspace`` must refuse a foreign-workspace
    ``_customer_reply`` Action with 403 + ``instinct.cross_workspace_approval`` on
    the approve, bulk-approve, reject, AND bulk-reject paths."""

    def test_single_approve_of_foreign_workspace_reply_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(_FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose_customer_reply(router_store, workspace_id="ws-B")
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, action_id) == "pending"

    def test_bulk_approve_of_foreign_workspace_reply_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(_FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose_customer_reply(router_store, workspace_id="ws-A", name="own")
            foreign = _propose_customer_reply(router_store, workspace_id="ws-B", name="foreign")
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [own, foreign]})
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"

    def test_single_reject_of_foreign_workspace_reply_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(_FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose_customer_reply(router_store, workspace_id="ws-B")
            resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "nope"})
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, action_id) == "pending"

    def test_bulk_reject_of_foreign_workspace_reply_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(_FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose_customer_reply(router_store, workspace_id="ws-A", name="own")
            foreign = _propose_customer_reply(router_store, workspace_id="ws-B", name="foreign")
            resp = client.post(
                "/instinct/actions/bulk-reject",
                json={"ids": [own, foreign], "reason": "batch nope"},
            )
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"

    def test_same_workspace_reply_is_not_blocked_by_the_gate(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        """A SAME-workspace ``_customer_reply`` approve must NOT 403 on the gate.

        Proves the assert keys on tenancy, not on the blob's mere presence — a
        ws-A admin approving a ws-A customer reply passes the workspace check.
        """
        client = _make_client(_FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose_customer_reply(router_store, workspace_id="ws-A")
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            # The tenancy gate must not fire; the action flips to approved.
            assert resp.status_code == 200, resp.text
            assert _status_of(client, action_id) == "approved"
