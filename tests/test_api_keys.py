# Tests for API key system.
# Created: 2026-02-20

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.api_keys import APIKeyManager, APIKeyRecord
from pocketpaw.api.v1.api_keys import router


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def manager(tmp_path):
    return APIKeyManager(storage_path=tmp_path / "api_keys.json")


@pytest.fixture
def test_app(manager, monkeypatch):
    import pocketpaw.api.api_keys as mod

    monkeypatch.setattr(mod, "_manager", manager)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


# ===================== APIKeyManager unit tests =====================


class TestAPIKeyManager:
    """Tests for the APIKeyManager class."""

    def test_create_key(self, manager):
        record, plaintext = manager.create("test-key")
        assert plaintext.startswith("pp_")
        assert record.name == "test-key"
        assert record.prefix == plaintext[:12]
        assert not record.revoked
        assert record.scopes == ["chat", "sessions"]

    def test_create_key_custom_scopes(self, manager):
        record, _ = manager.create("admin-key", scopes=["admin", "chat"])
        assert "admin" in record.scopes
        assert "chat" in record.scopes

    def test_create_key_invalid_scopes(self, manager):
        with pytest.raises(ValueError, match="Invalid scopes"):
            manager.create("bad-key", scopes=["nonexistent"])

    def test_verify_valid_key(self, manager):
        record, plaintext = manager.create("verify-test")
        result = manager.verify(plaintext)
        assert result is not None
        assert result.id == record.id
        assert result.last_used_at is not None

    def test_verify_invalid_key(self, manager):
        result = manager.verify("pp_invalid_key_here_xxxxxxxxxxxxx")
        assert result is None

    def test_verify_non_pp_key(self, manager):
        result = manager.verify("not-a-pp-key")
        assert result is None

    def test_revoke_key(self, manager):
        record, plaintext = manager.create("revoke-test")
        assert manager.revoke(record.id) is True
        # Verify fails after revocation
        assert manager.verify(plaintext) is None

    def test_revoke_nonexistent(self, manager):
        assert manager.revoke("nonexistent-id") is False

    def test_revoke_already_revoked(self, manager):
        record, _ = manager.create("double-revoke")
        manager.revoke(record.id)
        assert manager.revoke(record.id) is False

    def test_rotate_key(self, manager):
        record, old_plaintext = manager.create("rotate-test")
        result = manager.rotate(record.id)
        assert result is not None
        new_record, new_plaintext = result
        # Old key revoked
        assert manager.verify(old_plaintext) is None
        # New key works
        assert manager.verify(new_plaintext) is not None
        assert new_record.name == "rotate-test"
        assert new_record.scopes == record.scopes

    def test_rotate_nonexistent(self, manager):
        assert manager.rotate("nonexistent") is None

    def test_list_keys(self, manager):
        manager.create("key-1")
        manager.create("key-2")
        keys = manager.list_keys()
        assert len(keys) == 2
        assert keys[0].name == "key-1"
        assert keys[1].name == "key-2"

    def test_list_keys_empty(self, manager):
        assert manager.list_keys() == []

    def test_get_key(self, manager):
        record, _ = manager.create("get-test")
        fetched = manager.get(record.id)
        assert fetched is not None
        assert fetched.name == "get-test"

    def test_get_nonexistent(self, manager):
        assert manager.get("nope") is None

    def test_expired_key_rejected(self, manager):
        record, plaintext = manager.create(
            "expired", expires_at="2020-01-01T00:00:00+00:00"
        )
        assert manager.verify(plaintext) is None

    def test_file_permissions(self, manager):
        import os

        manager.create("perm-test")
        mode = oct(os.stat(manager._path).st_mode & 0o777)
        assert mode == "0o600"


# ===================== API endpoint tests =====================


class TestAPIKeyEndpoints:
    """Tests for the API key REST endpoints."""

    def test_create_key_endpoint(self, client):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "my-key", "scopes": ["chat"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"].startswith("pp_")
        assert data["name"] == "my-key"
        assert data["scopes"] == ["chat"]

    def test_create_key_default_scopes(self, client):
        resp = client.post("/api/v1/auth/api-keys", json={"name": "default"})
        assert resp.status_code == 200
        assert resp.json()["scopes"] == ["chat", "sessions"]

    def test_create_key_invalid_scopes(self, client):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "bad", "scopes": ["invalid_scope"]},
        )
        assert resp.status_code == 400

    def test_list_keys_endpoint(self, client):
        client.post("/api/v1/auth/api-keys", json={"name": "k1"})
        client.post("/api/v1/auth/api-keys", json={"name": "k2"})
        resp = client.get("/api/v1/auth/api-keys")
        assert resp.status_code == 200
        keys = resp.json()
        assert len(keys) == 2
        # No plaintext keys in list response
        for k in keys:
            assert "key" not in k

    def test_revoke_key_endpoint(self, client):
        create_resp = client.post("/api/v1/auth/api-keys", json={"name": "revoke"})
        key_id = create_resp.json()["id"]
        resp = client.delete(f"/api/v1/auth/api-keys/{key_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_revoke_not_found(self, client):
        resp = client.delete("/api/v1/auth/api-keys/nonexistent")
        assert resp.status_code == 404

    def test_rotate_key_endpoint(self, client):
        create_resp = client.post("/api/v1/auth/api-keys", json={"name": "rotate"})
        old_key = create_resp.json()["key"]
        key_id = create_resp.json()["id"]

        resp = client.post(f"/api/v1/auth/api-keys/{key_id}/rotate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] != old_key
        assert data["key"].startswith("pp_")
        assert data["name"] == "rotate"

    def test_rotate_not_found(self, client):
        resp = client.post("/api/v1/auth/api-keys/nonexistent/rotate")
        assert resp.status_code == 404
