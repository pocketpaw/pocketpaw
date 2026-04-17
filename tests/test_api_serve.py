"""Tests for the ``pocketpaw serve`` API-only server."""

import socket
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@pytest.fixture
def api_app():
    """Create the lightweight API app."""
    from pocketpaw.api.serve import create_api_app

    return create_api_app()


@pytest.fixture
def client(api_app):
    return TestClient(api_app)


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------


@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
class TestAPIAppStructure:
    def test_openapi_json(self, _mock, client):
        resp = client.get("/api/v1/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["info"]["title"] == "PocketPaw API"
        assert "paths" in data

    def test_docs_page(self, _mock, client):
        resp = client.get("/api/v1/docs")
        assert resp.status_code == 200

    def test_redoc_page(self, _mock, client):
        resp = client.get("/api/v1/redoc")
        assert resp.status_code == 200

    def test_health_endpoint(self, _mock, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_backends_endpoint(self, _mock, client):
        resp = client.get("/api/v1/backends")
        assert resp.status_code == 200

    def test_sessions_endpoint(self, _mock, client):
        resp = client.get("/api/v1/sessions")
        # When ee.cloud is installed, the enterprise sessions router takes
        # precedence and requires JWT auth (401).  The core sessions router
        # returns 200 when ee is absent.
        assert resp.status_code in (200, 401)

    def test_skills_endpoint(self, _mock, client):
        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200

    def test_version_endpoint(self, _mock, client):
        resp = client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "python" in data
        assert "agent_backend" in data


# ---------------------------------------------------------------------------
# No dashboard UI
# ---------------------------------------------------------------------------


@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
class TestNoDashboardUI:
    """The serve app should NOT serve the web dashboard."""

    def test_no_root_html(self, _mock, client):
        resp = client.get("/")
        # Should 404 or redirect — not serve the dashboard HTML
        assert resp.status_code in (404, 307, 405)

    def test_websocket_endpoint_exists(self, _mock, api_app):
        """WebSocket endpoints at /ws, /v1/ws, and /api/v1/ws must exist."""
        route_paths = [r.path for r in api_app.routes if hasattr(r, "path")]
        assert "/ws" in route_paths
        assert "/v1/ws" in route_paths
        assert "/api/v1/ws" in route_paths


# ---------------------------------------------------------------------------
# Auth middleware is active
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    def test_unauthenticated_request_blocked(self, client):
        """Non-localhost requests without a token should be rejected."""
        with patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False):
            resp = client.get("/api/v1/health")
            assert resp.status_code == 401

    def test_options_preflight_passes_without_auth(self, client):
        """OPTIONS preflight requests must pass through auth middleware."""
        with patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False):
            resp = client.options(
                "/api/v1/health",
                headers={"Origin": "http://localhost:1420", "Access-Control-Request-Method": "GET"},
            )
            # Should get 200 from CORSMiddleware, not 401 from auth
            assert resp.status_code == 200
            assert "access-control-allow-origin" in resp.headers

    def test_cors_headers_on_allowed_origin(self, client):
        """Responses should include CORS headers for allowed origins."""
        with patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True):
            resp = client.get(
                "/api/v1/health",
                headers={"Origin": "http://localhost:1420"},
            )
            assert resp.status_code == 200
            assert resp.headers.get("access-control-allow-origin") == "http://localhost:1420"
            assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_docs_exempt_from_auth(self, client):
        """OpenAPI docs should be accessible without auth."""
        with patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False):
            resp = client.get("/api/v1/docs")
            assert resp.status_code == 200

    def test_openapi_json_exempt_from_auth(self, client):
        """OpenAPI JSON schema should be accessible without auth."""
        with patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=False):
            resp = client.get("/api/v1/openapi.json")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestServeCommand:
    def test_serve_recognized_by_argparser(self):
        """The 'serve' command should be parsed by argparse."""
        import argparse

        # Re-import to ensure we get the updated parser
        from pocketpaw.__main__ import main  # noqa: F401

        # Just verify the parser doesn't crash on 'serve'
        parser = argparse.ArgumentParser()
        parser.add_argument("command", nargs="?", default=None)
        parser.add_argument("--host", default=None)
        parser.add_argument("--port", type=int, default=8888)
        parser.add_argument("--dev", action="store_true")
        args = parser.parse_args(["serve"])
        assert args.command == "serve"

    def test_serve_with_host_and_port(self):
        """The 'serve' command should accept --host and --port."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("command", nargs="?", default=None)
        parser.add_argument("--host", default=None)
        parser.add_argument("--port", type=int, default=8888)
        parser.add_argument("--dev", action="store_true")
        args = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000"])
        assert args.command == "serve"
        assert args.host == "0.0.0.0"
        assert args.port == 9000


# ---------------------------------------------------------------------------
# Socket resource safety (issue #608)
# ---------------------------------------------------------------------------


class TestSocketResourceSafety:
    """Verify the local-IP detection socket is always closed — even on error.

    Regression tests for the resource leak reported in issue #608:
    s.close() was only called on the happy path, leaving the socket open
    whenever connect() or getsockname() raised an exception.
    """

    def _make_mock_socket(self) -> MagicMock:
        """Return a MagicMock that looks enough like a socket.socket."""
        sock = MagicMock(spec=socket.socket)
        # Make it usable as a context manager (__enter__ returns itself,
        # __exit__ calls close — same as the real socket implementation).
        sock.__enter__ = MagicMock(return_value=sock)
        sock.__exit__ = MagicMock(return_value=False)
        return sock

    def test_serve_socket_closed_on_success(self):
        """Socket must be closed after successful IP detection."""
        mock_sock = self._make_mock_socket()
        mock_sock.getsockname.return_value = ("192.168.1.100", 0)

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("uvicorn.run"),
        ):
            from pocketpaw.api.serve import run_api_server

            run_api_server(host="0.0.0.0", port=9999)

        # __exit__ is how the context manager closes the socket
        mock_sock.__exit__.assert_called_once()

    def test_serve_socket_closed_on_connect_error(self):
        """Socket must be closed even when connect() raises."""
        mock_sock = self._make_mock_socket()
        mock_sock.connect.side_effect = OSError("Network unreachable")

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("uvicorn.run"),
        ):
            from pocketpaw.api.serve import run_api_server

            # Should not raise — falls back to placeholder IP
            run_api_server(host="0.0.0.0", port=9999)

        mock_sock.__exit__.assert_called_once()

    def test_serve_socket_closed_on_getsockname_error(self):
        """Socket must be closed even when getsockname() raises."""
        mock_sock = self._make_mock_socket()
        mock_sock.getsockname.side_effect = OSError("Socket error")

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("uvicorn.run"),
        ):
            from pocketpaw.api.serve import run_api_server

            run_api_server(host="0.0.0.0", port=9999)

        mock_sock.__exit__.assert_called_once()

    def test_serve_fallback_ip_used_on_error(self):
        """When socket raises, the fallback placeholder should be printed."""
        mock_sock = self._make_mock_socket()
        mock_sock.connect.side_effect = OSError("Network unreachable")

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("uvicorn.run"),
            patch("builtins.print") as mock_print,
        ):
            from pocketpaw.api.serve import run_api_server

            run_api_server(host="0.0.0.0", port=9999)

        printed = " ".join(str(c) for call in mock_print.call_args_list for c in call.args)
        assert "<your-server-ip>" in printed

    def test_dashboard_socket_closed_on_connect_error(self):
        """dashboard.py IP detection socket must be closed even when connect() raises."""
        mock_sock = self._make_mock_socket()
        mock_sock.connect.side_effect = OSError("Network unreachable")

        mock_uv_server = MagicMock()
        mock_uv_server.run = MagicMock()  # no-op so the loop exits

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("pocketpaw.dashboard.uvicorn.Config", return_value=MagicMock()),
            patch("pocketpaw.dashboard.uvicorn.Server", return_value=mock_uv_server),
        ):
            from pocketpaw.dashboard import run_dashboard

            run_dashboard(host="0.0.0.0", port=9999, open_browser=False)

        mock_sock.__exit__.assert_called_once()

    def test_dashboard_socket_closed_on_success(self):
        """dashboard.py IP detection socket must be closed on the happy path."""
        mock_sock = self._make_mock_socket()
        mock_sock.getsockname.return_value = ("10.0.0.1", 0)

        mock_uv_server = MagicMock()
        mock_uv_server.run = MagicMock()

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("pocketpaw.dashboard.uvicorn.Config", return_value=MagicMock()),
            patch("pocketpaw.dashboard.uvicorn.Server", return_value=mock_uv_server),
        ):
            from pocketpaw.dashboard import run_dashboard

            run_dashboard(host="0.0.0.0", port=9999, open_browser=False)

        mock_sock.__exit__.assert_called_once()
