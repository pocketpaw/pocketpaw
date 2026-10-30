# Tests for integrations/oauth.py and integrations/token_store.py
# Created: 2026-02-07
# 2026-06-08: added TestTokenStorePerUser + TestOAuthManagerPerUser to lock
#   per-(service, user_id) token isolation and the user_id=None back-compat
#   default (foundation for VIP Onboarding Phase B).

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
# TokenStore — per-user isolation (VIP Onboarding Phase B foundation)
# ---------------------------------------------------------------------------


class TestTokenStorePerUser:
    """Tokens are keyed by (service, user_id), not service alone.

    Back-compat invariant: user_id=None writes/reads the legacy
    ``{service}.json`` path so existing single-user installs keep working.
    """

    def test_two_users_same_service_do_not_collide(self, store):
        """Alice and Bob both connect Gmail — their tokens stay separate."""
        store.save(OAuthTokens(service="google_gmail", access_token="alice_tok"), user_id="alice")
        store.save(OAuthTokens(service="google_gmail", access_token="bob_tok"), user_id="bob")

        alice = store.load("google_gmail", user_id="alice")
        bob = store.load("google_gmail", user_id="bob")
        assert alice is not None and alice.access_token == "alice_tok"
        assert bob is not None and bob.access_token == "bob_tok"

    def test_user_scoped_token_invisible_to_default_bucket(self, store):
        """A per-user token must not leak into the service-only (None) bucket."""
        store.save(OAuthTokens(service="google_gmail", access_token="alice_tok"), user_id="alice")
        assert store.load("google_gmail") is None
        assert store.load("google_gmail", user_id="bob") is None

    def test_default_bucket_is_legacy_path(self, store, tmp_path):
        """user_id=None writes the exact legacy ``{service}.json`` file."""
        store.save(OAuthTokens(service="google_gmail", access_token="legacy"))
        assert (tmp_path / "google_gmail.json").exists()
        loaded = store.load("google_gmail")
        assert loaded is not None and loaded.access_token == "legacy"

    def test_legacy_file_still_loads_after_rekey(self, store, tmp_path):
        """A token file written the OLD way (no user_id) still loads.

        Simulates an existing install: the file predates the per-user key,
        so it has no ``user_id`` field in its JSON. ``user_id=None`` load
        must find it.
        """
        import json

        legacy = {
            "service": "google_calendar",
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "token_type": "Bearer",
            "expires_at": None,
            "scopes": [],
            "extra": {},
        }
        (tmp_path / "google_calendar.json").write_text(json.dumps(legacy))

        loaded = store.load("google_calendar")
        assert loaded is not None
        assert loaded.access_token == "old_access"
        assert loaded.refresh_token == "old_refresh"

    def test_delete_is_user_scoped(self, store):
        """Deleting alice's token leaves bob's and the default bucket intact."""
        store.save(OAuthTokens(service="google_gmail", access_token="default"))
        store.save(OAuthTokens(service="google_gmail", access_token="alice"), user_id="alice")
        store.save(OAuthTokens(service="google_gmail", access_token="bob"), user_id="bob")

        assert store.delete("google_gmail", user_id="alice") is True
        assert store.load("google_gmail", user_id="alice") is None
        # Bob and the default bucket untouched.
        assert store.load("google_gmail", user_id="bob").access_token == "bob"
        assert store.load("google_gmail").access_token == "default"

    def test_email_like_user_id_is_filesystem_safe(self, store):
        """A user_id that looks like an email (or has path chars) is sanitized.

        It must not escape the oauth dir and must round-trip cleanly.
        """
        store.save(
            OAuthTokens(service="google_gmail", access_token="email_tok"),
            user_id="prakash@snctm.com",
        )
        loaded = store.load("google_gmail", user_id="prakash@snctm.com")
        assert loaded is not None and loaded.access_token == "email_tok"
        # Distinct user_ids that sanitize to the same disk name must not
        # be possible to confuse — a different email is a different bucket.
        assert store.load("google_gmail", user_id="other@snctm.com") is None

    def test_user_id_with_path_traversal_is_contained(self, store, tmp_path):
        """A malicious user_id can't write outside the oauth dir."""
        store.save(
            OAuthTokens(service="google_gmail", access_token="evil"),
            user_id="../../etc/passwd",
        )
        # Nothing was written above tmp_path.
        assert not (tmp_path.parent / "etc").exists()
        # But it still round-trips within the store.
        loaded = store.load("google_gmail", user_id="../../etc/passwd")
        assert loaded is not None and loaded.access_token == "evil"

    def test_list_services_dedupes_across_users(self, store):
        """list_services reports each service once even with multiple users."""
        store.save(OAuthTokens(service="google_gmail", access_token="a"), user_id="alice")
        store.save(OAuthTokens(service="google_gmail", access_token="b"), user_id="bob")
        store.save(OAuthTokens(service="google_calendar", access_token="c"))

        services = store.list_services()
        assert sorted(services) == ["google_calendar", "google_gmail"]


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
# OAuthManager — per-user threading (VIP Onboarding Phase B foundation)
# ---------------------------------------------------------------------------


