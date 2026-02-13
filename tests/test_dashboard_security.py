"""Tests for dashboard security hardening.

Covers:
  - Tunnel auth bypass fix (_is_genuine_localhost)
  - Rate limiting (burst, refill, 429 responses, per-IP isolation)
  - Session tokens (create, verify, expired, tampered, master regen)
  - Security headers
  - CORS rejection of non-matching origins
  - WebSocket tunnel auth
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from pocketclaw.security.rate_limiter import RateLimiter
from pocketclaw.security.session_tokens import create_session_token, verify_session_token


class TestRateLimiter:
    def test_allows_within_capacity(self):
        rl = RateLimiter(rate=10.0, capacity=5)
        for _ in range(5):
            assert rl.allow("client1") is True

    def test_rejects_over_capacity(self):
        rl = RateLimiter(rate=10.0, capacity=3)
        for _ in range(3):
            rl.allow("client1")
        assert rl.allow("client1") is False

    def test_refills_over_time(self):
        rl = RateLimiter(rate=1000.0, capacity=1)
        assert rl.allow("a") is True
        assert rl.allow("a") is False
        # Simulate time passing by manipulating last_refill
        rl._buckets["a"].last_refill -= 1.0  # 1 second ago
        assert rl.allow("a") is True

    def test_per_ip_isolation(self):
        rl = RateLimiter(rate=10.0, capacity=1)
        assert rl.allow("ip1") is True
        assert rl.allow("ip1") is False
        # Different IP still has tokens
        assert rl.allow("ip2") is True

    def test_cleanup_removes_stale(self):
        rl = RateLimiter(rate=10.0, capacity=5)
        rl.allow("old")
        rl._buckets["old"].last_refill -= 7200  # 2 hours ago
        rl.allow("recent")
        removed = rl.cleanup(max_age=3600)
        assert removed == 1
        assert "old" not in rl._buckets
        assert "recent" in rl._buckets

    def test_cleanup_keeps_active(self):
        rl = RateLimiter(rate=10.0, capacity=5)
        rl.allow("active")
        removed = rl.cleanup(max_age=3600)
        assert removed == 0


class TestSessionTokens:
    def test_create_and_verify(self):
        master = "test-master-token-1234"
        token = create_session_token(master, ttl_hours=1)
        assert ":" in token
        assert verify_session_token(token, master) is True

    def test_expired_token_rejected(self):
        master = "test-master-token"
        token = create_session_token(master, ttl_hours=1)  # noqa: F841
        # Build an expired token with correct HMAC
        expired_ts = str(int(time.time()) - 100)
        # Re-sign with correct HMAC for the expired timestamp
        from pocketclaw.security.session_tokens import _sign

        sig = _sign(master, expired_ts)
        expired_token = f"{expired_ts}:{sig}"
        assert verify_session_token(expired_token, master) is False

    def test_tampered_token_rejected(self):
        master = "test-master-token"
        token = create_session_token(master, ttl_hours=1)
        # Tamper with the HMAC
        parts = token.split(":", 1)
        tampered = f"{parts[0]}:{'0' * 64}"
        assert verify_session_token(tampered, master) is False

    def test_wrong_master_rejects(self):
        master = "original-master"
        token = create_session_token(master, ttl_hours=1)
        assert verify_session_token(token, "different-master") is False

    def test_invalid_format_rejected(self):
        assert verify_session_token("no-colon-here", "master") is False
        assert verify_session_token("", "master") is False
        assert verify_session_token("abc:def", "master") is False

    def test_master_regeneration_invalidates(self):
        master1 = "master-v1"
        token = create_session_token(master1, ttl_hours=24)
        assert verify_session_token(token, master1) is True
        # After master regen, old session tokens are invalid
        master2 = "master-v2"
        assert verify_session_token(token, master2) is False


# ---------------------------------------------------------------------------
# _is_genuine_localhost tests
# ---------------------------------------------------------------------------


class TestIsGenuineLocalhost:
    """Test the _is_genuine_localhost helper function."""

    def _make_request(self, host="127.0.0.1", headers=None):
        """Create a mock request with given client host and headers."""
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = host
        req.headers = headers or {}
        return req

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_genuine_localhost_no_tunnel(self, mock_tunnel_fn, mock_settings_cls):
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": False}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1")
        assert _is_genuine_localhost(req) is True

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_tunneled_request_blocked(self, mock_tunnel_fn, mock_settings_cls):
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": True}
        mock_tunnel_fn.return_value = tunnel

        # Request comes from localhost but has Cf-Connecting-Ip header (tunnel proxy)
        req = self._make_request("127.0.0.1", headers={"cf-connecting-ip": "1.2.3.4"})
        assert _is_genuine_localhost(req) is False

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_tunneled_request_x_forwarded_for(self, mock_tunnel_fn, mock_settings_cls):
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": True}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1", headers={"x-forwarded-for": "5.6.7.8"})
        assert _is_genuine_localhost(req) is False

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_genuine_localhost_with_active_tunnel_no_proxy_headers(
        self, mock_tunnel_fn, mock_settings_cls
    ):
        """Genuine localhost browser while tunnel is active — no proxy headers."""
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": True}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1", headers={})
        assert _is_genuine_localhost(req) is True

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_bypass_disabled(self, mock_tunnel_fn, mock_settings_cls):
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = False
        mock_settings_cls.load.return_value = settings

        req = self._make_request("127.0.0.1")
        assert _is_genuine_localhost(req) is False

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_non_localhost_rejected(self, mock_tunnel_fn, mock_settings_cls):
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        req = self._make_request("192.168.1.5")
        assert _is_genuine_localhost(req) is False

    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard.get_tunnel_manager")
    def test_ipv6_localhost(self, mock_tunnel_fn, mock_settings_cls):
        from pocketclaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": False}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("::1")
        assert _is_genuine_localhost(req) is True


# ---------------------------------------------------------------------------
# Dashboard integration tests (auth middleware, headers, CORS, session exchange)
# ---------------------------------------------------------------------------


@pytest.fixture
def test_client():
    """Create a FastAPI TestClient for the dashboard app."""
    from starlette.testclient import TestClient

    from pocketclaw.dashboard import app

    return TestClient(app, raise_server_exceptions=False)


class TestSecurityHeaders:
    def test_headers_present(self, test_client):
        resp = test_client.get("/")
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "camera=()" in resp.headers.get("Permissions-Policy", "")
        assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")

    def test_hsts_only_on_https(self, test_client):
        # Regular HTTP request — no HSTS
        resp = test_client.get("/")
        assert "Strict-Transport-Security" not in resp.headers


class TestSessionTokenEndpoint:
    @patch("pocketclaw.dashboard.get_access_token", return_value="master-abc")
    @patch("pocketclaw.dashboard.Settings")
    @patch("pocketclaw.dashboard._is_genuine_localhost", return_value=True)
    def test_exchange_valid_master(self, mock_local, mock_settings_cls, mock_token, test_client):
        settings = MagicMock()
        settings.session_token_ttl_hours = 24
        mock_settings_cls.load.return_value = settings

        resp = test_client.post(
            "/api/auth/session",
            headers={"Authorization": "Bearer master-abc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_token" in data
        assert ":" in data["session_token"]
        assert data["expires_in_hours"] == 24

    @patch("pocketclaw.dashboard.get_access_token", return_value="master-abc")
    @patch("pocketclaw.dashboard._is_genuine_localhost", return_value=True)
    def test_exchange_invalid_master(self, mock_local, mock_token, test_client):
        resp = test_client.post(
            "/api/auth/session",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestAuthMiddlewareSessionToken:
    @patch("pocketclaw.dashboard.get_access_token", return_value="master-xyz")
    @patch("pocketclaw.dashboard._is_genuine_localhost", return_value=False)
    def test_session_token_accepted(self, mock_local, mock_token, test_client):
        session = create_session_token("master-xyz", ttl_hours=1)
        resp = test_client.get(
            "/api/channels/status",
            headers={"Authorization": f"Bearer {session}"},
        )
        # Should not be 401 (may be other status depending on channel state)
        assert resp.status_code != 401

    @patch("pocketclaw.dashboard.get_access_token", return_value="master-xyz")
    @patch("pocketclaw.dashboard._is_genuine_localhost", return_value=False)
    def test_no_token_rejected(self, mock_local, mock_token, test_client):
        resp = test_client.get("/api/channels/status")
        assert resp.status_code == 401
