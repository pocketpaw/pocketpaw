"""Tests for MCP OAuth flow — token storage, callback coordination,
preset flags, and dashboard endpoint.

Created: 2026-02-17
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.mcp.config import MCPServerConfig
from pocketpaw.mcp.presets import get_all_presets, get_preset, preset_to_config

# ======================================================================
# MCPTokenStorage tests
# ======================================================================


class TestMCPTokenStorage:
    @pytest.fixture
    def storage(self, tmp_path):
        with patch("pocketpaw.mcp.oauth_store.get_config_dir", return_value=tmp_path):
            from pocketpaw.mcp.oauth_store import MCPTokenStorage

            return MCPTokenStorage("test-server")

    async def test_get_tokens_empty(self, storage):
        result = await storage.get_tokens()
        assert result is None

    async def test_set_and_get_tokens(self, storage):
        from mcp.shared.auth import OAuthToken

        token = OAuthToken(access_token="access_123", token_type="Bearer", refresh_token="ref_456")
        await storage.set_tokens(token)

        loaded = await storage.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "access_123"
        assert loaded.refresh_token == "ref_456"
        assert loaded.token_type == "Bearer"

    async def test_get_client_info_empty(self, storage):
        result = await storage.get_client_info()
        assert result is None

    async def test_set_and_get_client_info(self, storage):
        from mcp.shared.auth import OAuthClientInformationFull

        info = OAuthClientInformationFull(
            client_id="cid_123",
            client_secret="secret_456",
            redirect_uris=["http://localhost:8888/callback"],
        )
        await storage.set_client_info(info)

        loaded = await storage.get_client_info()
        assert loaded is not None
        assert loaded.client_id == "cid_123"
        assert loaded.client_secret == "secret_456"

    async def test_tokens_and_client_info_coexist(self, storage):
        """Both tokens and client info can be stored in the same file."""
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

        token = OAuthToken(access_token="tok")
        info = OAuthClientInformationFull(client_id="cid", redirect_uris=["http://localhost/cb"])

        await storage.set_tokens(token)
        await storage.set_client_info(info)

        loaded_tok = await storage.get_tokens()
        loaded_info = await storage.get_client_info()
        assert loaded_tok.access_token == "tok"
        assert loaded_info.client_id == "cid"

    async def test_file_permissions(self, storage, tmp_path):
        """Token file should be chmod 0600."""
        import os
        import stat

        from mcp.shared.auth import OAuthToken

        await storage.set_tokens(OAuthToken(access_token="x"))

        path = tmp_path / "mcp_oauth" / "test-server.json"
        mode = os.stat(path).st_mode
        assert mode & stat.S_IRWXG == 0  # No group perms
        assert mode & stat.S_IRWXO == 0  # No other perms
        assert mode & stat.S_IRUSR  # Owner read

    async def test_corrupted_file_returns_none(self, storage, tmp_path):
        """Corrupted JSON should return None, not raise."""
        path = tmp_path / "mcp_oauth" / "test-server.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json{{{")

        result = await storage.get_tokens()
        assert result is None

        result = await storage.get_client_info()
        assert result is None


# ======================================================================
# OAuth callback coordination tests
# ======================================================================


class TestOAuthCallbackCoordination:
    def setup_method(self):
        from pocketpaw.mcp import manager

        manager._oauth_pending.clear()

    def teardown_method(self):
        from pocketpaw.mcp import manager

        manager._oauth_pending.clear()

    def test_set_oauth_callback_result_resolves_future(self):
        from pocketpaw.mcp.manager import set_oauth_callback_result

        loop = asyncio.new_event_loop()
        future = loop.create_future()

        from pocketpaw.mcp import manager

        manager._oauth_pending["test_state_123"] = future

        result = set_oauth_callback_result("test_state_123", "auth_code_456")
        assert result is True
        assert future.done()
        assert future.result() == ("auth_code_456", "test_state_123")
        loop.close()

    def test_set_oauth_callback_result_unknown_state(self):
        from pocketpaw.mcp.manager import set_oauth_callback_result

        result = set_oauth_callback_result("nonexistent_state", "code")
        assert result is False

    def test_set_oauth_callback_result_already_resolved(self):
        from pocketpaw.mcp.manager import set_oauth_callback_result

        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_result(("old_code", "old_state"))

        from pocketpaw.mcp import manager

        manager._oauth_pending["done_state"] = future

        result = set_oauth_callback_result("done_state", "new_code")
        assert result is False
        loop.close()

    def test_set_ws_broadcast(self):
        from pocketpaw.mcp import manager
        from pocketpaw.mcp.manager import set_ws_broadcast

        old = manager._ws_broadcast
        try:
            fn = MagicMock()
            set_ws_broadcast(fn)
            assert manager._ws_broadcast is fn
        finally:
            manager._ws_broadcast = old


# ======================================================================
# Preset OAuth flag tests
# ======================================================================


class TestPresetOAuthFlags:
    _OAUTH_PRESET_IDS = {
        "github",
        "notion",
        "atlassian",
        "stripe",
        "cloudflare",
        "supabase",
        "vercel",
        "gitlab",
        "figma",
    }

    def test_http_presets_have_oauth_true(self):
        for preset_id in self._OAUTH_PRESET_IDS:
            preset = get_preset(preset_id)
            assert preset is not None, f"Preset {preset_id} not found"
            assert preset.oauth is True, f"Preset {preset_id} should have oauth=True"
            assert preset.transport == "http"

    def test_stdio_presets_have_oauth_false(self):
        for preset in get_all_presets():
            if preset.transport == "stdio":
                assert preset.oauth is False, f"Stdio preset {preset.id} should have oauth=False"

    def test_preset_to_config_passes_oauth(self):
        preset = get_preset("github")
        config = preset_to_config(preset)
        assert config.oauth is True

    def test_preset_to_config_stdio_no_oauth(self):
        preset = get_preset("sentry")
        config = preset_to_config(preset, env={"SENTRY_ACCESS_TOKEN": "x"})
        assert config.oauth is False


# ======================================================================
# MCPServerConfig oauth field tests
# ======================================================================


class TestConfigOAuthField:
    def test_to_dict_includes_oauth_when_true(self):
        config = MCPServerConfig(name="test", oauth=True)
        d = config.to_dict()
        assert d["oauth"] is True

    def test_to_dict_excludes_oauth_when_false(self):
        config = MCPServerConfig(name="test", oauth=False)
        d = config.to_dict()
        assert "oauth" not in d

    def test_from_dict_reads_oauth(self):
        config = MCPServerConfig.from_dict({"name": "test", "oauth": True})
        assert config.oauth is True

    def test_from_dict_defaults_oauth_false(self):
        config = MCPServerConfig.from_dict({"name": "test"})
        assert config.oauth is False


# ======================================================================
# Dashboard OAuth callback endpoint tests
# ======================================================================

_TEST_TOKEN = "test-oauth-token-12345"


def _auth(**extra):
    h = {"Authorization": f"Bearer {_TEST_TOKEN}"}
    h.update(extra)
    return h


class TestDashboardOAuthCallback:
    @pytest.fixture(autouse=True)
    def _mock_token(self):
        with patch("pocketpaw.dashboard.get_access_token", return_value=_TEST_TOKEN):
            yield

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from pocketpaw.dashboard import app

        return TestClient(app, raise_server_exceptions=False)

    @patch("pocketpaw.mcp.manager.set_oauth_callback_result")
    def test_callback_success(self, mock_set_result, client):
        mock_set_result.return_value = True
        res = client.get("/api/mcp/oauth/callback?code=abc123&state=xyz789")
        assert res.status_code == 200
        assert "Authenticated" in res.text
        assert "window.close" in res.text
        mock_set_result.assert_called_once_with("xyz789", "abc123")

    @patch("pocketpaw.mcp.manager.set_oauth_callback_result")
    def test_callback_expired_flow(self, mock_set_result, client):
        mock_set_result.return_value = False
        res = client.get("/api/mcp/oauth/callback?code=abc&state=xyz")
        assert res.status_code == 400
        assert "expired" in res.text.lower() or "not found" in res.text.lower()

    def test_callback_missing_params(self, client):
        # Empty defaults → 400
        res = client.get("/api/mcp/oauth/callback")
        assert res.status_code == 400
        # Explicit empty strings → also 400
        res = client.get("/api/mcp/oauth/callback?code=&state=")
        assert res.status_code == 400

    def test_callback_is_auth_exempt(self, client):
        """OAuth callback should work without auth token."""
        with patch("pocketpaw.mcp.manager.set_oauth_callback_result", return_value=True):
            res = client.get(
                "/api/mcp/oauth/callback?code=test&state=test",
                # No auth header
            )
            # Should not get 401 — the endpoint is auth-exempt
            assert res.status_code != 401

    @patch("pocketpaw.mcp.config.load_mcp_config")
    def test_presets_include_oauth_field(self, mock_load, client):
        mock_load.return_value = []
        res = client.get("/api/mcp/presets", headers=_auth())
        assert res.status_code == 200
        data = res.json()
        github = next(p for p in data if p["id"] == "github")
        assert github["oauth"] is True
        sentry = next(p for p in data if p["id"] == "sentry")
        assert sentry["oauth"] is False
