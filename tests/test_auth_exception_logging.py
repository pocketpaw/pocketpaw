"""Tests for issue #627 — silent exception swallowing in auth paths.

Verifies that bare ``except Exception: pass`` blocks have been replaced with
``logger.warning(..., exc_info=True)`` so auth failures are never silently
discarded.

Covered paths:
  - ``_auth_dispatch`` step 4: API key validation (``dashboard_auth.py``)
  - ``_auth_dispatch`` step 5: OAuth2 token validation (``dashboard_auth.py``)
  - ``cookie_login`` OAuth2 branch (``dashboard_auth.py``)
  - ``cookie_login`` API key branch (``dashboard_auth.py``)
  - ``AuthorizationServer.exchange`` audit log (``api/oauth2/server.py``)
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.api.oauth2.server import AuthorizationServer
from pocketpaw.api.oauth2.storage import OAuthStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pkce_pair():
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


@pytest.fixture
def auth_test_client():
    """FastAPI TestClient for dashboard app used for black-box auth tests."""
    from starlette.testclient import TestClient

    from pocketpaw.dashboard import app

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# _auth_dispatch — API key exception logging (step 4)
# ---------------------------------------------------------------------------


class TestAuthDispatchApiKeyExceptionLogging:
    """When API key verification raises, a warning must be logged and the
    request must still result in 401 (not silently pass through)."""

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-token")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    @patch("pocketpaw.dashboard_auth.logger")
    async def test_api_key_exception_is_logged(self, mock_logger, mock_local, mock_token):
        """Exception during API key validation must be logged as a warning."""
        import pocketpaw.dashboard_auth as auth_mod

        # Patch the source module — lazy imports are resolved there
        with patch(
            "pocketpaw.api.api_keys.get_api_key_manager",
            side_effect=RuntimeError("DB unavailable"),
        ):
            req = MagicMock()
            req.method = "GET"
            req.url.path = "/api/channels/status"
            req.query_params.get = lambda k, d=None: "pp_bad_key" if k == "token" else d
            req.headers.get = lambda k, d=None: None
            req.cookies.get = lambda k, d=None: None
            req.client = MagicMock()
            req.client.host = "10.0.0.1"

            rl_result = MagicMock()
            rl_result.allowed = True
            rl_result.headers.return_value = {}
            with patch("pocketpaw.dashboard_auth.api_limiter") as mock_rl:
                mock_rl.check.return_value = rl_result
                req.state = MagicMock()
                await auth_mod._auth_dispatch(req)

        mock_logger.warning.assert_called_once()
        logged_msg = mock_logger.warning.call_args.args[0]
        assert "API key" in logged_msg

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-token")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    def test_api_key_exception_still_returns_401(self, mock_local, mock_token, auth_test_client):
        """A failing API key manager must not grant access — 401 expected."""
        with patch(
            "pocketpaw.api.api_keys.get_api_key_manager",
            side_effect=RuntimeError("DB unavailable"),
        ):
            resp = auth_test_client.get(
                "/api/channels/status",
                headers={"Authorization": "Bearer pp_some_key"},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# _auth_dispatch — OAuth2 exception logging (step 5)
# ---------------------------------------------------------------------------


class TestAuthDispatchOAuth2ExceptionLogging:
    """When OAuth2 token verification raises, a warning must be logged."""

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-token")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    @patch("pocketpaw.dashboard_auth.logger")
    async def test_oauth2_exception_is_logged(self, mock_logger, mock_local, mock_token):
        import pocketpaw.dashboard_auth as auth_mod

        with patch(
            "pocketpaw.api.oauth2.server.get_oauth_server",
            side_effect=RuntimeError("OAuth server unavailable"),
        ):
            req = MagicMock()
            req.method = "GET"
            req.url.path = "/api/channels/status"
            req.query_params.get = lambda k, d=None: "ppat_bad_token" if k == "token" else d
            req.headers.get = lambda k, d=None: None
            req.cookies.get = lambda k, d=None: None
            req.client = MagicMock()
            req.client.host = "10.0.0.1"

            rl_result = MagicMock()
            rl_result.allowed = True
            rl_result.headers.return_value = {}
            with patch("pocketpaw.dashboard_auth.api_limiter") as mock_rl:
                mock_rl.check.return_value = rl_result
                req.state = MagicMock()
                await auth_mod._auth_dispatch(req)

        mock_logger.warning.assert_called_once()
        logged_msg = mock_logger.warning.call_args.args[0]
        assert "OAuth2" in logged_msg

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-token")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    def test_oauth2_exception_still_returns_401(self, mock_local, mock_token, auth_test_client):
        with patch(
            "pocketpaw.api.oauth2.server.get_oauth_server",
            side_effect=RuntimeError("OAuth server crash"),
        ):
            resp = auth_test_client.get(
                "/api/channels/status",
                headers={"Authorization": "Bearer ppat_some_token"},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# cookie_login — OAuth2 exception logging
# ---------------------------------------------------------------------------


class TestCookieLoginOAuth2ExceptionLogging:
    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-xyz")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    @patch("pocketpaw.dashboard_auth.logger")
    def test_oauth2_login_exception_is_logged(
        self, mock_logger, mock_local, mock_token, auth_test_client
    ):
        """Exception during OAuth2 verification in cookie login must be logged."""
        with patch(
            "pocketpaw.api.oauth2.server.get_oauth_server",
            side_effect=RuntimeError("Verification boom"),
        ):
            resp = auth_test_client.post(
                "/api/auth/login",
                json={"token": "ppat_broken_token"},
            )
        assert resp.status_code == 401
        mock_logger.warning.assert_called()
        logged_msg = mock_logger.warning.call_args.args[0]
        assert "OAuth2" in logged_msg or "login" in logged_msg.lower()

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-xyz")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    def test_oauth2_login_exception_returns_401(self, mock_local, mock_token, auth_test_client):
        with patch(
            "pocketpaw.api.oauth2.server.get_oauth_server",
            side_effect=RuntimeError("Verification boom"),
        ):
            resp = auth_test_client.post(
                "/api/auth/login",
                json={"token": "ppat_broken_token"},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# cookie_login — API key exception logging
# ---------------------------------------------------------------------------


class TestCookieLoginApiKeyExceptionLogging:
    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-xyz")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    @patch("pocketpaw.dashboard_auth.logger")
    def test_api_key_login_exception_is_logged(
        self, mock_logger, mock_local, mock_token, auth_test_client
    ):
        """Exception during API key verification in cookie login must be logged."""
        with patch(
            "pocketpaw.api.api_keys.get_api_key_manager",
            side_effect=RuntimeError("Key store unavailable"),
        ):
            resp = auth_test_client.post(
                "/api/auth/login",
                json={"token": "pp_broken_api_key"},
            )
        assert resp.status_code == 401
        mock_logger.warning.assert_called()
        logged_msg = mock_logger.warning.call_args.args[0]
        assert "API key" in logged_msg or "login" in logged_msg.lower()

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value="master-xyz")
    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False)
    def test_api_key_login_exception_returns_401(self, mock_local, mock_token, auth_test_client):
        with patch(
            "pocketpaw.api.api_keys.get_api_key_manager",
            side_effect=RuntimeError("Key store unavailable"),
        ):
            resp = auth_test_client.post(
                "/api/auth/login",
                json={"token": "pp_broken_api_key"},
            )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AuthorizationServer.exchange — audit log exception logging
# ---------------------------------------------------------------------------


class TestOAuth2ServerAuditLogExceptionLogging:
    """When the audit log write raises inside exchange(), a warning must be
    logged and the token exchange must still succeed (audit failure is non-fatal)."""

    def _setup_server(self):
        storage = OAuthStorage()
        return AuthorizationServer(storage)

    def test_audit_log_failure_is_logged(self):
        server = self._setup_server()
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )

        # Patch in the security.audit module (lazy import resolves there)
        with patch(
            "pocketpaw.security.audit.get_audit_logger",
            side_effect=RuntimeError("Audit store unreachable"),
        ):
            with patch("pocketpaw.api.oauth2.server.logger") as mock_logger:
                result, error = server.exchange(
                    code=code,
                    client_id="pocketpaw-desktop",
                    code_verifier=verifier,
                )

        # Token exchange itself must succeed
        assert error is None
        assert result is not None
        assert result["access_token"].startswith("ppat_")

        # Warning must have been logged
        mock_logger.warning.assert_called_once()
        logged_msg = mock_logger.warning.call_args.args[0]
        assert "audit" in logged_msg.lower()

    def test_audit_log_failure_does_not_block_token_exchange(self):
        """Non-fatal: broken audit logger must not prevent token issuance."""
        server = self._setup_server()
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat sessions",
            code_challenge=challenge,
        )

        mock_audit = MagicMock()
        mock_audit.log_api_event.side_effect = RuntimeError("disk full")

        with patch("pocketpaw.security.audit.get_audit_logger", return_value=mock_audit):
            result, error = server.exchange(
                code=code,
                client_id="pocketpaw-desktop",
                code_verifier=verifier,
            )

        assert error is None
        assert result is not None
        assert result["token_type"] == "Bearer"
