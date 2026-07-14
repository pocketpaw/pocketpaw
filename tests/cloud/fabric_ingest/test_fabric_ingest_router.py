# tests/cloud/fabric_ingest/test_fabric_ingest_router.py
# Created: 2026-07-11 (feat/real-pipeline-s1) — the transform-surface router.
#
# Pins the HTTP contract of /fabric/ingest/*:
#
#   1. CRUD — POST authors a mapping (201), GET lists it, POST again with the
#      same collection REPLACES (no duplicate), DELETE removes it (204) and a
#      second DELETE 404s.
#   2. validation — a malformed mapping (blank object_type_id) 422s at entry.
#   3. tenancy — GET only ever returns the CALLER's workspace's mappings even
#      when another tenant has its own config; POST writes only to the
#      caller's config row.
#   4. RBAC — a caller who is not a member of their active workspace 403s on
#      every route (fabric.read gates the GET, fabric.write gates the rest).
#   5. run-now — POST /fabric/ingest/run dispatches the mapping through the
#      service (connector mapping → spied registry ingestor, 200 + status ok);
#      an unknown collection reports status="error" in the body (never a 5xx).
#
# Harness matches tests/cloud/test_fabric_objects_gate.py: the real router on
# a bare FastAPI app with require_license / current_active_user /
# current_workspace_id overridden and get_workspace_plan patched to
# enterprise, over mongomock Beanie (mongo_db).

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.fabric_ingest.router import router  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector  # noqa: E402
from pocketpaw_ee.cloud.models.fabric_ingest_state import (  # noqa: E402
    FabricFieldMapping,
    FabricIngestConfig,
)

import pocketpaw.connectors.fabric_ingest as oss_fabric_ingest  # noqa: E402
from pocketpaw.connectors.fabric_ingest import IngestResult  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Harness (same shape as test_fabric_objects_gate.py)
# --------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "u1", workspace_id: str = "w1", member: bool = True) -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        # member=False models a caller whose active workspace they do NOT
        # belong to — require_action_any_workspace must 403 them.
        self.workspaces = [_FakeMembership(workspace=workspace_id)] if member else []


def _make_client(user: _FakeUser, monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _mapping_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "collection": "gcalendar",
        "object_type_id": "ot-calendar-event",
        "source_kind": "connector",
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# 1 — CRUD round-trip
# --------------------------------------------------------------------------


async def test_crud_roundtrip(mongo_db, monkeypatch):  # noqa: ARG001
    client = _make_client(_FakeUser(), monkeypatch)

    # Author.
    resp = client.post("/fabric/ingest/mappings", json=_mapping_body())
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["collection"] == "gcalendar"
    assert created["source_kind"] == "connector"

    # List.
    resp = client.get("/fabric/ingest/mappings")
    assert resp.status_code == 200, resp.text
    assert [m["collection"] for m in resp.json()["mappings"]] == ["gcalendar"]

    # Upsert (same collection) REPLACES, never duplicates.
    resp = client.post("/fabric/ingest/mappings", json=_mapping_body(object_type_id="ot-updated"))
    assert resp.status_code == 201, resp.text
    mappings = client.get("/fabric/ingest/mappings").json()["mappings"]
    assert len(mappings) == 1
    assert mappings[0]["object_type_id"] == "ot-updated"

    # Delete.
    resp = client.delete("/fabric/ingest/mappings", params={"collection": "gcalendar"})
    assert resp.status_code == 204, resp.text
    assert client.get("/fabric/ingest/mappings").json()["mappings"] == []

    # Second delete 404s.
    resp = client.delete("/fabric/ingest/mappings", params={"collection": "gcalendar"})
    assert resp.status_code == 404, resp.text


async def test_malformed_mapping_422s(mongo_db, monkeypatch):  # noqa: ARG001
    client = _make_client(_FakeUser(), monkeypatch)
    resp = client.post("/fabric/ingest/mappings", json=_mapping_body(object_type_id=""))
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------
# 3 — tenancy: caller only sees / writes its own workspace's config
# --------------------------------------------------------------------------


async def test_list_is_tenant_filtered(mongo_db, monkeypatch):  # noqa: ARG001
    await FabricIngestConfig(
        workspace="w-OTHER",
        mappings=[FabricFieldMapping(collection="secret", object_type_id="ot-x")],
    ).insert()

    client = _make_client(_FakeUser(workspace_id="w1"), monkeypatch)
    assert client.get("/fabric/ingest/mappings").json()["mappings"] == []

    client.post("/fabric/ingest/mappings", json=_mapping_body())
    # w1's write landed in w1's config row, not w-OTHER's.
    other = await FabricIngestConfig.find_one(FabricIngestConfig.workspace == "w-OTHER")
    assert [m.collection for m in other.mappings] == ["secret"]
    mine = await FabricIngestConfig.find_one(FabricIngestConfig.workspace == "w1")
    assert [m.collection for m in mine.mappings] == ["gcalendar"]


# --------------------------------------------------------------------------
# 4 — RBAC: non-member of the active workspace 403s on every route
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/fabric/ingest/mappings", {}),
        ("post", "/fabric/ingest/mappings", {"json": _mapping_body()}),
        ("delete", "/fabric/ingest/mappings", {"params": {"collection": "gcalendar"}}),
        ("post", "/fabric/ingest/run", {"json": {"collection": "gcalendar"}}),
    ],
)
async def test_non_member_403s(mongo_db, monkeypatch, method, path, kwargs):  # noqa: ARG001
    client = _make_client(_FakeUser(member=False), monkeypatch)
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}: {resp.text}"


# --------------------------------------------------------------------------
# 5 — run-now
# --------------------------------------------------------------------------


class SpyIngestor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, store: Any, **kwargs: Any) -> IngestResult:  # noqa: ARG002
        self.calls.append(kwargs)
        return IngestResult(type_name="CalendarEvent", created=2)


async def test_run_now_dispatches_connector_mapping(mongo_db, monkeypatch, tmp_path):  # noqa: ARG001
    ws = "w1"
    await FabricIngestConfig(
        workspace=ws,
        mappings=[
            FabricFieldMapping(
                collection="gcalendar", object_type_id="ot-cal", source_kind="connector"
            )
        ],
    ).insert()
    await WorkspaceConnector(
        workspace=ws, name="gcalendar", enabled=True, scope="user", user_id="u42"
    ).insert()

    spy = SpyIngestor()
    monkeypatch.setitem(oss_fabric_ingest.FABRIC_INGESTORS, "gcalendar", spy)
    # Keep the default tenant store off the real home dir.
    from pocketpaw.fabric.store import FabricStore

    fs = FabricStore(tmp_path / "fabric_run_now.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda *a, workspace_id=None, **k: fs)

    client = _make_client(_FakeUser(workspace_id=ws), monkeypatch)
    resp = client.post("/fabric/ingest/run", json={"collection": "gcalendar"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["objects"] == 2
    assert body["workspace_id"] == ws
    assert spy.calls == [{"workspace_id": ws, "user_id": "u42"}]


async def test_run_now_unknown_collection_reports_error_body(mongo_db, monkeypatch):  # noqa: ARG001
    client = _make_client(_FakeUser(), monkeypatch)
    resp = client.post("/fabric/ingest/run", json={"collection": "never-configured"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["objects"] == 0
