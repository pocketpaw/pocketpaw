# Cluster F — API keys panel acceptance tests.
# Created: 2026-04-19 — walks the UI-facing flow end-to-end:
# create → list → rotate → verify old key no longer authenticates.
#
# These tests complement tests/test_api_keys.py (unit-level coverage) by
# pinning the shape the new /settings/api-keys panel depends on. They
# exist specifically because the panel copies the plaintext key to the
# clipboard on first show and expects rotate() to atomically revoke the
# old key — regressions here break the UI silently.

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.api_keys import APIKeyManager
from pocketpaw.api.v1.api_keys import router


@pytest.fixture
def storage_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def manager(storage_dir):
    return APIKeyManager(storage_path=storage_dir / "api_keys.json")


@pytest.fixture
def client(manager, monkeypatch):
    import pocketpaw.api.api_keys as mod

    monkeypatch.setattr(mod, "_manager", manager)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TestApiKeysPanelFlow:
    """End-to-end exercise of the flow the ApiKeysPanel.svelte panel walks."""

    def test_create_list_rotate_old_key_fails_verification(self, client, manager):
        """The full panel journey in one test.

        Mirrors the UI's lifecycle:
          1. User clicks "Create key" and sees the plaintext once.
          2. Panel refreshes the list — the new key appears (no plaintext).
          3. User clicks "Rotate" — the returned response has a new plaintext.
          4. Old plaintext can no longer authenticate.
        """
        # 1. Create
        create_resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "panel-smoke", "scopes": ["chat", "sessions"]},
        )
        assert create_resp.status_code == 200
        created = create_resp.json()
        old_plaintext = created["key"]
        key_id = created["id"]

        assert old_plaintext.startswith("pp_")
        assert created["prefix"] == old_plaintext[:12]
        # Shape contract: panel needs id + prefix + name for row identity.
        for field in ("id", "name", "prefix", "scopes", "created_at"):
            assert field in created

        # 2. List — no plaintext leak, our key is there.
        list_resp = client.get("/api/v1/auth/api-keys")
        assert list_resp.status_code == 200
        keys = list_resp.json()
        assert len(keys) == 1
        row = keys[0]
        assert row["id"] == key_id
        assert row["name"] == "panel-smoke"
        assert "key" not in row, "plaintext must never appear in list response"

        # 3. Rotate.
        rotate_resp = client.post(f"/api/v1/auth/api-keys/{key_id}/rotate")
        assert rotate_resp.status_code == 200
        rotated = rotate_resp.json()
        new_plaintext = rotated["key"]
        assert new_plaintext.startswith("pp_")
        assert new_plaintext != old_plaintext, "rotation must mint a fresh plaintext"
        # Scopes carry over — admin lever matches the old key.
        assert rotated["scopes"] == ["chat", "sessions"]

        # 4. Old key no longer authenticates via the manager, new one does.
        assert manager.verify(old_plaintext) is None, (
            "rotate() must revoke the old key — an attacker with the old "
            "plaintext must not authenticate"
        )
        fresh = manager.verify(new_plaintext)
        assert fresh is not None
        assert fresh.id == rotated["id"]

        # After rotation the list has two rows — old revoked, new active.
        list_after = client.get("/api/v1/auth/api-keys").json()
        assert len(list_after) == 2
        by_id = {r["id"]: r for r in list_after}
        assert by_id[key_id]["revoked"] is True
        assert by_id[rotated["id"]]["revoked"] is False

    def test_revoke_panel_flow(self, client, manager):
        """Revoke button on a row: DELETE should flip the key to revoked and
        invalidate verification."""
        created = client.post("/api/v1/auth/api-keys", json={"name": "revoke-me"}).json()
        plaintext = created["key"]
        key_id = created["id"]

        assert manager.verify(plaintext) is not None

        resp = client.delete(f"/api/v1/auth/api-keys/{key_id}")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert manager.verify(plaintext) is None

    def test_list_response_is_sorted_by_creation(self, client):
        """Panel renders rows in creation order — asserts the service doesn't
        shuffle them on a round-trip (some back-ends do, which makes the UI
        hard to reason about)."""
        for name in ("first", "second", "third"):
            client.post("/api/v1/auth/api-keys", json={"name": name})
        names = [row["name"] for row in client.get("/api/v1/auth/api-keys").json()]
        assert names == ["first", "second", "third"]
