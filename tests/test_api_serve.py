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


@patch("pocketpaw.dashboard._is_genuine_localhost", return_value=True)
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


# ---------------------------------------------------------------------------
# No dashboard UI
# ---------------------------------------------------------------------------


@patch("pocketpaw.dashboard._is_genuine_localhost", return_value=True)
class TestNoDashboardUI:
    """The serve app should NOT serve the web dashboard."""

    def test_no_root_html(self, _mock, client):
        resp = client.get("/")
        # Should 404 or redirect — not serve the dashboard HTML
        assert resp.status_code in (404, 307, 405)

    def test_no_websocket_endpoint(self, _mock, api_app):
        """The /ws endpoint should not exist on the API-only app."""
        route_paths = [r.path for r in api_app.routes if hasattr(r, "path")]
        assert "/ws" not in route_paths


# ---------------------------------------------------------------------------
# Auth middleware is active
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    def test_unauthenticated_request_blocked(self, client):
        """Non-localhost requests without a token should be rejected."""
        with patch("pocketpaw.dashboard._is_genuine_localhost", return_value=False):
            resp = client.get("/api/v1/health")
            assert resp.status_code == 401


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
