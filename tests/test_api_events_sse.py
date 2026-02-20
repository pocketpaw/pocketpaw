# Tests for API v1 events SSE endpoint.
# Created: 2026-02-20

from unittest.mock import patch

from pocketpaw.api.v1.events import router


class TestEventsRouter:
    """Tests for the events SSE router."""

    def test_router_has_stream_endpoint(self):
        """Router should have the /events/stream endpoint."""
        route_paths = [r.path for r in router.routes if hasattr(r, "path")]
        assert "/events/stream" in route_paths

    def test_router_tags(self):
        """Router should be tagged as Events."""
        assert "Events" in router.tags

    def test_router_registered_in_v1(self):
        """Events router should be in the v1 registry."""
        from pocketpaw.api.v1 import _V1_ROUTERS

        modules = [r[0] for r in _V1_ROUTERS]
        assert "pocketpaw.api.v1.events" in modules

    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
    def test_stream_endpoint_exists_on_dashboard(self, _mock):
        """The /api/v1/events/stream endpoint should be reachable on the dashboard app."""
        from pocketpaw.dashboard import app

        route_paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("/api/v1/events/stream" in p for p in route_paths)

    @patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
    def test_openapi_includes_events(self, _mock):
        """OpenAPI spec should include the events endpoint."""
        from fastapi.testclient import TestClient

        from pocketpaw.dashboard import app

        client = TestClient(app)
        resp = client.get("/api/v1/openapi.json")
        assert resp.status_code == 200
        paths = resp.json()["paths"]
        assert any("events/stream" in p for p in paths)
