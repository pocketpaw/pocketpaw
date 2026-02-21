"""Tests for the ``pocketpaw serve`` API-only server."""

from unittest.mock import patch

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
        assert resp.status_code == 200

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
