# tests/cloud/fabric_ingest/test_fabric_ingest_connector_dispatch.py
# Created: 2026-07-11 (feat/real-pipeline-s1) — connector-source dispatch.
#
# Pins the transform surface's dispatch contract:
#
#   1. model back-compat — a stored mapping WITHOUT the new fields parses as
#      source_kind="firestore", connector_id=None (existing configs are
#      untouched by the migration-free additive change).
#   2. happy path — a "connector" mapping resolves the workspace's ENABLED
#      WorkspaceConnector row and calls the registered ingestor with the
#      tenant store, workspace_id, and the row's user_id (the per-user OAuth
#      token seam); the run reports ok + objects, state bookkeeping advances.
#   3. user_id=None (workspace-scoped connector) → the shared bucket is used.
#   4. missing / DISABLED connector → error RESULT, never a raise; the
#      ingestor is not called.
#   5. unregistered connector id → error result, never a raise.
#   6. tenancy — workspace A's run never resolves workspace B's connector row.
#   7. the ingestor raising → error result (per-collection isolation holds).
#
# The registry is spied via monkeypatch.setitem on FABRIC_INGESTORS (the lazy
# builtin seed uses setdefault, so a pre-installed spy is never clobbered).

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.fabric_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector  # noqa: E402
from pocketpaw_ee.cloud.models.fabric_ingest_state import (  # noqa: E402
    FabricFieldMapping,
    FabricIngestConfig,
    FabricIngestState,
)

import pocketpaw.connectors.fabric_ingest as oss_fabric_ingest  # noqa: E402
from pocketpaw.connectors.fabric_ingest import IngestResult  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FakeStore:
    """Dispatch tests never touch Fabric — the spy ingestor absorbs the store."""


class SpyIngestor:
    """Records every call; returns a canned IngestResult (or raises)."""

    def __init__(self, result: IngestResult | None = None, exc: Exception | None = None):
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.result = result or IngestResult(type_name="CalendarEvent", created=2, updated=1)
        self.exc = exc

    async def __call__(self, store: Any, **kwargs: Any) -> IngestResult:
        self.calls.append((store, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


def _connector_mapping(name: str = "gcalendar", **overrides: Any) -> FabricFieldMapping:
    kwargs: dict[str, Any] = {
        "collection": name,
        "object_type_id": "ot-calendar-event",
        "source_kind": "connector",
    }
    kwargs.update(overrides)
    return FabricFieldMapping(**kwargs)


async def _seed_config(workspace_id: str, mappings: list[FabricFieldMapping]) -> None:
    await FabricIngestConfig(workspace=workspace_id, enabled=True, mappings=mappings).insert()


async def _seed_connector(
    workspace_id: str,
    name: str = "gcalendar",
    *,
    enabled: bool = True,
    user_id: str | None = None,
) -> None:
    await WorkspaceConnector(
        workspace=workspace_id,
        name=name,
        enabled=enabled,
        scope="user" if user_id else "workspace",
        user_id=user_id,
    ).insert()


# --------------------------------------------------------------------------
# 1 — model back-compat: existing configs parse as firestore.
# --------------------------------------------------------------------------


async def test_mapping_without_new_fields_defaults_to_firestore():
    """A raw stored mapping dict from BEFORE this change parses unchanged."""
    m = FabricFieldMapping.model_validate(
        {
            "collection": "customers",
            "object_type_id": "ot-customer",
            "field_map": {"display_name": "name"},
            "cursor_field": "updated_at",
        }
    )
    assert m.source_kind == "firestore"
    assert m.connector_id is None


# --------------------------------------------------------------------------
# 2/3 — happy path: WorkspaceConnector resolved, ingestor called with the seam.
# --------------------------------------------------------------------------


async def test_connector_mapping_dispatches_with_per_user_token(mongo_db, monkeypatch):  # noqa: ARG001
    ws = "w1"
    await _seed_config(ws, [_connector_mapping()])
    await _seed_connector(ws, user_id="u42")

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)
    store = FakeStore()

    result = await ingest_service.ingest_collection(ws, "gcalendar", store=store)

    assert result["status"] == "ok", result
    assert result["objects"] == 3  # created=2 + updated=1
    assert result["errors"] == []
    # The seam: tenant store + workspace + the CONNECTOR ROW's user token.
    assert len(spy.calls) == 1
    called_store, kwargs = spy.calls[0]
    assert called_store is store
    assert kwargs == {"workspace_id": ws, "user_id": "u42"}
    # State bookkeeping reused verbatim.
    state = await FabricIngestState.find_one(
        FabricIngestState.workspace == ws, FabricIngestState.source_id == "gcalendar"
    )
    assert state is not None
    assert state.status == "ok"
    assert state.backfill_done is True
    assert state.objects_ingested == 3
    assert state.cursor == ""  # connector runs never touch the firestore cursor


async def test_workspace_scoped_connector_uses_shared_bucket(mongo_db, monkeypatch):  # noqa: ARG001
    ws = "w1"
    await _seed_config(ws, [_connector_mapping()])
    await _seed_connector(ws, user_id=None)  # workspace scope — no per-user token

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)

    result = await ingest_service.ingest_collection(ws, "gcalendar", store=FakeStore())

    assert result["status"] == "ok", result
    assert spy.calls[0][1]["user_id"] is None


