# test_audit.py — Tests for the enterprise audit log module.
# Created: 2026-03-27
# TDD: tests written before implementation.
# Covers AuditEntry model, AuditStore (log/query/export), and API endpoints.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw.audit.store import AuditStore

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_db(tmp_path) -> AuditStore:
    """Isolated in-memory (tmp file) AuditStore per test."""
    from pocketpaw.audit.store import AuditStore

    return AuditStore(db_path=tmp_path / "audit.db")


@pytest.fixture
def sample_entry_data():
    return {
        "pocket_id": "pocket-abc",
        "actor": "agent",
        "action": "create_pocket",
        "category": "decision",
        "description": "Agent created inventory pocket",
        "context": {"query": "inventory levels Q1"},
        "ai_recommendation": "Create pocket with 3 widgets",
        "outcome": "Pocket created successfully",
        "status": "completed",
        "metadata": {"tool": "create_pocket"},
    }


@pytest.fixture
def populated_store(audit_db, sample_entry_data):
    """Store pre-populated with several entries for filter testing."""
    import asyncio

    entries = [
        {**sample_entry_data, "category": "decision", "actor": "agent", "pocket_id": "pocket-1"},
        {**sample_entry_data, "category": "data", "actor": "user:prakash", "pocket_id": "pocket-1"},
        {**sample_entry_data, "category": "security", "actor": "system", "pocket_id": "pocket-2"},
        {**sample_entry_data, "category": "decision", "actor": "agent", "pocket_id": "pocket-2"},
        {
            **sample_entry_data,
            "category": "config",
            "actor": "user:prakash",
            "pocket_id": "pocket-1",
        },
    ]

    async def _populate():
        for e in entries:
            await audit_db.log_entry(**e)

    asyncio.get_event_loop().run_until_complete(_populate())
    return audit_db


# ---------------------------------------------------------------------------
# AuditEntry model tests
# ---------------------------------------------------------------------------


class TestAuditEntryModel:
    def test_model_has_required_fields(self):
        from pocketpaw.audit.models import AuditEntry

        entry = AuditEntry(
            actor="agent",
            action="create_pocket",
            category="decision",
            description="Agent created a pocket",
        )
        assert entry.id is not None
        assert entry.timestamp is not None
        assert entry.actor == "agent"
        assert entry.action == "create_pocket"
        assert entry.category == "decision"
        assert entry.description == "Agent created a pocket"

    def test_model_defaults(self):
        from pocketpaw.audit.models import AuditEntry

        entry = AuditEntry(
            actor="system",
            action="connector_sync",
            category="data",
            description="Synced Stripe connector",
        )
        assert entry.status == "completed"
        assert entry.context == {}
        assert entry.metadata == {}
        assert entry.pocket_id is None
        assert entry.ai_recommendation is None
        assert entry.outcome is None

    def test_model_id_is_unique(self):
        from pocketpaw.audit.models import AuditEntry

        a = AuditEntry(actor="agent", action="x", category="decision", description="x")
        b = AuditEntry(actor="agent", action="x", category="decision", description="x")
        assert a.id != b.id

    def test_model_timestamp_is_utc_iso(self):
        from pocketpaw.audit.models import AuditEntry

        entry = AuditEntry(actor="agent", action="x", category="decision", description="x")
        # Should be parseable as ISO datetime
        dt = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
        assert dt is not None

    def test_model_rejects_invalid_status(self):
        from pocketpaw.audit.models import AuditEntry

        with pytest.raises(Exception):
            AuditEntry(
                actor="agent",
                action="x",
                category="decision",
                description="x",
                status="invalid_status",
            )

    def test_model_rejects_invalid_category(self):
        from pocketpaw.audit.models import AuditEntry

        with pytest.raises(Exception):
            AuditEntry(
                actor="agent",
                action="x",
                category="unknown_cat",
                description="x",
            )


# ---------------------------------------------------------------------------
# AuditStore tests
# ---------------------------------------------------------------------------


