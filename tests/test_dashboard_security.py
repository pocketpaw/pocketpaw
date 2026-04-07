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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.security.rate_limiter import RateLimiter
from pocketpaw.security.session_tokens import create_session_token, verify_session_token


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
        from pocketpaw.security.session_tokens import _sign

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

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_genuine_localhost_no_tunnel(self, mock_tunnel_fn, mock_settings_cls):
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": False}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1")
        assert _is_genuine_localhost(req) is True

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_tunneled_request_blocked(self, mock_tunnel_fn, mock_settings_cls):
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": True}
        mock_tunnel_fn.return_value = tunnel

        # Request comes from localhost but has Cf-Connecting-Ip header (tunnel proxy)
        req = self._make_request("127.0.0.1", headers={"cf-connecting-ip": "1.2.3.4"})
        assert _is_genuine_localhost(req) is False

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_tunneled_request_x_forwarded_for(self, mock_tunnel_fn, mock_settings_cls):
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": True}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1", headers={"x-forwarded-for": "5.6.7.8"})
        assert _is_genuine_localhost(req) is False

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_genuine_localhost_with_active_tunnel_no_proxy_headers(
        self, mock_tunnel_fn, mock_settings_cls
    ):
        """Genuine localhost browser while tunnel is active — no proxy headers."""
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": True}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1", headers={})
        assert _is_genuine_localhost(req) is True

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_bypass_disabled(self, mock_tunnel_fn, mock_settings_cls):
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = False
        mock_settings_cls.load.return_value = settings

        req = self._make_request("127.0.0.1")
        assert _is_genuine_localhost(req) is False

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_non_localhost_rejected(self, mock_tunnel_fn, mock_settings_cls):
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        req = self._make_request("192.168.1.5")
        assert _is_genuine_localhost(req) is False

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_ipv6_localhost(self, mock_tunnel_fn, mock_settings_cls):
        from pocketpaw.dashboard import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": False}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("::1")
        assert _is_genuine_localhost(req) is True

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_proxy_header_blocks_bypass_when_tunnel_inactive(
        self, mock_tunnel_fn, mock_settings_cls
    ):
        """Regression test for issue #871.

        A remote client that spoofs X-Forwarded-For: 127.0.0.1 must be blocked
        even when the tunnel manager reports inactive.  Previously the proxy-header
        check was only performed when tunnel.get_status()["active"] was True,
        allowing nginx / Caddy / ngrok deployments to be exploited.
        """
        from pocketpaw.dashboard_auth import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        # Tunnel is NOT active — this was the exploitable code path before the fix.
        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": False}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1", headers={"x-forwarded-for": "127.0.0.1"})
        assert _is_genuine_localhost(req) is False

    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth.get_tunnel_manager")
    def test_cf_connecting_ip_blocks_bypass_when_tunnel_inactive(
        self, mock_tunnel_fn, mock_settings_cls
    ):
        """Regression test for issue #871 — Cf-Connecting-Ip variant."""
        from pocketpaw.dashboard_auth import _is_genuine_localhost

        settings = MagicMock()
        settings.localhost_auth_bypass = True
        mock_settings_cls.load.return_value = settings

        tunnel = MagicMock()
        tunnel.get_status.return_value = {"active": False}
        mock_tunnel_fn.return_value = tunnel

        req = self._make_request("127.0.0.1", headers={"cf-connecting-ip": "8.8.8.8"})
        assert _is_genuine_localhost(req) is False


# ---------------------------------------------------------------------------
# Dashboard integration tests (auth middleware, headers, CORS, session exchange)
# ---------------------------------------------------------------------------


@pytest.fixture
def test_client():
    """Create a FastAPI TestClient for the dashboard app."""
    from starlette.testclient import TestClient

    from pocketpaw.dashboard import app

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


class TestFrontendSvgSafety:
    def test_memory_graph_uses_sanitized_svg_insertion(self):
        js_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "pocketpaw"
            / "frontend"
            / "js"
            / "features"
            / "transparency.js"
        )
        source = js_path.read_text(encoding="utf-8")

        assert "safeInsertGraphSvg(container, svg)" in source
        assert "container.innerHTML = graphUnavailable ? '' : svg;" not in source

    def test_memory_prune_requires_confirmation(self):
        js_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "pocketpaw"
            / "frontend"
            / "js"
            / "features"
            / "transparency.js"
        )
        source = js_path.read_text(encoding="utf-8")

        assert "pruneMemories()" in source
        assert "confirm(" in source
        assert "Prune memories older than" in source

    def test_memory_delete_requires_confirmation(self):
        js_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "pocketpaw"
            / "frontend"
            / "js"
            / "features"
            / "transparency.js"
        )
        source = js_path.read_text(encoding="utf-8")

        assert "deleteMemory(id)" in source
        assert "Delete this memory permanently?" in source