async def test_connector_id_overrides_collection_as_registry_key(mongo_db, monkeypatch):  # noqa: ARG001
    """An explicit connector_id wins over the collection routing key."""
    ws = "w1"
    await _seed_config(ws, [_connector_mapping("cal-main", connector_id="gcalendar")])
    await _seed_connector(ws, name="gcalendar", user_id="u7")

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)

    result = await ingest_service.ingest_collection(ws, "cal-main", store=FakeStore())

    assert result["status"] == "ok", result
    assert spy.calls[0][1] == {"workspace_id": ws, "user_id": "u7"}


# --------------------------------------------------------------------------
# 4/5 — misconfiguration: error results, never raises, ingestor untouched.
# --------------------------------------------------------------------------


async def test_missing_connector_is_error_result_not_raise(mongo_db, monkeypatch):  # noqa: ARG001
    ws = "w1"
    await _seed_config(ws, [_connector_mapping()])
    # No WorkspaceConnector row at all.

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)

    result = await ingest_service.ingest_collection(ws, "gcalendar", store=FakeStore())

    assert result["status"] == "error"
    assert result["objects"] == 0
    assert any("not connected/enabled" in e for e in result["errors"]), result["errors"]
    assert spy.calls == []


async def test_disabled_connector_is_error_result(mongo_db, monkeypatch):  # noqa: ARG001
    ws = "w1"
    await _seed_config(ws, [_connector_mapping()])
    await _seed_connector(ws, enabled=False, user_id="u42")

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)

    result = await ingest_service.ingest_collection(ws, "gcalendar", store=FakeStore())

    assert result["status"] == "error"
    assert any("not connected/enabled" in e for e in result["errors"])
    assert spy.calls == []


async def test_unregistered_connector_is_error_result(mongo_db):  # noqa: ARG001
    ws = "w1"
    await _seed_config(ws, [_connector_mapping("no-such-connector")])
    await _seed_connector(ws, name="no-such-connector")

    result = await ingest_service.ingest_collection(ws, "no-such-connector", store=FakeStore())

    assert result["status"] == "error"
    assert any("no fabric ingestor registered" in e for e in result["errors"]), result["errors"]


# --------------------------------------------------------------------------
# 6 — tenancy: another workspace's connector row never satisfies the lookup.
# --------------------------------------------------------------------------


async def test_other_tenants_connector_row_is_not_resolved(mongo_db, monkeypatch):  # noqa: ARG001
    await _seed_config("w1", [_connector_mapping()])
    await _seed_connector("w-OTHER", user_id="intruder")  # only the OTHER tenant connected

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)

    result = await ingest_service.ingest_collection("w1", "gcalendar", store=FakeStore())

    assert result["status"] == "error"
    assert spy.calls == []


# --------------------------------------------------------------------------
# 7 — a genuine ingestor failure lands as an error result (isolation holds).
# --------------------------------------------------------------------------


async def test_ingestor_exception_is_error_result(mongo_db, monkeypatch):  # noqa: ARG001
    ws = "w1"
    await _seed_config(ws, [_connector_mapping()])
    await _seed_connector(ws, user_id="u42")

    spy = SpyIngestor(exc=RuntimeError("token expired"))
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)

    result = await ingest_service.ingest_collection(ws, "gcalendar", store=FakeStore())

    assert result["status"] == "error"
    assert any("token expired" in e for e in result["errors"])
    state = await FabricIngestState.find_one(
        FabricIngestState.workspace == ws, FabricIngestState.source_id == "gcalendar"
    )
    assert state is not None
    assert state.status == "error"
    assert state.backfill_done is False  # a failed first run retries the full read
