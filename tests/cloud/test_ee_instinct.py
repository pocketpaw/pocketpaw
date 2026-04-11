# tests/test_ee_instinct.py — Comprehensive tests for ee/instinct (store + router).
# Created: 2026-03-28 — Initial store tests.
# Updated: 2026-03-30 — Full store unit tests + FastAPI router integration tests added.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ee.instinct.models import (
    ActionCategory,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditCategory,
)
from ee.instinct.router import router
from ee.instinct.store import InstinctStore

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_trigger(source: str = "claude", type_: str = "agent") -> ActionTrigger:
    """Return a minimal ActionTrigger for testing."""
    return ActionTrigger(type=type_, source=source, reason="unit test trigger")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    """Isolated SQLite store backed by a temp file — never touches ~/.pocketpaw."""
    return InstinctStore(tmp_path / "instinct_test.db")


@pytest.fixture
def test_app(tmp_path: Path):
    """FastAPI app with the instinct router and a patched store singleton."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    """Store used by router-level tests, isolated to tmp_path."""
    return InstinctStore(tmp_path / "router_instinct_test.db")


@pytest.fixture
def client(test_app, router_store: InstinctStore):
    """TestClient with _store patched to return the isolated router_store."""
    with patch("ee.instinct.router._store", return_value=router_store):
        yield TestClient(test_app)


# ---------------------------------------------------------------------------
# Unit Tests: Store — action lifecycle
# ---------------------------------------------------------------------------


class TestProposeAction:
    """test_propose_action — create a pending action and verify fields."""

    @pytest.mark.asyncio
    async def test_propose_action_returns_pending_action(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Reorder inventory",
            description="Stock at 4 units, threshold is 10",
            recommendation="Order 20 units from supplier",
            trigger=make_trigger(),
        )

        assert action.id.startswith("act-")
        assert action.pocket_id == "pocket-1"
        assert action.title == "Reorder inventory"
        assert action.description == "Stock at 4 units, threshold is 10"
        assert action.recommendation == "Order 20 units from supplier"
        assert action.status == ActionStatus.PENDING
        assert action.priority == ActionPriority.MEDIUM
        assert action.category == ActionCategory.WORKFLOW

    @pytest.mark.asyncio
    async def test_propose_action_persists_to_db(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Send alert",
            description="",
            recommendation="Notify team",
            trigger=make_trigger(),
            category=ActionCategory.ALERT,
            priority=ActionPriority.HIGH,
        )

        fetched = await store.get_action(action.id)
        assert fetched is not None
        assert fetched.id == action.id
        assert fetched.category == ActionCategory.ALERT
        assert fetched.priority == ActionPriority.HIGH

    @pytest.mark.asyncio
    async def test_propose_action_stores_parameters(self, store: InstinctStore) -> None:
        params = {"quantity": 20, "supplier": "ACME Corp", "unit_price": 4.99}
        action = await store.propose(
            pocket_id="pocket-1",
            title="Place order",
            description="",
            recommendation="",
            trigger=make_trigger(),
            parameters=params,
        )

        fetched = await store.get_action(action.id)
        assert fetched is not None
        assert fetched.parameters == params

    @pytest.mark.asyncio
    async def test_propose_action_creates_audit_entry(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Test propose audit",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )

        entries = await store.query_audit(pocket_id="pocket-1")
        events = [e.event for e in entries]
        assert "action_proposed" in events

        propose_entry = next(e for e in entries if e.event == "action_proposed")
        assert propose_entry.action_id == action.id
        assert propose_entry.pocket_id == "pocket-1"


class TestApproveAction:
    """test_approve_action — propose then approve, check status change + audit entry."""

    @pytest.mark.asyncio
    async def test_approve_action_changes_status(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Approve me",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )

        approved = await store.approve(action.id, approver="user:prakash")

        assert approved is not None
        assert approved.status == ActionStatus.APPROVED
        assert approved.approved_by == "user:prakash"

    @pytest.mark.asyncio
    async def test_approve_action_creates_audit_entry(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-2",
            title="Audit on approve",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(action.id, approver="user:admin")

        entries = await store.query_audit(pocket_id="pocket-2")
        events = [e.event for e in entries]
        assert "action_approved" in events

        approve_entry = next(e for e in entries if e.event == "action_approved")
        assert approve_entry.action_id == action.id


class TestRejectAction:
    """test_reject_action — propose then reject, check status change."""

    @pytest.mark.asyncio
    async def test_reject_action_changes_status(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Reject me",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )

        rejected = await store.reject(action.id)

        assert rejected is not None
        assert rejected.status == ActionStatus.REJECTED

    @pytest.mark.asyncio
    async def test_reject_action_creates_audit_entry(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Reject with audit",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.reject(action.id, reason="Not needed", rejector="user:manager")

        entries = await store.query_audit(pocket_id="pocket-1")
        events = [e.event for e in entries]
        assert "action_rejected" in events


class TestRejectActionWithReason:
    """test_reject_action_with_reason — verify rejection reason persists after round-trip."""

    @pytest.mark.asyncio
    async def test_rejection_reason_persists(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Costly action",
            description="Will cost $5000",
            recommendation="Proceed",
            trigger=make_trigger(),
        )

        rejected = await store.reject(action.id, reason="Budget not approved for Q1")

        assert rejected is not None
        assert rejected.rejected_reason == "Budget not approved for Q1"

    @pytest.mark.asyncio
    async def test_rejection_reason_retrievable_via_get_action(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="Another costly action",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.reject(action.id, reason="CEO said no")

        fetched = await store.get_action(action.id)
        assert fetched is not None
        assert fetched.rejected_reason == "CEO said no"
        assert fetched.status == ActionStatus.REJECTED

    @pytest.mark.asyncio
    async def test_rejection_without_reason_stores_empty(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="pocket-1",
            title="No reason reject",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        rejected = await store.reject(action.id)

        assert rejected is not None
        # rejected_reason should be None or empty when no reason given
        assert rejected.rejected_reason in (None, "")


class TestApproveNonexistent:
    """test_approve_nonexistent — approving unknown id returns None (not an exception)."""

    @pytest.mark.asyncio
    async def test_approve_nonexistent_returns_none(self, store: InstinctStore) -> None:
        result = await store.approve("act-does-not-exist-xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_reject_nonexistent_returns_none(self, store: InstinctStore) -> None:
        result = await store.reject("act-does-not-exist-xyz", reason="doesn't matter")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_action_nonexistent_returns_none(self, store: InstinctStore) -> None:
        result = await store.get_action("act-does-not-exist-xyz")
        assert result is None


class TestListPending:
    """test_list_pending — propose 3, approve 1, pending returns 2."""

    @pytest.mark.asyncio
    async def test_list_pending_excludes_approved(self, store: InstinctStore) -> None:
        a1 = await store.propose(
            pocket_id="pocket-1",
            title="Action A",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="pocket-1",
            title="Action B",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="pocket-1",
            title="Action C",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )

        await store.approve(a1.id)

        pending = await store.pending()
        assert len(pending) == 2
        pending_ids = {p.id for p in pending}
        assert a1.id not in pending_ids

    @pytest.mark.asyncio
    async def test_list_pending_filters_by_pocket_id(self, store: InstinctStore) -> None:
        await store.propose(
            pocket_id="pocket-A",
            title="For A",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="pocket-B",
            title="For B",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="pocket-A",
            title="For A again",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )

        pending_a = await store.pending(pocket_id="pocket-A")
        assert len(pending_a) == 2
        assert all(p.pocket_id == "pocket-A" for p in pending_a)

    @pytest.mark.asyncio
    async def test_list_pending_empty_when_all_resolved(self, store: InstinctStore) -> None:
        a = await store.propose(
            pocket_id="pocket-1",
            title="Will be approved",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        b = await store.propose(
            pocket_id="pocket-1",
            title="Will be rejected",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(a.id)
        await store.reject(b.id, reason="Not needed")

        pending = await store.pending()
        assert len(pending) == 0


class TestListActionsByStatus:
    """test_list_actions_by_status — filter actions by status via list_actions()."""

    @pytest.mark.asyncio
    async def test_filter_by_pending_status(self, store: InstinctStore) -> None:
        await store.propose(
            pocket_id="p1",
            title="Pending 1",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="p1",
            title="Pending 2",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        a3 = await store.propose(
            pocket_id="p1",
            title="Will approve",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(a3.id)

        pending_list = await store.list_actions(status=ActionStatus.PENDING)
        assert len(pending_list) == 2
        assert all(a.status == ActionStatus.PENDING for a in pending_list)

    @pytest.mark.asyncio
    async def test_filter_by_approved_status(self, store: InstinctStore) -> None:
        a1 = await store.propose(
            pocket_id="p1",
            title="Approve 1",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        a2 = await store.propose(
            pocket_id="p1",
            title="Approve 2",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="p1",
            title="Stay pending",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(a1.id)
        await store.approve(a2.id)

        approved_list = await store.list_actions(status=ActionStatus.APPROVED)
        assert len(approved_list) == 2
        assert all(a.status == ActionStatus.APPROVED for a in approved_list)

    @pytest.mark.asyncio
    async def test_filter_by_rejected_status(self, store: InstinctStore) -> None:
        a1 = await store.propose(
            pocket_id="p1",
            title="Reject this",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.propose(
            pocket_id="p1",
            title="Keep pending",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.reject(a1.id, reason="no longer needed")

        rejected_list = await store.list_actions(status=ActionStatus.REJECTED)
        assert len(rejected_list) == 1
        assert rejected_list[0].status == ActionStatus.REJECTED

    @pytest.mark.asyncio
    async def test_list_actions_no_filter_returns_all(self, store: InstinctStore) -> None:
        a1 = await store.propose(
            pocket_id="p1", title="A", description="", recommendation="", trigger=make_trigger()
        )
        a2 = await store.propose(
            pocket_id="p1", title="B", description="", recommendation="", trigger=make_trigger()
        )
        await store.approve(a1.id)
        await store.reject(a2.id)

        all_actions = await store.list_actions()
        assert len(all_actions) == 2

    @pytest.mark.asyncio
    async def test_list_actions_limit_is_respected(self, store: InstinctStore) -> None:
        for i in range(10):
            await store.propose(
                pocket_id="p1",
                title=f"Action {i}",
                description="",
                recommendation="",
                trigger=make_trigger(),
            )

        limited = await store.list_actions(limit=3)
        assert len(limited) == 3


class TestQueryAudit:
    """test_query_audit — verify audit entries are created on propose/approve/reject."""

    @pytest.mark.asyncio
    async def test_propose_creates_audit_entry(self, store: InstinctStore) -> None:
        await store.propose(
            pocket_id="audit-pocket",
            title="Test",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )

        entries = await store.query_audit(pocket_id="audit-pocket")
        assert len(entries) >= 1
        assert any(e.event == "action_proposed" for e in entries)

    @pytest.mark.asyncio
    async def test_approve_creates_audit_entry(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="audit-pocket",
            title="Test",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(action.id)

        entries = await store.query_audit(pocket_id="audit-pocket")
        assert any(e.event == "action_approved" for e in entries)

    @pytest.mark.asyncio
    async def test_reject_creates_audit_entry(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="audit-pocket",
            title="Test",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.reject(action.id, reason="No")

        entries = await store.query_audit(pocket_id="audit-pocket")
        assert any(e.event == "action_rejected" for e in entries)

    @pytest.mark.asyncio
    async def test_full_lifecycle_produces_three_audit_entries(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="lifecycle-pocket",
            title="Full lifecycle",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(action.id)
        await store.mark_executed(action.id, "Done")

        entries = await store.query_audit(pocket_id="lifecycle-pocket")
        events = {e.event for e in entries}
        assert "action_proposed" in events
        assert "action_approved" in events
        assert "action_executed" in events

    @pytest.mark.asyncio
    async def test_query_audit_filter_by_event(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="p1", title="A", description="", recommendation="", trigger=make_trigger()
        )
        await store.approve(action.id)
        await store.reject("act-nonexistent")  # returns None, no audit

        entries = await store.query_audit(event="action_approved")
        assert all(e.event == "action_approved" for e in entries)


class TestQueryAuditByCategory:
    """test_query_audit_by_category — filter audit entries by category."""

    @pytest.mark.asyncio
    async def test_filter_by_decision_category(self, store: InstinctStore) -> None:
        # Default category for action events is DECISION
        action = await store.propose(
            pocket_id="cat-pocket",
            title="Category test",
            description="",
            recommendation="",
            trigger=make_trigger(),
        )
        await store.approve(action.id)

        entries = await store.query_audit(
            pocket_id="cat-pocket", category=AuditCategory.DECISION.value
        )
        assert len(entries) >= 2
        assert all(e.category == AuditCategory.DECISION for e in entries)

    @pytest.mark.asyncio
    async def test_filter_by_security_category_returns_only_security(
        self, store: InstinctStore
    ) -> None:
        # Manually log a security event
        await store.log(
            actor="system",
            event="access_denied",
            description="Unauthorized access attempt",
            pocket_id="sec-pocket",
            category=AuditCategory.SECURITY,
        )
        # Also log a decision event
        await store.log(
            actor="agent:claude",
            event="action_proposed",
            description="Proposed some action",
            pocket_id="sec-pocket",
            category=AuditCategory.DECISION,
        )

        security_entries = await store.query_audit(
            pocket_id="sec-pocket", category=AuditCategory.SECURITY.value
        )
        assert len(security_entries) == 1
        assert security_entries[0].event == "access_denied"

    @pytest.mark.asyncio
    async def test_filter_by_data_category(self, store: InstinctStore) -> None:
        await store.log(
            actor="connector:stripe",
            event="data_synced",
            description="Synced 42 records",
            pocket_id="data-pocket",
            category=AuditCategory.DATA,
        )
        await store.log(
            actor="system",
            event="config_changed",
            description="Changed setting",
            pocket_id="data-pocket",
            category=AuditCategory.CONFIG,
        )

        data_entries = await store.query_audit(
            pocket_id="data-pocket", category=AuditCategory.DATA.value
        )
        assert len(data_entries) == 1
        assert data_entries[0].event == "data_synced"


class TestExportAudit:
    """test_export_audit — verify export returns all entries as valid JSON."""

    @pytest.mark.asyncio
    async def test_export_returns_valid_json(self, store: InstinctStore) -> None:
        await store.log(
            actor="system", event="test_event_1", description="First event", pocket_id="export-p"
        )
        await store.log(
            actor="agent:claude",
            event="test_event_2",
            description="Second event",
            pocket_id="export-p",
        )

        exported = await store.export_audit(pocket_id="export-p")
        parsed = json.loads(exported)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    @pytest.mark.asyncio
    async def test_export_includes_all_fields(self, store: InstinctStore) -> None:
        action = await store.propose(
            pocket_id="export-pocket",
            title="Export test action",
            description="Testing export",
            recommendation="Do it",
            trigger=make_trigger(source="test-agent"),
        )

        exported = await store.export_audit(pocket_id="export-pocket")
        parsed = json.loads(exported)

        assert len(parsed) >= 1
        entry = parsed[0]
        assert "id" in entry
        assert "actor" in entry
        assert "event" in entry
        assert "description" in entry
        assert "category" in entry
        assert entry["action_id"] == action.id

    @pytest.mark.asyncio
    async def test_export_without_pocket_filter_returns_all(self, store: InstinctStore) -> None:
        await store.log(actor="s", event="e1", description="d1", pocket_id="pocket-X")
        await store.log(actor="s", event="e2", description="d2", pocket_id="pocket-Y")

        exported = await store.export_audit()  # no pocket filter
        parsed = json.loads(exported)
        assert len(parsed) == 2

    @pytest.mark.asyncio
    async def test_export_empty_when_no_entries(self, store: InstinctStore) -> None:
        exported = await store.export_audit(pocket_id="nonexistent-pocket")
        parsed = json.loads(exported)
        assert parsed == []

    @pytest.mark.asyncio
    async def test_export_pocket_filter_isolates_entries(self, store: InstinctStore) -> None:
        await store.log(actor="s", event="e1", description="d1", pocket_id="pocket-A")
        await store.log(actor="s", event="e2", description="d2", pocket_id="pocket-B")
        await store.log(actor="s", event="e3", description="d3", pocket_id="pocket-A")

        exported = await store.export_audit(pocket_id="pocket-A")
        parsed = json.loads(exported)
        assert len(parsed) == 2
        assert all(e["pocket_id"] == "pocket-A" for e in parsed)


# ---------------------------------------------------------------------------
# Integration Tests: Router (FastAPI endpoints)
# ---------------------------------------------------------------------------

TRIGGER_PAYLOAD = {
    "type": "agent",
    "source": "claude",
    "reason": "Test trigger from unit tests",
}

PROPOSE_PAYLOAD = {
    "pocket_id": "pocket-router-test",
    "title": "Send restock alert",
    "description": "Stock at 5 units",
    "recommendation": "Order 30 units from default supplier",
    "trigger": TRIGGER_PAYLOAD,
    "category": "alert",
    "priority": "high",
    "parameters": {"quantity": 30},
}


class TestProposeActionEndpoint:
    """test_propose_action_endpoint — POST /instinct/actions."""

    def test_propose_returns_201_with_action(self, client: TestClient) -> None:
        resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"].startswith("act-")
        assert data["status"] == "pending"
        assert data["title"] == "Send restock alert"
        assert data["pocket_id"] == "pocket-router-test"
        assert data["priority"] == "high"
        assert data["category"] == "alert"

    def test_propose_missing_required_fields_returns_422(self, client: TestClient) -> None:
        resp = client.post("/instinct/actions", json={"title": "Missing fields"})
        assert resp.status_code == 422

    def test_propose_stores_parameters(self, client: TestClient) -> None:
        payload = {**PROPOSE_PAYLOAD, "parameters": {"threshold": 10, "auto_order": True}}
        resp = client.post("/instinct/actions", json=payload)
        assert resp.status_code == 201
        assert resp.json()["parameters"] == {"threshold": 10, "auto_order": True}

    def test_propose_default_category_is_workflow(self, client: TestClient) -> None:
        payload = {
            "pocket_id": "p1",
            "title": "Default category test",
            "trigger": TRIGGER_PAYLOAD,
        }
        resp = client.post("/instinct/actions", json=payload)
        assert resp.status_code == 201
        assert resp.json()["category"] == "workflow"


class TestListPendingEndpoint:
    """test_list_pending_endpoint — GET /instinct/actions/pending."""

    def test_list_pending_returns_pending_actions(self, client: TestClient) -> None:
        client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "title": "Second action"})

        resp = client.get("/instinct/actions/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert all(a["status"] == "pending" for a in data)

    def test_list_pending_empty_initially(self, client: TestClient) -> None:
        resp = client.get("/instinct/actions/pending")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_pending_filters_by_pocket_id(self, client: TestClient) -> None:
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "pocket-A"})
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "pocket-B"})

        resp = client.get("/instinct/actions/pending?pocket_id=pocket-A")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["pocket_id"] == "pocket-A"


class TestListAllActionsEndpoint:
    """test_list_all_actions_endpoint — GET /instinct/actions."""

    def test_list_actions_returns_response_with_total(self, client: TestClient) -> None:
        client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "title": "Action 2"})

        resp = client.get("/instinct/actions")
        assert resp.status_code == 200
        data = resp.json()
        assert "actions" in data
        assert "total" in data
        assert data["total"] == 2
        assert len(data["actions"]) == 2

    def test_list_actions_empty_store(self, client: TestClient) -> None:
        resp = client.get("/instinct/actions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["actions"] == []

    def test_list_actions_filter_by_status(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "title": "Stay pending"})

        client.post(f"/instinct/actions/{action_id}/approve")

        resp = client.get("/instinct/actions?status=approved")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["actions"][0]["status"] == "approved"

    def test_list_actions_filter_by_pocket_id(self, client: TestClient) -> None:
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "pocketX"})
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "pocketY"})

        resp = client.get("/instinct/actions?pocket_id=pocketX")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["actions"][0]["pocket_id"] == "pocketX"

    def test_list_actions_respects_limit(self, client: TestClient) -> None:
        for i in range(5):
            client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "title": f"Action {i}"})

        resp = client.get("/instinct/actions?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["actions"]) == 2
        assert data["total"] == 2


class TestApproveEndpoint:
    """test_approve_endpoint — POST /instinct/actions/{id}/approve."""

    def test_approve_returns_approved_action(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]

        approve_resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert approve_resp.status_code == 200
        data = approve_resp.json()
        assert data["status"] == "approved"
        assert data["id"] == action_id

    def test_approve_removes_from_pending(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]

        client.post(f"/instinct/actions/{action_id}/approve")

        pending_resp = client.get("/instinct/actions/pending")
        pending_ids = [a["id"] for a in pending_resp.json()]
        assert action_id not in pending_ids


class TestRejectEndpoint:
    """test_reject_endpoint — POST /instinct/actions/{id}/reject."""

    def test_reject_returns_rejected_action(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]

        reject_resp = client.post(
            f"/instinct/actions/{action_id}/reject",
            json={"reason": "Not in budget"},
        )
        assert reject_resp.status_code == 200
        data = reject_resp.json()
        assert data["status"] == "rejected"
        assert data["rejected_reason"] == "Not in budget"

    def test_reject_without_reason_body(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]

        reject_resp = client.post(f"/instinct/actions/{action_id}/reject")
        assert reject_resp.status_code == 200
        assert reject_resp.json()["status"] == "rejected"

    def test_reject_removes_from_pending(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]

        client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "Nope"})

        pending_resp = client.get("/instinct/actions/pending")
        pending_ids = [a["id"] for a in pending_resp.json()]
        assert action_id not in pending_ids


class TestAuditEndpoint:
    """test_audit_endpoint — GET /instinct/audit."""

    def test_audit_returns_entries_with_total(self, client: TestClient) -> None:
        client.post("/instinct/actions", json=PROPOSE_PAYLOAD)

        resp = client.get("/instinct/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data
        assert data["total"] >= 1

    def test_audit_empty_initially(self, client: TestClient) -> None:
        resp = client.get("/instinct/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["entries"] == []

    def test_audit_filter_by_pocket_id(self, client: TestClient) -> None:
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "audit-pocket-A"})
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "audit-pocket-B"})

        resp = client.get("/instinct/audit?pocket_id=audit-pocket-A")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["pocket_id"] == "audit-pocket-A" for e in data["entries"])

    def test_audit_filter_by_event(self, client: TestClient) -> None:
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]
        client.post(f"/instinct/actions/{action_id}/approve")

        resp = client.get("/instinct/audit?event=action_proposed")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["event"] == "action_proposed" for e in data["entries"])

    def test_audit_filter_by_category(self, client: TestClient) -> None:
        client.post("/instinct/actions", json=PROPOSE_PAYLOAD)

        resp = client.get("/instinct/audit?category=decision")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["category"] == "decision" for e in data["entries"])


class TestAuditExportEndpoint:
    """test_audit_export_endpoint — GET /instinct/audit/export."""

    def test_export_returns_json_attachment(self, client: TestClient) -> None:
        client.post("/instinct/actions", json=PROPOSE_PAYLOAD)

        resp = client.get("/instinct/audit/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "instinct_audit.json" in resp.headers.get("content-disposition", "")

    def test_export_content_is_valid_json_list(self, client: TestClient) -> None:
        client.post("/instinct/actions", json=PROPOSE_PAYLOAD)

        resp = client.get("/instinct/audit/export")
        parsed = resp.json()
        assert isinstance(parsed, list)
        assert len(parsed) >= 1

    def test_export_empty_store_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/instinct/audit/export")
        parsed = resp.json()
        assert parsed == []

    def test_export_filter_by_pocket_id(self, client: TestClient) -> None:
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "export-A"})
        client.post("/instinct/actions", json={**PROPOSE_PAYLOAD, "pocket_id": "export-B"})

        resp = client.get("/instinct/audit/export?pocket_id=export-A")
        parsed = resp.json()
        assert all(e["pocket_id"] == "export-A" for e in parsed)


class TestApproveNonexistentEndpoint:
    """test_approve_nonexistent_endpoint — approve unknown id should return 404."""

    def test_approve_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.post("/instinct/actions/act-does-not-exist/approve")
        assert resp.status_code == 404

    def test_reject_nonexistent_returns_404(self, client: TestClient) -> None:
        resp = client.post(
            "/instinct/actions/act-does-not-exist/reject",
            json={"reason": "whatever"},
        )
        assert resp.status_code == 404


class TestFullLifecycle:
    """test_full_lifecycle — propose → approve → verify audit trail end-to-end."""

    def test_full_happy_path(self, client: TestClient) -> None:
        # Step 1: propose
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        assert propose_resp.status_code == 201
        action = propose_resp.json()
        action_id = action["id"]
        assert action["status"] == "pending"

        # Step 2: appears in pending list
        pending_resp = client.get("/instinct/actions/pending")
        pending_ids = [a["id"] for a in pending_resp.json()]
        assert action_id in pending_ids

        # Step 3: approve
        approve_resp = client.post(f"/instinct/actions/{action_id}/approve")
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"

        # Step 4: no longer in pending
        pending_resp_after = client.get("/instinct/actions/pending")
        pending_ids_after = [a["id"] for a in pending_resp_after.json()]
        assert action_id not in pending_ids_after

        # Step 5: appears in approved list
        all_resp = client.get("/instinct/actions?status=approved")
        approved_ids = [a["id"] for a in all_resp.json()["actions"]]
        assert action_id in approved_ids

        # Step 6: audit trail has both propose and approve entries
        audit_resp = client.get(f"/instinct/audit?pocket_id={PROPOSE_PAYLOAD['pocket_id']}")
        events = {e["event"] for e in audit_resp.json()["entries"]}
        assert "action_proposed" in events
        assert "action_approved" in events

    def test_full_reject_path(self, client: TestClient) -> None:
        # Propose
        propose_resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        action_id = propose_resp.json()["id"]

        # Reject with reason
        reject_resp = client.post(
            f"/instinct/actions/{action_id}/reject",
            json={"reason": "Too costly for this quarter"},
        )
        assert reject_resp.status_code == 200
        rejected = reject_resp.json()
        assert rejected["status"] == "rejected"
        assert rejected["rejected_reason"] == "Too costly for this quarter"

        # Audit trail has reject entry
        audit_resp = client.get("/instinct/audit?event=action_rejected")
        assert audit_resp.json()["total"] >= 1

        # Export includes the rejection
        export_resp = client.get("/instinct/audit/export")
        export_data = export_resp.json()
        events_in_export = {e["event"] for e in export_data}
        assert "action_rejected" in events_in_export

    def test_propose_multiple_then_approve_one(self, client: TestClient) -> None:
        # Propose three actions
        ids = []
        for title in ["Alpha", "Beta", "Gamma"]:
            resp = client.post(
                "/instinct/actions",
                json={**PROPOSE_PAYLOAD, "title": title},
            )
            ids.append(resp.json()["id"])

        # Approve just the first one
        client.post(f"/instinct/actions/{ids[0]}/approve")

        # Pending count should be 2
        pending = client.get("/instinct/actions/pending").json()
        assert len(pending) == 2

        # Approved count should be 1
        approved = client.get("/instinct/actions?status=approved").json()
        assert approved["total"] == 1
        assert approved["actions"][0]["id"] == ids[0]

        # Total in store is 3
        all_actions = client.get("/instinct/actions").json()
        assert all_actions["total"] == 3
