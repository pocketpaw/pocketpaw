# Tests for integrations/oauth.py and integrations/token_store.py
# Created: 2026-02-07

import stat
import time

import pytest

from pocketclaw.integrations.oauth import PROVIDERS, OAuthManager
from pocketclaw.integrations.token_store import OAuthTokens, TokenStore

# ---------------------------------------------------------------------------
# TokenStore
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketclaw.integrations.token_store._get_oauth_dir", lambda: tmp_path)
    return TokenStore()


class TestTokenStore:
    def test_save_and_load(self, store, tmp_path):
        tokens = OAuthTokens(
            service="test_service",
            access_token="access123",
            refresh_token="refresh456",
            expires_at=time.time() + 3600,
            scopes=["email", "profile"],
        )
        store.save(tokens)

        loaded = store.load("test_service")
        assert loaded is not None
        assert loaded.access_token == "access123"
        assert loaded.refresh_token == "refresh456"
        assert loaded.scopes == ["email", "profile"]

    def test_load_nonexistent(self, store):
        assert store.load("nope") is None

    def test_delete(self, store):
        tokens = OAuthTokens(service="to_delete", access_token="x")
        store.save(tokens)
        assert store.delete("to_delete") is True
        assert store.load("to_delete") is None

    def test_delete_nonexistent(self, store):
        assert store.delete("nope") is False

    def test_list_services(self, store):
        store.save(OAuthTokens(service="svc1", access_token="a"))
        store.save(OAuthTokens(service="svc2", access_token="b"))
        services = store.list_services()
        assert "svc1" in services
        assert "svc2" in services

    def test_file_permissions(self, store, tmp_path):
        tokens = OAuthTokens(service="perms_test", access_token="secret")
        store.save(tokens)
        path = tmp_path / "perms_test.json"
        mode = path.stat().st_mode
        # Owner read+write only
        assert mode & stat.S_IRUSR
        assert mode & stat.S_IWUSR
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)


# ---------------------------------------------------------------------------
# OAuthManager
# ---------------------------------------------------------------------------


class TestOAuthManager:
    def test_get_auth_url(self):
        manager = OAuthManager()
        url = manager.get_auth_url(
            provider="google",
            client_id="test-client-id",
            redirect_uri="http://localhost:8888/oauth/callback",
            scopes=["email", "profile"],
            state="google:test_service",
        )
        assert "accounts.google.com" in url
        assert "test-client-id" in url
        assert "email" in url
        assert "state=google" in url

    def test_get_auth_url_unknown_provider(self):
        manager = OAuthManager()
        with pytest.raises(ValueError, match="Unknown OAuth provider"):
            manager.get_auth_url(
                provider="unknown",
                client_id="x",
                redirect_uri="http://localhost",
                scopes=[],
            )

    def test_providers_config(self):
        assert "google" in PROVIDERS
        assert "auth_url" in PROVIDERS["google"]
        assert "token_url" in PROVIDERS["google"]


class TestOAuthTokens:
    def test_dataclass_fields(self):
        t = OAuthTokens(
            service="test",
            access_token="a",
            refresh_token="r",
            token_type="Bearer",
            expires_at=1234567890.0,
            scopes=["email"],
        )
        assert t.service == "test"
        assert t.access_token == "a"
        assert t.refresh_token == "r"
        assert t.scopes == ["email"]

    def test_defaults(self):
        t = OAuthTokens(service="test", access_token="a")
        assert t.refresh_token is None
        assert t.token_type == "Bearer"
        assert t.scopes == []


# ---------------------------------------------------------------------------
# get_valid_token — unit test with mocked store
# ---------------------------------------------------------------------------


async def test_get_valid_token_fresh(store):
    """Should return access token if not expired."""
    tokens = OAuthTokens(
        service="fresh_svc",
        access_token="fresh_token",
        refresh_token="refresh",
        expires_at=time.time() + 3600,
    )
    store.save(tokens)

    manager = OAuthManager(store)
    token = await manager.get_valid_token(
        service="fresh_svc",
        client_id="id",
        client_secret="secret",
    )
    assert token == "fresh_token"


async def test_get_valid_token_not_found(store):
    """Should return None if no tokens stored."""
    manager = OAuthManager(store)
    token = await manager.get_valid_token(
        service="missing",
        client_id="id",
        client_secret="secret",
    )
    assert token is None