class TestSessionTokenEndpoint:
    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-abc")
    @patch("pocketpaw.dashboard_auth.Settings")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
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

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-abc")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
    def test_exchange_invalid_master(self, mock_local, mock_token, test_client):
        resp = test_client.post(
            "/api/auth/session",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


class TestAuthMiddlewareSessionToken:
    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-xyz")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    def test_session_token_accepted(self, mock_local, mock_token, test_client):
        session = create_session_token("master-xyz", ttl_hours=1)
        resp = test_client.get(
            "/api/channels/status",
            headers={"Authorization": f"Bearer {session}"},
        )
        # Should not be 401 (may be other status depending on channel state)
        assert resp.status_code != 401

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-xyz")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    def test_no_token_rejected(self, mock_local, mock_token, test_client):
        resp = test_client.get("/api/channels/status")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Issue #851 — /api/auth/login rate-limit brute-force fix
# ---------------------------------------------------------------------------


class TestLoginRateLimit:
    """Auth login endpoints must be rate-limited even though they are exempt from
    token auth.  Verifies the OWASP A07 fix: auth_limiter fires BEFORE the
    exempt-paths check so unlimited brute-force is no longer possible.

    Tests call _auth_dispatch directly to avoid full-app test_client side effects.
    """

    def _make_request(self, path: str, method: str = "POST", client_ip: str = "1.2.3.4"):
        req = MagicMock()
        req.method = method
        req.url.path = path
        req.client = MagicMock()
        req.client.host = client_ip
        req.query_params = {}
        req.headers = {}
        req.cookies = {}
        req.state = MagicMock()
        return req

    def _denied_rl_info(self):
        from pocketpaw.security.rate_limiter import RateLimitInfo

        return RateLimitInfo(allowed=False, limit=5, remaining=0, reset_after=1.0)

    def _allowed_rl_info(self):
        from pocketpaw.security.rate_limiter import RateLimitInfo

        return RateLimitInfo(allowed=True, limit=5, remaining=4, reset_after=1.0)

    @patch("pocketpaw.dashboard_auth.auth_limiter")
    @patch("pocketpaw.dashboard_auth._audit_auth_event")
    async def test_login_rate_limited_when_over_limit(self, mock_audit, mock_auth_limiter):
        """_auth_dispatch must return 429 for /api/auth/login when auth_limiter denies."""
        from pocketpaw.dashboard_auth import _auth_dispatch

        mock_auth_limiter.check.return_value = self._denied_rl_info()
        req = self._make_request("/api/auth/login")
        resp = await _auth_dispatch(req)
        assert resp is not None
        assert resp.status_code == 429
        mock_auth_limiter.check.assert_called_once_with("1.2.3.4")

    @patch("pocketpaw.dashboard_auth.auth_limiter")
    @patch("pocketpaw.dashboard_auth._audit_auth_event")
    async def test_v1_login_rate_limited_when_over_limit(self, mock_audit, mock_auth_limiter):
        """_auth_dispatch must return 429 for /api/v1/auth/login when denied."""
        from pocketpaw.dashboard_auth import _auth_dispatch

        mock_auth_limiter.check.return_value = self._denied_rl_info()
        req = self._make_request("/api/v1/auth/login")
        resp = await _auth_dispatch(req)
        assert resp is not None
        assert resp.status_code == 429
        mock_auth_limiter.check.assert_called_once()

    @patch("pocketpaw.dashboard_auth.auth_limiter")
    async def test_login_allowed_when_within_limit(self, mock_auth_limiter):
        """_auth_dispatch must return None (allow-through) when auth_limiter allows."""
        from pocketpaw.dashboard_auth import _auth_dispatch

        mock_auth_limiter.check.return_value = self._allowed_rl_info()
        req = self._make_request("/api/auth/login")
        resp = await _auth_dispatch(req)
        # Returns None = allow through to handler (login path is still exempt from token auth)
        assert resp is None
        mock_auth_limiter.check.assert_called_once()

    @patch("pocketpaw.dashboard_auth.auth_limiter")
    @patch("pocketpaw.dashboard_auth._audit_auth_event")
    async def test_login_rate_limit_audit_on_block(self, mock_audit, mock_auth_limiter):
        """A 429 block on /api/auth/login must emit an audit event."""
        from pocketpaw.dashboard_auth import _auth_dispatch

        mock_auth_limiter.check.return_value = self._denied_rl_info()
        req = self._make_request("/api/auth/login")
        await _auth_dispatch(req)
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "brute_force_blocked"
        assert mock_audit.call_args[1]["status"] == "block"

    @patch("pocketpaw.dashboard_auth.auth_limiter")
    @patch("pocketpaw.dashboard_auth._audit_auth_event")
    async def test_qr_endpoint_rate_limited_when_over_limit(self, mock_audit, mock_auth_limiter):
        """_auth_dispatch must return 429 for /api/qr when auth_limiter denies."""
        from pocketpaw.dashboard_auth import _auth_dispatch

        mock_auth_limiter.check.return_value = self._denied_rl_info()
        req = self._make_request("/api/qr", method="GET")
        resp = await _auth_dispatch(req)
        assert resp is not None
        assert resp.status_code == 429
        mock_auth_limiter.check.assert_called_once()

    @patch("pocketpaw.dashboard_auth.auth_limiter")
    async def test_static_assets_not_rate_limited(self, mock_auth_limiter):
        """_auth_dispatch must NOT call auth_limiter for static assets."""
        from pocketpaw.dashboard_auth import _auth_dispatch

        req = self._make_request("/static/some-asset.js", method="GET")
        await _auth_dispatch(req)
        mock_auth_limiter.check.assert_not_called()
