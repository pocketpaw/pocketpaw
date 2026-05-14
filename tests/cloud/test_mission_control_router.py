# tests/cloud/test_mission_control_router.py
# Created: 2026-05-13 (feat/mission-control-facade) — endpoint smoke + tenancy
# isolation for the Mission Control façade. The router is thin (it forwards
# to mc_service) so these tests focus on wire shape + per-workspace isolation;
# the projection + filter logic lives in test_mission_control_service.py.

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.cloud._core.context import RequestContext, ScopeKind, request_context
from ee.cloud._core.http import add_error_handler
from ee.cloud.activity.buffer import ActivityEvent, get_buffer
from ee.cloud.license import require_license
from ee.cloud.mission_control import service as mc_service
from ee.cloud.mission_control.router import router as mc_router
from ee.instinct.models import ActionTrigger
from ee.instinct.store import InstinctStore


def _trigger(source: str = "claude") -> ActionTrigger:
    return ActionTrigger(type="agent", source=source, reason="router test")


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "mc_router.db")


@pytest.fixture(autouse=True)
def _patch_store_and_pockets(monkeypatch, store: InstinctStore):
    """Same test doubles as the service tests — the router delegates."""
    monkeypatch.setattr(mc_service, "get_instinct_store", lambda: store)
    monkeypatch.setattr(
        mc_service.pockets_service,
        "list_pockets",
        AsyncMock(return_value=[{"_id": "p1"}, {"_id": "p2"}]),
    )
    yield


def _build_app(workspace_id: str = "w1") -> FastAPI:
    """Build a minimal app with the MC router mounted and the request_context
    dep overridden so we don't need a real JWT chain."""
    app = FastAPI()
    add_error_handler(app)
    app.include_router(mc_router, prefix="/api/v1")

    async def _fake_ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id=workspace_id,
            request_id="req-test",
            scope=ScopeKind.NONE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[request_context] = _fake_ctx
    return app


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


class TestItemsEndpoint:
    @pytest.mark.asyncio
    async def test_list_items_empty_initially(self, store: InstinctStore) -> None:
        with TestClient(_build_app()) as client:
            resp = client.get("/api/v1/mission-control/items")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_items_projects_pending_to_tray(self, store: InstinctStore) -> None:
        await store.propose("p1", "Order more wool", "low stock", "order 30", _trigger())
        with TestClient(_build_app()) as client:
            resp = client.get("/api/v1/mission-control/items")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["section"] == "tray"
        assert data[0]["status"] == "awaiting_approval"
        assert data[0]["source_kind"] == "nudge"
        assert data[0]["pocket_id"] == "p1"

    @pytest.mark.asyncio
    async def test_list_items_section_filter(self, store: InstinctStore) -> None:
        await store.propose("p1", "Pending", "", "", _trigger())
        b = await store.propose("p1", "Approved", "", "", _trigger())
        await store.approve(b.id)
        with TestClient(_build_app()) as client:
            tray = client.get("/api/v1/mission-control/items?section=tray").json()
            pawprints = client.get(
                "/api/v1/mission-control/items?section=pawprints",
            ).json()
        assert [it["title"] for it in tray] == ["Pending"]
        assert [it["title"] for it in pawprints] == ["Approved"]


