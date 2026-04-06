"""Tests for the backend registry — lazy import, fallback, legacy mapping."""

from unittest.mock import patch

from pocketpaw.agents.registry import (
    _LEGACY_BACKENDS,
    get_backend_class,
    get_backend_info,
    list_backends,
    register_backend,
)


class TestListBackends:
    def test_returns_all_registered(self):
        backends = list_backends()
        assert "claude_agent_sdk" in backends
        assert "openai_agents" in backends
        assert "google_adk" in backends
        assert "opencode" in backends

    def test_does_not_include_legacy(self):
        backends = list_backends()
        assert "pocketpaw_native" not in backends
        assert "open_interpreter" not in backends
        assert "claude_code" not in backends
        assert "gemini_cli" not in backends


class TestGetBackendClass:
    def test_claude_agent_sdk_loads(self):
        cls = get_backend_class("claude_agent_sdk")
        assert cls is not None
        assert cls.__name__ == "ClaudeSDKBackend"

    def test_unknown_returns_none(self):
        assert get_backend_class("nonexistent_xyz") is None

    def test_legacy_falls_back(self):
        """Legacy backend names should resolve to their fallback."""
        for legacy_name in _LEGACY_BACKENDS:
            cls = get_backend_class(legacy_name)
            assert cls is not None, f"Legacy '{legacy_name}' failed to fall back"

    def test_gemini_cli_legacy_falls_back_to_google_adk(self):
        """gemini_cli legacy name should resolve to GoogleADKBackend."""
        cls = get_backend_class("gemini_cli")
        assert cls is not None
        assert cls.__name__ == "GoogleADKBackend"

    def test_missing_dep_returns_none(self):
        """If import fails, returns None instead of raising."""
        with patch(
            "pocketpaw.agents.registry._BACKEND_REGISTRY",
            {"broken": ("nonexistent.module.xyz", "SomeClass")},
        ):
            assert get_backend_class("broken") is None


class TestGetBackendInfo:
    def test_claude_sdk_info(self):
        info = get_backend_info("claude_agent_sdk")
        assert info is not None
        assert info.name == "claude_agent_sdk"
        assert info.display_name == "Claude Agent SDK"
        assert len(info.builtin_tools) > 0

    def test_claude_sdk_required_keys(self):
        info = get_backend_info("claude_agent_sdk")
        assert info is not None
        assert "anthropic_api_key" in info.required_keys
        assert "anthropic" in info.supported_providers
        assert "ollama" in info.supported_providers
        assert "openai_compatible" in info.supported_providers

    def test_openai_agents_required_keys(self):
        info = get_backend_info("openai_agents")
        assert info is not None
        assert "openai_api_key" in info.required_keys
        assert "openai" in info.supported_providers
        assert "ollama" in info.supported_providers

    def test_google_adk_required_keys(self):
        info = get_backend_info("google_adk")
        assert info is not None
        assert info.name == "google_adk"
        assert info.display_name == "Google ADK"
        assert "google_api_key" in info.required_keys
        assert "google" in info.supported_providers

    def test_gemini_cli_legacy_info(self):
        """gemini_cli legacy name should resolve to google_adk info."""
        info = get_backend_info("gemini_cli")
        assert info is not None
        assert info.name == "google_adk"

    def test_opencode_no_keys(self):
        info = get_backend_info("opencode")
        assert info is not None
        assert info.required_keys == []
        assert info.supported_providers == []

    def test_unknown_returns_none(self):
        assert get_backend_info("nonexistent_xyz") is None


class TestRegisterBackend:
    def test_plugin_registration(self):
        register_backend("test_plugin", "pocketpaw.agents.claude_sdk", "ClaudeSDKBackend")
        cls = get_backend_class("test_plugin")
        assert cls is not None
        # Clean up
        from pocketpaw.agents.registry import _BACKEND_REGISTRY

        _BACKEND_REGISTRY.pop("test_plugin", None)
