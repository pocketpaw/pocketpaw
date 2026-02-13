"""Tests for MCP Server Presets — catalog registry + dashboard endpoints.

Created: 2026-02-09
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketclaw.mcp.config import MCPServerConfig
from pocketclaw.mcp.presets import (
    EnvKeySpec,
    MCPPreset,
    get_all_presets,
    get_preset,
    get_presets_by_category,
    preset_to_config,
)

# ======================================================================
# Registry tests
# ======================================================================


class TestPresetRegistry:
    def test_all_presets_have_unique_ids(self):
        presets = get_all_presets()
        ids = [p.id for p in presets]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"

    def test_all_presets_have_required_fields(self):
        for p in get_all_presets():
            assert p.id, f"Preset missing id: {p}"
            assert p.name, f"Preset {p.id} missing name"
            assert p.description, f"Preset {p.id} missing description"
            assert p.icon, f"Preset {p.id} missing icon"
            assert p.category in (
                "dev",
                "productivity",
                "data",
                "search",
                "devops",
            ), f"Preset {p.id} has invalid category: {p.category}"
            if p.transport == "stdio":
                assert p.command, f"Preset {p.id} (stdio) missing command"
            else:
                assert p.url, f"Preset {p.id} ({p.transport}) missing url"

    def test_get_preset_returns_correct(self):
        p = get_preset("github")
        assert p is not None
        assert p.name == "GitHub"
        assert p.transport == "http"
        assert "githubcopilot.com" in p.url
        assert p.env_keys == []

    def test_get_preset_notion_oauth(self):
        """Notion uses hosted OAuth via HTTP — no env keys, no npm package."""
        p = get_preset("notion")
        assert p is not None
        assert p.transport == "http"
        assert p.url == "https://mcp.notion.com/mcp"
        assert p.package == ""
        assert p.env_keys == []

    def test_get_preset_unknown_returns_none(self):
        assert get_preset("nonexistent-server") is None

    def test_get_all_presets_returns_list(self):
        presets = get_all_presets()
        assert isinstance(presets, list)
        assert len(presets) >= 15

    def test_get_presets_by_category_filters(self):
        dev = get_presets_by_category("dev")
        assert all(p.category == "dev" for p in dev)
        assert len(dev) >= 1

        empty = get_presets_by_category("nonexistent")
        assert empty == []

    def test_preset_to_config_basic_stdio(self):
        p = get_preset("sentry")
        config = preset_to_config(p, env={"SENTRY_ACCESS_TOKEN": "sntrys_123"})
        assert isinstance(config, MCPServerConfig)
        assert config.name == "sentry"
        assert config.transport == "stdio"
        assert config.command == "npx"
        assert config.args == ["-y", "@sentry/mcp-server"]
        assert config.env == {"SENTRY_ACCESS_TOKEN": "sntrys_123"}
        assert config.enabled is True

    def test_preset_to_config_basic_http(self):
        p = get_preset("github")
        config = preset_to_config(p)
        assert isinstance(config, MCPServerConfig)
        assert config.name == "github"
        assert config.transport == "http"
        assert "githubcopilot.com" in config.url
        assert config.enabled is True

    def test_preset_to_config_extra_args(self):
        p = get_preset("filesystem")
        config = preset_to_config(p, extra_args=["/home/user/docs"])
        assert config.args[-1] == "/home/user/docs"
        assert config.env == {}

    def test_preset_to_config_no_env(self):
        p = get_preset("fetch")
        config = preset_to_config(p)
        assert config.env == {}
        assert config.name == "fetch"

    def test_preset_to_config_does_not_mutate_original(self):
        p = get_preset("filesystem")
        original_args = list(p.args)
        preset_to_config(p, extra_args=["/tmp"])
        assert p.args == original_args

    def test_preset_to_config_applies_transform(self):
        """Transform templates rewrite env values automatically."""
        p = MCPPreset(
            id="test-transform",
            name="Test",
            description="test",
            icon="star",
            category="dev",
            package="test-pkg",
            command="npx",
            args=["-y", "test-pkg"],
            env_keys=[
                EnvKeySpec(
                    key="AUTH_HEADER",
                    label="Token",
                    transform='{"Authorization": "Bearer {value}"}',
                ),
            ],
        )
        config = preset_to_config(p, env={"AUTH_HEADER": "tok_abc123"})
        assert config.env["AUTH_HEADER"] == '{"Authorization": "Bearer tok_abc123"}'

    def test_preset_to_config_no_transform_passthrough(self):
        p = get_preset("brave-search")
        config = preset_to_config(p, env={"BRAVE_API_KEY": "BSA_raw"})
        assert config.env["BRAVE_API_KEY"] == "BSA_raw"

    def test_preset_to_config_transform_skips_empty_value(self):
        """Transform is not applied when the value is empty."""
        p = MCPPreset(
            id="test-transform",
            name="Test",
            description="test",
            icon="star",
            category="dev",
            package="test-pkg",
            command="npx",
            args=["-y", "test-pkg"],
            env_keys=[
                EnvKeySpec(
                    key="AUTH_HEADER",
                    label="Token",
                    transform='{"Authorization": "Bearer {value}"}',
                ),
            ],
        )
        config = preset_to_config(p, env={"AUTH_HEADER": ""})
        assert config.env["AUTH_HEADER"] == ""


# ======================================================================
# Dashboard route tests
# ======================================================================

_TEST_TOKEN = "test-preset-token-12345"


def _auth(**extra):
    h = {"Authorization": f"Bearer {_TEST_TOKEN}"}
    h.update(extra)
    return h


class TestPresetRoutes:
    @pytest.fixture(autouse=True)
    def _mock_token(self):
        with patch("pocketclaw.dashboard.get_access_token", return_value=_TEST_TOKEN):
            yield

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from pocketclaw.dashboard import app

        return TestClient(app, raise_server_exceptions=False)

    @patch("pocketclaw.mcp.config.load_mcp_config")
    def test_get_presets_returns_list(self, mock_load, client):
        mock_load.return_value = [MCPServerConfig(name="sentry")]
        res = client.get("/api/mcp/presets", headers=_auth())
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 28

        # Check installed flag
        sentry = next(p for p in data if p["id"] == "sentry")
        assert sentry["installed"] is True
        fetch = next(p for p in data if p["id"] == "fetch")
        assert fetch["installed"] is False

        # Check env_keys serialization
        assert isinstance(sentry["env_keys"], list)
        assert sentry["env_keys"][0]["key"] == "SENTRY_ACCESS_TOKEN"

        # HTTP presets have url and empty env_keys
        github = next(p for p in data if p["id"] == "github")
        assert github["transport"] == "http"
        assert "url" in github

    @patch("pocketclaw.mcp.manager.get_mcp_manager")
    @patch("pocketclaw.mcp.presets.get_preset")
    def test_install_preset_success(self, mock_get_preset, mock_mgr_fn, client):
        preset = get_preset("github")
        mock_get_preset.return_value = preset

        mgr = MagicMock()
        mgr.add_server_config = MagicMock()
        mgr.start_server = AsyncMock(return_value=True)
        mgr.discover_tools = MagicMock(return_value=[])
        mock_mgr_fn.return_value = mgr

        res = client.post(
            "/api/mcp/presets/install",
            json={
                "preset_id": "github",
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test123"},
            },
            headers=_auth(),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["connected"] is True
        mgr.add_server_config.assert_called_once()
        mgr.start_server.assert_called_once()

    @patch("pocketclaw.mcp.presets.get_preset")
    def test_install_preset_missing_env(self, mock_get_preset, client):
        preset = get_preset("sentry")  # Has required SENTRY_ACCESS_TOKEN
        mock_get_preset.return_value = preset

        res = client.post(
            "/api/mcp/presets/install",
            json={"preset_id": "sentry", "env": {}},
            headers=_auth(),
        )
        assert res.status_code == 400
        assert "Missing required" in res.json()["error"]

    def test_install_preset_unknown(self, client):
        res = client.post(
            "/api/mcp/presets/install",
            json={"preset_id": "nonexistent", "env": {}},
            headers=_auth(),
        )
        assert res.status_code == 404
        assert "Unknown preset" in res.json()["error"]

    @patch("pocketclaw.mcp.manager.get_mcp_manager")
    @patch("pocketclaw.mcp.presets.get_preset")
    def test_install_preset_no_required_env(self, mock_get_preset, mock_mgr_fn, client):
        preset = get_preset("fetch")  # No env keys
        mock_get_preset.return_value = preset

        mgr = MagicMock()
        mgr.add_server_config = MagicMock()
        mgr.start_server = AsyncMock(return_value=True)
        mgr.discover_tools = MagicMock(return_value=[])
        mock_mgr_fn.return_value = mgr

        res = client.post(
            "/api/mcp/presets/install",
            json={"preset_id": "fetch", "env": {}},
            headers=_auth(),
        )
        assert res.status_code == 200
        assert res.json()["status"] == "ok"
