# test_api_v1_connector_lifecycle.py — connector-store-unification CS-6 —
# the OSS router survives restarts.
# Created: 2026-06-12 — Locks the restart contract at the HTTP layer with a
#   REAL ConnectorRegistry on tmp dirs (no fakes): connect → fresh registry
#   instance (simulated process restart, _STATUS_EXTRAS cleared) → list,
#   detail, and status endpoints all report "connected"; /execute reconnects
#   from persisted config via ensure_connected; /disconnect works after the
#   restart; orphaned state rows surface as definition_missing in the list.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pocketpaw.api.v1.connectors as connectors_module
import pocketpaw.connectors.registry as registry_module
from pocketpaw.api.deps import require_scope
from pocketpaw.connectors.protocol import ActionResult, ConnectionResult, ConnectorStatus
from pocketpaw.connectors.registry import ConnectorRegistry
from pocketpaw.connectors.state_store import FileConnectorStateStore

_TEST_YAML = """\
name: testsvc
display_name: Test Service
type: rest
icon: plug
auth:
  type: api_key
  credentials:
    - name: api_key
      required: true
actions:
  - name: ping
    description: Ping the service
    method: GET
    url: https://example.invalid/ping
"""


class FakeAdapter:
    """Minimal adapter so /execute works without real HTTP."""

    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "testsvc"

    @property
    def display_name(self) -> str:
        return "Test Service"

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        self.connect_calls.append((pocket_id, config))
        return ConnectionResult(
            success=True,
            connector_name=self.name,
            status=ConnectorStatus.CONNECTED,
            message="connected",
        )

    async def disconnect(self, pocket_id: str) -> bool:
        return True

    async def actions(self) -> list:
        return []

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        return ActionResult(success=True, data={"action": action})


@pytest.fixture
def dirs(tmp_path) -> dict[str, Path]:
    defs = tmp_path / "defs"
    defs.mkdir()
    (defs / "testsvc.yaml").write_text(_TEST_YAML)
    return {
        "defs": defs,
        "home": tmp_path / "home-defs",
        "state": tmp_path / "state",
    }


@pytest.fixture
def fake_adapters(monkeypatch) -> list[FakeAdapter]:
    created: list[FakeAdapter] = []

    def _fake_create(connector_name: str):
        if connector_name != "testsvc":
            return None
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    monkeypatch.setattr(registry_module, "_create_native_adapter", _fake_create)
    return created


def _build_registry(dirs: dict[str, Path]) -> ConnectorRegistry:
    return ConnectorRegistry(
        dirs["defs"],
        state_store=FileConnectorStateStore(base_dir=dirs["state"]),
        home_connectors_dir=dirs["home"],
    )


@pytest.fixture
def client(dirs, monkeypatch) -> TestClient:
    monkeypatch.setattr(connectors_module, "_registry", _build_registry(dirs))
    connectors_module._STATUS_EXTRAS.clear()
    app = FastAPI()
    app.dependency_overrides[require_scope("connectors")] = lambda: None
    app.include_router(connectors_module.router, prefix="/api/v1")
    yield TestClient(app)
    connectors_module._STATUS_EXTRAS.clear()


def _simulate_restart(dirs: dict[str, Path], monkeypatch) -> None:
    """Fresh registry instance + cleared in-memory side-table = new process."""
    monkeypatch.setattr(connectors_module, "_registry", _build_registry(dirs))
    connectors_module._STATUS_EXTRAS.clear()


def _connect(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/connectors/connect",
        json={
            "connector_name": "testsvc",
            "pocket_id": "default",
            "config": {"api_key": "k1"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


class TestRestartTruth:
    def test_list_shows_connected_after_restart(
        self, client, dirs, monkeypatch, fake_adapters
    ) -> None:
        _connect(client)
        _simulate_restart(dirs, monkeypatch)

        listed = client.get("/api/v1/connectors").json()
        testsvc = next(c for c in listed if c["name"] == "testsvc")
        assert testsvc["status"] == "connected"

    def test_status_shows_connected_after_restart(
        self, client, dirs, monkeypatch, fake_adapters
    ) -> None:
        _connect(client)
        _simulate_restart(dirs, monkeypatch)

        status = client.get("/api/v1/connectors/testsvc/status").json()
        assert status["connected"] is True
        # Extras died with the process; cred_state falls back from connected.
        assert status["cred_state"] == "valid"

    def test_detail_shows_connected_after_restart(
        self, client, dirs, monkeypatch, fake_adapters
    ) -> None:
        _connect(client)
        _simulate_restart(dirs, monkeypatch)

        detail = client.get("/api/v1/connectors/testsvc").json()
        assert detail["status"] == "connected"

    def test_execute_reconnects_after_restart(
        self, client, dirs, monkeypatch, fake_adapters
    ) -> None:
        _connect(client)
        _simulate_restart(dirs, monkeypatch)

        resp = client.post(
            "/api/v1/connectors/execute",
            json={"connector_name": "testsvc", "action": "ping", "pocket_id": "default"},
        ).json()
        assert resp["success"] is True
        assert resp["data"] == {"action": "ping"}
        # The post-restart adapter reconnected from the PERSISTED config.
        assert fake_adapters[-1].connect_calls == [("default", {"api_key": "k1"})]

    def test_disconnect_works_after_restart(self, client, dirs, monkeypatch, fake_adapters) -> None:
        _connect(client)
        _simulate_restart(dirs, monkeypatch)

        resp = client.post(
            "/api/v1/connectors/disconnect",
            json={"connector_name": "testsvc", "pocket_id": "default"},
        ).json()
        assert resp["success"] is True

        status = client.get("/api/v1/connectors/testsvc/status").json()
        assert status["connected"] is False


class TestNeverConfigured:
    def test_execute_without_config_still_errors(self, client, fake_adapters) -> None:
        resp = client.post(
            "/api/v1/connectors/execute",
            json={"connector_name": "testsvc", "action": "ping", "pocket_id": "default"},
        ).json()
        assert resp["success"] is False
        assert "not connected" in resp["error"]

    def test_status_disconnected_without_config(self, client) -> None:
        status = client.get("/api/v1/connectors/testsvc/status").json()
        assert status["connected"] is False
        assert status["cred_state"] == "missing"


class TestOrphanRows:
    def test_orphan_row_listed_as_definition_missing(self, client, dirs) -> None:
        FileConnectorStateStore(base_dir=dirs["state"]).set("ghost", "default", {"k": "v"})

        listed = client.get("/api/v1/connectors").json()
        ghost = next(c for c in listed if c["name"] == "ghost")
        assert ghost["status"] == "definition_missing"
        assert ghost["type"] == "unknown"
        # Defined connectors are unaffected.
        testsvc = next(c for c in listed if c["name"] == "testsvc")
        assert testsvc["status"] == "disconnected"