class TestAuditStoreLogEntry:
    @pytest.mark.asyncio
    async def test_log_entry_returns_entry_id(self, audit_db, sample_entry_data):
        entry_id = await audit_db.log_entry(**sample_entry_data)
        assert isinstance(entry_id, str)
        assert len(entry_id) > 0

    @pytest.mark.asyncio
    async def test_log_entry_persists_to_db(self, audit_db, sample_entry_data):
        entry_id = await audit_db.log_entry(**sample_entry_data)
        entries = await audit_db.query_entries()
        assert any(e.id == entry_id for e in entries)

    @pytest.mark.asyncio
    async def test_log_entry_stores_all_fields(self, audit_db, sample_entry_data):
        entry_id = await audit_db.log_entry(**sample_entry_data)
        entries = await audit_db.query_entries()
        entry = next(e for e in entries if e.id == entry_id)

        assert entry.pocket_id == "pocket-abc"
        assert entry.actor == "agent"
        assert entry.action == "create_pocket"
        assert entry.category == "decision"
        assert entry.description == "Agent created inventory pocket"
        assert entry.context["query"] == "inventory levels Q1"
        assert entry.ai_recommendation == "Create pocket with 3 widgets"
        assert entry.outcome == "Pocket created successfully"
        assert entry.status == "completed"
        assert entry.metadata["tool"] == "create_pocket"

    @pytest.mark.asyncio
    async def test_log_entry_without_optional_fields(self, audit_db):
        entry_id = await audit_db.log_entry(
            actor="system",
            action="connector_sync",
            category="data",
            description="Synced connector",
        )
        entries = await audit_db.query_entries()
        entry = next(e for e in entries if e.id == entry_id)
        assert entry.pocket_id is None
        assert entry.ai_recommendation is None
        assert entry.outcome is None


class TestAuditStoreQueryEntries:
    @pytest.mark.asyncio
    async def test_query_all_entries(self, populated_store):
        entries = await populated_store.query_entries()
        assert len(entries) == 5

    @pytest.mark.asyncio
    async def test_filter_by_pocket_id(self, populated_store):
        entries = await populated_store.query_entries(pocket_id="pocket-1")
        assert len(entries) == 3
        assert all(e.pocket_id == "pocket-1" for e in entries)

    @pytest.mark.asyncio
    async def test_filter_by_category(self, populated_store):
        entries = await populated_store.query_entries(category="decision")
        assert len(entries) == 2
        assert all(e.category == "decision" for e in entries)

    @pytest.mark.asyncio
    async def test_filter_by_actor(self, populated_store):
        entries = await populated_store.query_entries(actor="user:prakash")
        assert len(entries) == 2
        assert all(e.actor == "user:prakash" for e in entries)

    @pytest.mark.asyncio
    async def test_filter_by_date_range(self, audit_db):

        past = datetime.now(UTC) - timedelta(hours=2)
        future = datetime.now(UTC) + timedelta(hours=2)

        await audit_db.log_entry(
            actor="agent",
            action="x",
            category="decision",
            description="entry 1",
        )

        entries = await audit_db.query_entries(date_from=past, date_to=future)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_filter_excludes_outside_date_range(self, audit_db):
        await audit_db.log_entry(
            actor="agent",
            action="x",
            category="decision",
            description="entry 1",
        )

        # Query for a range entirely in the past
        past_start = datetime.now(UTC) - timedelta(hours=10)
        past_end = datetime.now(UTC) - timedelta(hours=5)
        entries = await audit_db.query_entries(date_from=past_start, date_to=past_end)
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_query_returns_entries_newest_first(self, populated_store):
        entries = await populated_store.query_entries()
        timestamps = [e.timestamp for e in entries]
        # Should be sorted descending
        assert timestamps == sorted(timestamps, reverse=True)

    @pytest.mark.asyncio
    async def test_query_limit(self, populated_store):
        entries = await populated_store.query_entries(limit=2)
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_query_combined_filters(self, populated_store):
        entries = await populated_store.query_entries(pocket_id="pocket-1", category="decision")
        assert len(entries) == 1
        assert entries[0].pocket_id == "pocket-1"
        assert entries[0].category == "decision"


class TestAuditStoreExport:
    @pytest.mark.asyncio
    async def test_export_csv_returns_bytes(self, populated_store):
        data = await populated_store.export_csv()
        assert isinstance(data, bytes)
        assert len(data) > 0

    @pytest.mark.asyncio
    async def test_export_csv_has_header_row(self, populated_store):
        data = await populated_store.export_csv()
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        headers = reader.fieldnames
        assert "id" in headers
        assert "timestamp" in headers
        assert "actor" in headers
        assert "action" in headers
        assert "category" in headers
        assert "description" in headers
        assert "status" in headers

    @pytest.mark.asyncio
    async def test_export_csv_row_count_matches_entries(self, populated_store):
        data = await populated_store.export_csv()
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        rows = list(reader)
        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_export_csv_respects_pocket_id_filter(self, populated_store):
        data = await populated_store.export_csv(pocket_id="pocket-1")
        reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
        rows = list(reader)
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_export_json_returns_list(self, populated_store):
        data = await populated_store.export_json()
        parsed = json.loads(data)
        assert isinstance(parsed, list)
        assert len(parsed) == 5

    @pytest.mark.asyncio
    async def test_export_json_respects_pocket_id_filter(self, populated_store):
        data = await populated_store.export_json(pocket_id="pocket-1")
        parsed = json.loads(data)
        assert len(parsed) == 3


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(tmp_path):
    """FastAPI test client with the audit router mounted."""
    from pocketpaw.audit.router import router as audit_router
    from pocketpaw.audit.store import AuditStore, get_audit_store

    # Override the store dependency to use a temp DB
    store = AuditStore(db_path=tmp_path / "api_audit.db")

    app = FastAPI()
    app.include_router(audit_router, prefix="/api/v1")

    # Override the store dependency
    app.dependency_overrides[get_audit_store] = lambda: store

    return TestClient(app), store


