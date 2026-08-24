# Tests for API CORS configuration.
# Created: 2026-02-20
# Updated 2026-08-24 (fix/cors-headers-on-unhandled-500): the origin policy moved
# out of dashboard.py into the shared pocketpaw.api.cors module (dashboard.py and
# api/serve.py carried byte-identical copies), so these read it from there. The
# behaviour these assert is unchanged.

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
        assert len(_V1_ROUTERS) >= 26

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
        from pocketpaw.api.cors import BUILTIN_ORIGINS

        assert "tauri://localhost" in BUILTIN_ORIGINS
        assert "http://localhost:1420" in BUILTIN_ORIGINS

    def test_localhost_origins_match_the_middleware_regex(self):
        """The 500 path reuses `origin_allowed`; it must not be looser than the
        regex handed to CORSMiddleware, or a crash would answer origins the
        happy path refuses."""
        from pocketpaw.api.cors import origin_allowed

        assert origin_allowed("http://localhost:5173", [])
        assert origin_allowed("https://127.0.0.1", [])
        assert origin_allowed("https://paw.example.com", ["https://paw.example.com"])
        assert not origin_allowed("https://evil.example.com", [])
        assert not origin_allowed("https://localhost.evil.example.com", [])
        assert not origin_allowed("", [])

    def test_api_cors_allowed_origins_in_settings(self):
        """api_cors_allowed_origins field exists in Settings."""
        from pocketpaw.config import Settings

        assert "api_cors_allowed_origins" in Settings.model_fields
        # Default should be empty list
        s = Settings()
        assert s.api_cors_allowed_origins == []
