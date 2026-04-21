# test_api_v1_connector_status.py — Cluster C / PR2 — connector status route.
# Created: 2026-04-19 — Locks the status transition contract
# (disconnected -> connecting -> connected -> expired) and pocket-scoped
# cred isolation. See docs/plans/cluster-C-reality.md, gap C5.

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pocketpaw.api.v1.connectors as connectors_module
from pocketpaw.api.deps import require_scope
from pocketpaw.connectors.protocol import ConnectionResult, ConnectorStatus


class _FakeDefn:
    def __init__(self, name: str) -> None:
        self.name = name
        self.display_name = name.replace("_", " ").title()
        self.type = "rest"
        self.icon = "plug"
        self.actions: list[dict] = []
        self.auth = {"credentials": []}


class _FakeRegistry:
    """In-memory fake for ConnectorRegistry used by the route tests.

    Sticks to the subset of the real surface the connector router touches:
    get_definition, get_adapter, connect, disconnect, status, available,
    _definitions.
    """

    def __init__(self) -> None:
        self._definitions = {"google_drive": _FakeDefn("google_drive")}
        self._instances: dict[str, MagicMock] = {}
        self.available = [
            {
                "name": "google_drive",
                "display_name": "Google Drive",
                "type": "rest",
                "icon": "drive",
            }
        ]

    def get_definition(self, name: str):
        return self._definitions.get(name)

    def get_adapter(self, pocket_id: str, connector_name: str):
        return self._instances.get(f"{pocket_id}:{connector_name}")

    async def connect(self, pocket_id: str, connector_name: str, config: dict):
        if connector_name not in self._definitions:
            return None
        adapter = MagicMock()
        adapter.execute = MagicMock()
        self._instances[f"{pocket_id}:{connector_name}"] = adapter
        return ConnectionResult(
            success=True,
            connector_name=connector_name,
            status=ConnectorStatus.CONNECTED,
            message="connected",
        )

    async def disconnect(self, pocket_id: str, connector_name: str) -> bool:
        key = f"{pocket_id}:{connector_name}"
        if key in self._instances:
            del self._instances[key]
            return True
        return False

    def status(self, pocket_id: str):
        return [
            {
                "name": n,
                "display_name": d.display_name,
                "icon": d.icon,
                "status": ConnectorStatus.CONNECTED
                if f"{pocket_id}:{n}" in self._instances
                else ConnectorStatus.DISCONNECTED,
            }
            for n, d in self._definitions.items()
        ]


@pytest.fixture(autouse=True)
def _reset_extras():
    connectors_module._STATUS_EXTRAS.clear()
    yield
    connectors_module._STATUS_EXTRAS.clear()


@pytest.fixture
def fake_registry(monkeypatch):
    fake = _FakeRegistry()
    monkeypatch.setattr(connectors_module, "_registry", fake)
    return fake


@pytest.fixture
def client(fake_registry) -> TestClient:
    app = FastAPI()
    # Disable scope check for the test app so we can hit the routes without
    # a valid token. The real deployment still enforces require_scope.
    app.dependency_overrides[require_scope("connectors")] = lambda: None
    app.include_router(connectors_module.router, prefix="/api/v1")
    return TestClient(app)


class TestConnectorStatusRoute:
    def test_unknown_connector_is_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/connectors/slack/status")
        assert resp.status_code == 404

    def test_disconnected_default(self, client: TestClient) -> None:
        resp = client.get("/api/v1/connectors/google_drive/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "name": "google_drive",
            "pocket_id": "default",
            "connected": False,
            "last_sync": None,
            "cred_state": "missing",
            "scope": "",
        }

    def test_connected_transitions_cred_state(self, client: TestClient) -> None:
        # Connect first
        connect_resp = client.post(
            "/api/v1/connectors/connect",
            json={
                "connector_name": "google_drive",
                "pocket_id": "p1",
                "config": {"scope": "drive.readonly"},
            },
        )
        assert connect_resp.status_code == 200

        status = client.get(
            "/api/v1/connectors/google_drive/status?pocket_id=p1"
        ).json()
        assert status["connected"] is True
        assert status["cred_state"] == "valid"
        assert status["scope"] == "drive.readonly"
        assert status["last_sync"] is not None
        # ISO timestamp shape.
        assert "T" in status["last_sync"]

    def test_disconnect_returns_to_missing(self, client: TestClient) -> None:
        client.post(
            "/api/v1/connectors/connect",
            json={
                "connector_name": "google_drive",
                "pocket_id": "p1",
                "config": {"scope": "drive.readonly"},
            },
        )
        client.post(
            "/api/v1/connectors/disconnect",
            json={"connector_name": "google_drive", "pocket_id": "p1"},
        )
        status = client.get(
            "/api/v1/connectors/google_drive/status?pocket_id=p1"
        ).json()
        assert status["connected"] is False
        assert status["cred_state"] == "missing"
        assert status["scope"] == ""

    def test_status_is_pocket_scoped(self, client: TestClient) -> None:
        """A pocket with no connection must NOT inherit another pocket's state."""
        client.post(
            "/api/v1/connectors/connect",
            json={
                "connector_name": "google_drive",
                "pocket_id": "alpha",
                "config": {"scope": "drive.readonly"},
            },
        )

        alpha = client.get(
            "/api/v1/connectors/google_drive/status?pocket_id=alpha"
        ).json()
        beta = client.get(
            "/api/v1/connectors/google_drive/status?pocket_id=beta"
        ).json()

        assert alpha["connected"] is True
        assert beta["connected"] is False
        assert beta["cred_state"] == "missing"
        assert beta["scope"] == ""

    def test_expired_state_is_surfaced(self, client: TestClient) -> None:
        """The helper accepts an 'expired' cred_state override so the OAuth
        refresh path can flip the UI badge without reconnecting."""
        connectors_module.record_connector_event(
            pocket_id="p1",
            connector_name="google_drive",
            cred_state="expired",
        )
        status = client.get(
            "/api/v1/connectors/google_drive/status?pocket_id=p1"
        ).json()
        # adapter isn't instantiated so connected stays false, but cred_state
        # reflects the OAuth refresh result.
        assert status["connected"] is False
        assert status["cred_state"] == "expired"

    def test_config_secrets_never_leak_into_status(self, client: TestClient) -> None:
        """Secrets from the connect payload must NOT be retrievable via /status.

        This is the core pocket-scope + no-leak contract from the security
        review focus in the plan.
        """
        client.post(
            "/api/v1/connectors/connect",
            json={
                "connector_name": "google_drive",
                "pocket_id": "p1",
                "config": {
                    "scope": "drive.readonly",
                    "client_secret": "super-secret-xyz",
                    "refresh_token": "rt-abcdef-sensitive",
                },
            },
        )
        status = client.get(
            "/api/v1/connectors/google_drive/status?pocket_id=p1"
        ).json()
        assert "client_secret" not in status
        assert "refresh_token" not in status
        # Payload is small and flat — the sensitive strings must not appear
        # anywhere in the serialised response either.
        body = repr(status)
        assert "super-secret-xyz" not in body
        assert "rt-abcdef-sensitive" not in body