class TestAuditAPIQuery:
    @pytest.mark.asyncio
    async def test_get_audit_empty(self, api_client):
        client, _ = api_client
        resp = client.get("/api/v1/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_get_audit_returns_entries(self, api_client):
        client, store = api_client
        await store.log_entry(
            actor="agent",
            action="create_pocket",
            category="decision",
            description="Agent created a pocket",
        )
        resp = client.get("/api/v1/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_get_audit_filter_by_pocket_id(self, api_client):
        client, store = api_client
        await store.log_entry(
            actor="agent",
            action="x",
            category="decision",
            description="d",
            pocket_id="pocket-1",
        )
        await store.log_entry(
            actor="agent",
            action="x",
            category="decision",
            description="d",
            pocket_id="pocket-2",
        )
        resp = client.get("/api/v1/audit?pocket_id=pocket-1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["pocket_id"] == "pocket-1"

    @pytest.mark.asyncio
    async def test_get_audit_filter_by_category(self, api_client):
        client, store = api_client
        await store.log_entry(actor="agent", action="x", category="decision", description="d")
        await store.log_entry(actor="agent", action="x", category="security", description="d")
        resp = client.get("/api/v1/audit?category=security")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["category"] == "security"

    @pytest.mark.asyncio
    async def test_get_audit_entry_shape(self, api_client):
        client, store = api_client
        await store.log_entry(
            actor="agent",
            action="create_pocket",
            category="decision",
            description="Created pocket",
            ai_recommendation="Use 3 widgets",
            outcome="Done",
            status="completed",
        )
        resp = client.get("/api/v1/audit")
        entry = resp.json()["entries"][0]
        assert "id" in entry
        assert "timestamp" in entry
        assert "actor" in entry
        assert "action" in entry
        assert "category" in entry
        assert "description" in entry
        assert "status" in entry
        assert "ai_recommendation" in entry
        assert "outcome" in entry


class TestAuditAPIExport:
    @pytest.mark.asyncio
    async def test_export_csv_returns_csv_content_type(self, api_client):
        client, store = api_client
        await store.log_entry(actor="agent", action="x", category="decision", description="d")
        resp = client.get("/api/v1/audit/export?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_export_json_returns_json(self, api_client):
        client, store = api_client
        await store.log_entry(actor="agent", action="x", category="decision", description="d")
        resp = client.get("/api/v1/audit/export?format=json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_export_csv_respects_pocket_id(self, api_client):
        client, store = api_client
        await store.log_entry(
            actor="agent",
            action="x",
            category="decision",
            description="d",
            pocket_id="p1",
        )
        await store.log_entry(
            actor="agent",
            action="x",
            category="decision",
            description="d",
            pocket_id="p2",
        )
        resp = client.get("/api/v1/audit/export?format=csv&pocket_id=p1")
        assert resp.status_code == 200
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["pocket_id"] == "p1"

    @pytest.mark.asyncio
    async def test_export_invalid_format_returns_400(self, api_client):
        client, _ = api_client
        resp = client.get("/api/v1/audit/export?format=xml")
        assert resp.status_code == 422  # FastAPI validation error


# ---------------------------------------------------------------------------
# Integration: tool execution auto-logging
# ---------------------------------------------------------------------------


class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_log_tool_execution(self, audit_db):
        """log_tool_execution helper logs a tool action."""

        entry_id = await audit_db.log_tool_execution(
            tool_name="web_search",
            actor="agent",
            description="Searched for inventory data",
            context={"query": "inventory Q1 2026"},
            pocket_id="pocket-123",
        )
        entries = await audit_db.query_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.id == entry_id
        assert entry.action == "tool_execution"
        assert entry.category == "decision"
        assert entry.actor == "agent"
        assert entry.metadata["tool"] == "web_search"

    @pytest.mark.asyncio
    async def test_log_connector_sync(self, audit_db):
        """log_connector_sync helper logs a data sync event."""
        entry_id = await audit_db.log_connector_sync(
            connector_name="stripe",
            actor="system",
            description="Synced Stripe invoices",
            record_count=42,
        )
        entries = await audit_db.query_entries()
        entry = entries[0]
        assert entry.id == entry_id
        assert entry.action == "connector_sync"
        assert entry.category == "data"
        assert entry.metadata["connector"] == "stripe"
        assert entry.metadata["record_count"] == 42
