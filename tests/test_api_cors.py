# Tests for API CORS configuration.
# Created: 2026-02-20

from pocketpaw.api.v1 import _V1_ROUTERS, mount_v1_routers


class TestV1RouterRegistration:
    """Tests for v1 router mount system."""

    def test_v1_routers_list_complete(self):
        """All expected domain routers are listed."""
        router_modules = [r[0] for r in _V1_ROUTERS]
        assert "pocketpaw.api.v1.auth" in router_modules
        assert "pocketpaw.api.v1.sessions" in router_modules
        assert "pocketpaw.api.v1.health" in router_modules
        assert "pocketpaw.api.v1.identity" in router_modules
        assert "pocketpaw.api.v1.settings" in router_modules
        assert "pocketpaw.api.v1.channels" in router_modules
        assert "pocketpaw.api.v1.memory" in router_modules
        assert "pocketpaw.api.v1.mcp" in router_modules
        assert "pocketpaw.api.v1.skills" in router_modules
        assert "pocketpaw.api.v1.webhooks" in router_modules
        assert "pocketpaw.api.v1.backends" in router_modules

    def test_v1_routers_count(self):
        """Verify total number of registered routers."""
        assert len(_V1_ROUTERS) == 21

    def test_mount_v1_routers_succeeds(self):
        """mount_v1_routers should not raise on a real FastAPI app."""
        from fastapi import FastAPI

        app = FastAPI()
        mount_v1_routers(app)
        # Check that routes were added
        route_paths = [r.path for r in app.routes if hasattr(r, "path")]
        # Should have at least auth and sessions routes
        assert any("/api/v1/auth/session" in p for p in route_paths)
        assert any("/api/v1/sessions" in p for p in route_paths)
        assert any("/api/v1/health" in p for p in route_paths)


class TestCORSConfig:
    """Tests for CORS configuration."""

    def test_cors_origins_include_tauri(self):
        """Tauri origins should be in the CORS config."""
        from pocketpaw.dashboard import _BUILTIN_ORIGINS

        assert "tauri://localhost" in _BUILTIN_ORIGINS
        assert "http://localhost:1420" in _BUILTIN_ORIGINS

    def test_api_cors_allowed_origins_in_settings(self):
        """api_cors_allowed_origins field exists in Settings."""
        from pocketpaw.config import Settings

        assert "api_cors_allowed_origins" in Settings.model_fields
        # Default should be empty list
        s = Settings()
        assert s.api_cors_allowed_origins == []