async def test_get_valid_token_is_user_scoped(store):
    """get_valid_token(user_id=...) only sees that user's fresh token."""
    store.save(
        OAuthTokens(
            service="google_gmail",
            access_token="alice_fresh",
            expires_at=time.time() + 3600,
        ),
        user_id="alice",
    )
    manager = OAuthManager(store)

    alice = await manager.get_valid_token(
        service="google_gmail", client_id="id", client_secret="secret", user_id="alice"
    )
    assert alice == "alice_fresh"
    # Bob has nothing; the default bucket has nothing.
    assert (
        await manager.get_valid_token(
            service="google_gmail", client_id="id", client_secret="secret", user_id="bob"
        )
        is None
    )
    assert (
        await manager.get_valid_token(
            service="google_gmail", client_id="id", client_secret="secret"
        )
        is None
    )


async def test_exchange_code_persists_under_user_id(store, monkeypatch):
    """exchange_code(user_id=...) stamps + stores the token in that user's bucket."""
    from unittest.mock import MagicMock, patch

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "access_token": "alice_access",
                    "refresh_token": "alice_refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            )
            return resp

    with patch("pocketpaw.clients.oauth.httpx.AsyncClient", _FakeClient):
        manager = OAuthManager(store)
        tokens = await manager.exchange_code(
            provider="google",
            service="google_gmail",
            code="auth_code",
            client_id="cid",
            client_secret="csec",
            redirect_uri="http://localhost/cb",
            scopes=["https://mail.google.com/"],
            user_id="alice",
        )

    assert tokens.access_token == "alice_access"
    # Landed in alice's bucket, NOT the default one.
    assert store.load("google_gmail", user_id="alice").access_token == "alice_access"
    assert store.load("google_gmail") is None


async def test_refresh_token_is_user_scoped(store):
    """refresh_token(user_id=...) refreshes + re-saves that user's row only."""
    from unittest.mock import MagicMock, patch

    store.save(
        OAuthTokens(
            service="google_gmail",
            access_token="stale",
            refresh_token="alice_refresh",
            expires_at=time.time() - 60,
        ),
        user_id="alice",
    )

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"access_token": "alice_new", "expires_in": 3600})
            return resp

    with patch("pocketpaw.clients.oauth.httpx.AsyncClient", _FakeClient):
        manager = OAuthManager(store)
        refreshed = await manager.refresh_token(
            provider="google",
            service="google_gmail",
            client_id="cid",
            client_secret="csec",
            user_id="alice",
        )

    assert refreshed is not None and refreshed.access_token == "alice_new"
    # Re-saved into alice's bucket.
    assert store.load("google_gmail", user_id="alice").access_token == "alice_new"


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
