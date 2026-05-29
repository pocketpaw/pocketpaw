# Tests for integrations/oauth.py and integrations/token_store.py
# Created: 2026-02-07

import stat
import sys
import time

import pytest

from pocketpaw.clients.oauth import PROVIDERS, OAuthManager
from pocketpaw.clients.token_store import OAuthTokens, TokenStore

# ---------------------------------------------------------------------------
# TokenStore
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketpaw.clients.token_store._get_oauth_dir", lambda: tmp_path)
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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Unix file permissions not available on Windows",
    )
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

    def test_meetings_providers_registered(self):
        for name in ("zoom", "google_meet"):
            assert name in PROVIDERS, f"{name} missing from PROVIDERS"
            assert "token_url" in PROVIDERS[name]
        assert PROVIDERS["zoom"]["grant_type"] == "account_credentials"
        assert PROVIDERS["google_meet"]["token_url"] == PROVIDERS["google"]["token_url"]


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


# ---------------------------------------------------------------------------
# Zoom S2S OAuth (account_credentials grant)
# ---------------------------------------------------------------------------


async def test_exchange_account_credentials(store, monkeypatch):
    """Zoom S2S exchange should POST with Basic auth + account_id and persist tokens."""
    from unittest.mock import MagicMock, patch

    captured = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, data=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "access_token": "zoom_access_xyz",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "scope": "meeting:write recording:read",
                }
            )
            return resp

    with patch("pocketpaw.clients.oauth.httpx.AsyncClient", _FakeClient):
        manager = OAuthManager(store)
        tokens = await manager.exchange_account_credentials(
            provider="zoom",
            service="ws-1-zoom",
            client_id="cid",
            client_secret="csec",
            account_id="acct-abc",
        )

    assert tokens.access_token == "zoom_access_xyz"
    assert tokens.refresh_token is None  # S2S never has refresh_token
    assert tokens.extra["account_id"] == "acct-abc"
    # Phase 1.5: client_id/client_secret persist in the token blob too,
    # so the meetings adapter factory can reconstruct ZoomClient.
    assert tokens.extra["client_id"] == "cid"
    assert tokens.extra["client_secret"] == "csec"
    assert "meeting:write" in tokens.scopes
    assert captured["url"] == PROVIDERS["zoom"]["token_url"]
    assert captured["headers"]["Authorization"].startswith("Basic ")
    assert captured["data"] == {"grant_type": "account_credentials", "account_id": "acct-abc"}

    # Round-trip through store
    loaded = store.load("ws-1-zoom")
    assert loaded is not None
    assert loaded.extra["account_id"] == "acct-abc"


async def test_refresh_token_zoom_s2s_uses_account_credentials(store):
    """refresh_token() on a Zoom service should re-request via account_credentials."""
    from unittest.mock import MagicMock, patch

    # Seed an expired S2S token
    store.save(
        OAuthTokens(
            service="ws-2-zoom",
            access_token="old",
            refresh_token=None,
            expires_at=time.time() - 60,
            extra={"account_id": "acct-zzz"},
        )
    )

    grant_seen = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, data=None):
            grant_seen.append(data.get("grant_type"))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "access_token": "new_zoom_tok",
                    "token_type": "bearer",
                    "expires_in": 3600,
                }
            )
            return resp

    with patch("pocketpaw.clients.oauth.httpx.AsyncClient", _FakeClient):
        manager = OAuthManager(store)
        refreshed = await manager.refresh_token(
            provider="zoom",
            service="ws-2-zoom",
            client_id="cid",
            client_secret="csec",
        )

    assert refreshed is not None
    assert refreshed.access_token == "new_zoom_tok"
    # Critical: must route through account_credentials, NOT refresh_token grant
    assert grant_seen == ["account_credentials"]


async def test_refresh_token_zoom_s2s_missing_account_id(store):
    """If a Zoom token row has no account_id in extra, refresh fails gracefully."""
    store.save(
        OAuthTokens(
            service="ws-3-zoom",
            access_token="x",
            extra={},  # missing account_id
        )
    )
    manager = OAuthManager(store)
    result = await manager.refresh_token(
        provider="zoom",
        service="ws-3-zoom",
        client_id="cid",
        client_secret="csec",
    )
    assert result is None
