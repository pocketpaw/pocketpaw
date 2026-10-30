# tests/cloud/test_instinct_bulk_actions.py
# Created: 2026-05-13 (feat/mission-control-facade) — store + router tests for
# the new bulk-approve / bulk-reject Instinct endpoints. Asserts the shared
# bulk_id audit tag, the per-item Pawprint write, the require_status guard,
# and the new ``assignee`` filter on /actions/pending.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.instinct.router import router

from pocketpaw.instinct.models import ActionStatus, ActionTrigger
from pocketpaw.instinct.store import InstinctStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, workspace_id: str = "ws-test") -> None:
        self.id = "user-test-1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _trigger(source: str = "claude") -> ActionTrigger:
    return ActionTrigger(type="agent", source=source, reason="bulk-action test")


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "bulk_instinct.db")


@pytest.fixture
def test_app(tmp_path: Path, monkeypatch):
    fake_user = _FakeUser()
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: fake_user.active_workspace
    return app


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "bulk_router.db")


@pytest.fixture
def client(test_app, router_store: InstinctStore):
    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        yield TestClient(test_app)


# ---------------------------------------------------------------------------
# Store-level: bulk_approve
# ---------------------------------------------------------------------------


class TestBulkApprove:
    @pytest.mark.asyncio
    async def test_bulk_approve_flips_all_listed_pending(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())
        c = await store.propose("p1", "C", "", "", _trigger())

        approved, missing, bulk_id = await store.bulk_approve([a.id, b.id, c.id])

        assert {x.id for x in approved} == {a.id, b.id, c.id}
        assert missing == []
        assert bulk_id  # uuid4 hex
        for x in approved:
            assert x.status == ActionStatus.APPROVED

    @pytest.mark.asyncio
    async def test_bulk_approve_writes_shared_bulk_id_on_every_audit_row(
        self, store: InstinctStore
    ) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())

        _, _, bulk_id = await store.bulk_approve([a.id, b.id], note="ship it")

        entries = await store.query_audit(pocket_id="p1")
        approved_rows = [e for e in entries if e.event == "action_approved"]
        assert len(approved_rows) == 2
        for row in approved_rows:
            assert row.context.get("bulk_id") == bulk_id
            assert row.context.get("note") == "ship it"

    @pytest.mark.asyncio
    async def test_bulk_approve_skips_already_resolved(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())
        # Approve a single-item first — b stays pending.
        await store.approve(a.id)

        approved, missing, _ = await store.bulk_approve([a.id, b.id])
        # a is already approved -> not approved again. b flips.
        assert [x.id for x in approved] == [b.id]
        assert missing == [a.id]

    @pytest.mark.asyncio
    async def test_bulk_approve_records_missing_for_unknown_ids(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        approved, missing, _ = await store.bulk_approve([a.id, "act-does-not-exist"])
        assert [x.id for x in approved] == [a.id]
        assert missing == ["act-does-not-exist"]

    @pytest.mark.asyncio
    async def test_rejected_items_dont_get_approved(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())
        await store.reject(a.id, reason="no")

        approved, missing, _ = await store.bulk_approve([a.id, b.id])
        assert [x.id for x in approved] == [b.id]
        assert missing == [a.id]
        # Confirm a stays rejected
        fetched = await store.get_action(a.id)
        assert fetched is not None and fetched.status == ActionStatus.REJECTED


class TestBulkReject:
    @pytest.mark.asyncio
    async def test_bulk_reject_requires_reason_at_call_site(self, store: InstinctStore) -> None:
        # The store doesn't enforce non-empty (the router does), so this
        # is a behavioural check that the empty reason still produces a
        # bulk_id and audit row with reason=''.
        a = await store.propose("p1", "A", "", "", _trigger())
        rejected, _, bulk_id = await store.bulk_reject([a.id], reason="")
        assert len(rejected) == 1
        entries = await store.query_audit(pocket_id="p1")
        reject_rows = [e for e in entries if e.event == "action_rejected"]
        assert len(reject_rows) == 1
        assert reject_rows[0].context.get("bulk_id") == bulk_id

    @pytest.mark.asyncio
    async def test_bulk_reject_writes_reason_on_audit_and_action(
        self, store: InstinctStore
    ) -> None:
        a = await store.propose("p1", "A", "", "", _trigger())
        b = await store.propose("p1", "B", "", "", _trigger())

        rejected, _, bulk_id = await store.bulk_reject([a.id, b.id], reason="out of scope")
        for x in rejected:
            assert x.status == ActionStatus.REJECTED
            assert x.rejected_reason == "out of scope"

        entries = await store.query_audit(pocket_id="p1")
        for row in [e for e in entries if e.event == "action_rejected"]:
            assert row.context.get("bulk_id") == bulk_id
            assert row.context.get("reason") == "out of scope"


# ---------------------------------------------------------------------------
# Store-level: assignee filter on pending
# ---------------------------------------------------------------------------


class TestPendingAssignee:
    @pytest.mark.asyncio
    async def test_pending_default_returns_all(self, store: InstinctStore) -> None:
        await store.propose("p1", "A", "", "", _trigger(), assignee="alice")
        await store.propose("p1", "B", "", "", _trigger(), assignee="bob")
        await store.propose("p1", "C", "", "", _trigger())  # no assignee

        pending = await store.pending()
        assert len(pending) == 3

    @pytest.mark.asyncio
    async def test_pending_with_assignee_filters_to_owner(self, store: InstinctStore) -> None:
        await store.propose("p1", "A", "", "", _trigger(), assignee="alice")
        await store.propose("p1", "B", "", "", _trigger(), assignee="bob")
        await store.propose("p1", "C", "", "", _trigger(), assignee="alice")

        pending_alice = await store.pending(assignee="alice")
        assert {a.title for a in pending_alice} == {"A", "C"}

    @pytest.mark.asyncio
    async def test_assignee_persists_across_load(self, store: InstinctStore) -> None:
        a = await store.propose("p1", "A", "", "", _trigger(), assignee="alice")
        fetched = await store.get_action(a.id)
        assert fetched is not None and fetched.assignee == "alice"


# ---------------------------------------------------------------------------
# Router-level: HTTP endpoints
# ---------------------------------------------------------------------------


TRIGGER = {"type": "agent", "source": "claude", "reason": "router test"}


class TestBulkApproveEndpoint:
    def test_bulk_approve_returns_bulk_id_and_affected(self, client: TestClient) -> None:
        r1 = client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "A", "trigger": TRIGGER},
        )
        r2 = client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "B", "trigger": TRIGGER},
        )
        ids = [r1.json()["id"], r2.json()["id"]]

        resp = client.post(
            "/instinct/actions/bulk-approve",
            json={"ids": ids, "note": "ship-it"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "bulk_id" in data and data["bulk_id"]
        assert len(data["affected"]) == 2
        assert data["missing"] == []

    def test_bulk_approve_does_not_consume_param_route(self, client: TestClient) -> None:
        """``bulk-approve`` is a literal path; FastAPI must not eat it as a
        parameter to ``/{action_id}/approve``."""
        resp = client.post("/instinct/actions/bulk-approve", json={"ids": []})
        # Empty ids fail schema validation (min_length=1); we want a 422
        # — not a 404 from the parameterised route swallowing the path.
        assert resp.status_code == 422

    def test_bulk_approve_reports_missing_ids(self, client: TestClient) -> None:
        resp = client.post(
            "/instinct/actions/bulk-approve",
            json={"ids": ["act-nope-1", "act-nope-2"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["affected"] == []
        assert set(data["missing"]) == {"act-nope-1", "act-nope-2"}


class TestBulkRejectEndpoint:
    def test_bulk_reject_requires_reason_in_body(self, client: TestClient) -> None:
        resp = client.post("/instinct/actions/bulk-reject", json={"ids": ["x"]})
        # reason has min_length=1; missing field is a 422.
        assert resp.status_code == 422

    def test_bulk_reject_persists_reason_per_item(self, client: TestClient) -> None:
        r1 = client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "A", "trigger": TRIGGER},
        )
        r2 = client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "B", "trigger": TRIGGER},
        )
        ids = [r1.json()["id"], r2.json()["id"]]
        resp = client.post(
            "/instinct/actions/bulk-reject",
            json={"ids": ids, "reason": "budget exhausted"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["affected"]) == 2
        for item in data["affected"]:
            assert item["status"] == "rejected"
            assert item["rejected_reason"] == "budget exhausted"


class TestPendingAssigneeEndpoint:
    def test_pending_default_returns_all_pending(self, client: TestClient) -> None:
        client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "A", "trigger": TRIGGER},
        )
        client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "B", "trigger": TRIGGER},
        )
        resp = client.get("/instinct/actions/pending")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_pending_with_assignee_yields_empty_when_unset(self, client: TestClient) -> None:
        # We can't set assignee via the propose endpoint right now (it's
        # a store-level field added in this PR). Calling the endpoint
        # with an assignee filter should simply yield zero rows.
        client.post(
            "/instinct/actions",
            json={"pocket_id": "p1", "title": "A", "trigger": TRIGGER},
        )
        resp = client.get("/instinct/actions/pending?assignee=alice")
        assert resp.status_code == 200
        assert resp.json() == []