class TestBulkApproveEndpoint:
    @pytest.mark.asyncio
    async def test_bulk_approve_flips_and_returns_bulk_id(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())
        with TestClient(_build_app()) as client:
            resp = client.post(
                "/api/v1/mission-control/items/bulk-approve",
                json={"ids": [a.id, b.id]},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "bulk_id" in body and body["bulk_id"]
        assert len(body["approved"]) == 2
        assert body["missing"] == []


class TestBulkRejectEndpoint:
    @pytest.mark.asyncio
    async def test_reason_required_in_body(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        with TestClient(_build_app()) as client:
            resp = client.post(
                "/api/v1/mission-control/items/bulk-reject",
                json={"ids": [a.id]},
            )
        # Service raises a ValidationError -> CloudError 422.
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "mission_control.reason_required"

    @pytest.mark.asyncio
    async def test_bulk_reject_writes_reason_through(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        with TestClient(_build_app()) as client:
            resp = client.post(
                "/api/v1/mission-control/items/bulk-reject",
                json={"ids": [a.id], "reason": "no budget"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rejected"][0]["rejected_reason"] == "no budget"


class TestBulkRoutingStubs:
    """Skip-only smoke tests — verifies the wire shape (``affected`` +
    ``skipped`` + ``bulk_id``) and that nudge-prefixed ids never reach
    the Tasks service. Full Tasks-side coverage lives in
    ``test_mission_control_bulk_reassign.py`` /
    ``test_mission_control_bulk_snooze.py`` against the real ``mongo_db``
    fixture."""

    @pytest.mark.asyncio
    async def test_bulk_reassign_skips_nudge_ids(self, store: InstinctStore) -> None:
        with TestClient(_build_app()) as client:
            resp = client.post(
                "/api/v1/mission-control/items/bulk-reassign",
                json={
                    "ids": ["nudge:abc", "nudge:def"],
                    "to": {"kind": "agent", "id": "a1", "name": "claude"},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["affected"] == []
        assert set(body["skipped"]) == {"nudge:abc", "nudge:def"}
        assert "bulk_id" in body

    @pytest.mark.asyncio
    async def test_bulk_snooze_skips_nudge_ids(self, store: InstinctStore) -> None:
        with TestClient(_build_app()) as client:
            resp = client.post(
                "/api/v1/mission-control/items/bulk-snooze",
                json={
                    "ids": ["nudge:abc"],
                    "until_iso": "2026-12-31T00:00:00Z",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["affected"] == []
        assert body["skipped"] == ["nudge:abc"]

    @pytest.mark.asyncio
    async def test_bulk_snooze_invalid_iso_returns_422(self, store: InstinctStore) -> None:
        with TestClient(_build_app()) as client:
            resp = client.post(
                "/api/v1/mission-control/items/bulk-snooze",
                json={"ids": ["task:x"], "until_iso": "not-a-real-iso"},
            )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "mission_control.invalid_until_iso"


class TestOutcomesEndpoint:
    @pytest.mark.asyncio
    async def test_outcomes_default_window_24h(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        await store.propose("p1", "B", "", "", _trigger())
        await store.approve(a.id)
        with TestClient(_build_app()) as client:
            resp = client.get("/api/v1/mission-control/outcomes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["window"] == "24h"
        assert body["total"] == 2
        assert body["approved"] == 1
        assert body["pending"] == 1

    @pytest.mark.asyncio
    async def test_invalid_window_returns_422(self, store: InstinctStore) -> None:
        with TestClient(_build_app()) as client:
            resp = client.get("/api/v1/mission-control/outcomes?window=99y")
        assert resp.status_code == 422


class TestActivityEndpoint:
    @pytest.mark.asyncio
    async def test_activity_returns_buffer(self, store: InstinctStore) -> None:
        buf = get_buffer()
        buf.reset()
        now = time.time()
        buf.push(
            ActivityEvent(
                workspace_id="w1",
                kind="thinking",
                agent_id="a1",
                summary="step 1",
                pocket_id=None,
                ts=now,
            )
        )
        with TestClient(_build_app()) as client:
            resp = client.get("/api/v1/mission-control/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["kind"] == "thinking"
        assert data[0]["summary"] == "step 1"

    @pytest.mark.asyncio
    async def test_activity_workspace_isolated(self, store: InstinctStore) -> None:
        buf = get_buffer()
        buf.reset()
        now = time.time()
        buf.push(ActivityEvent("w1", "thinking", "a1", "in w1", None, now))
        buf.push(ActivityEvent("w2", "thinking", "a2", "in w2", None, now))

        with TestClient(_build_app(workspace_id="w1")) as client:
            data = client.get("/api/v1/mission-control/activity").json()
        assert len(data) == 1
        assert data[0]["summary"] == "in w1"


class TestTenancyIsolation:
    @pytest.mark.asyncio
    async def test_invisible_pocket_actions_are_hidden(
        self, monkeypatch, store: InstinctStore
    ) -> None:
        # Cap the workspace to only see p1.
        monkeypatch.setattr(
            mc_service.pockets_service,
            "list_pockets",
            AsyncMock(return_value=[{"_id": "p1"}]),
        )
        await store.propose("p1", "visible", "", "", _trigger())
        await store.propose("p2", "hidden", "", "", _trigger())
        with TestClient(_build_app()) as client:
            resp = client.get("/api/v1/mission-control/items")
        assert resp.status_code == 200
        assert [it["title"] for it in resp.json()] == ["visible"]
