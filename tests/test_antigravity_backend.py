"""Tests for the Google Antigravity backend.

Created: 2026-06-06 (feat/antigravity-backend). The ``google-antigravity`` SDK
is an optional dependency; these tests cover metadata, registry wiring, API-key
resolution, and the run-time guards that fire WITHOUT a live API call, so they
pass whether or not the SDK wheel is installed in the test env.
"""

from pocketpaw.agents.antigravity import AntigravityBackend
from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


class TestAntigravityInfo:
    def test_info_metadata(self):
        info = AntigravityBackend.info()
        assert info.name == "antigravity"
        assert info.display_name == "Google Antigravity"
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MCP in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities

    def test_install_hint(self):
        info = AntigravityBackend.info()
        assert info.install_hint["pip_package"] == "google-antigravity"
        assert info.install_hint["pip_spec"] == "pocketpaw[antigravity]"
        assert info.install_hint["verify_import"] == "google.antigravity"

    def test_required_keys_and_providers(self):
        info = AntigravityBackend.info()
        assert "gemini_api_key" in info.required_keys
        assert "google" in info.supported_providers
        assert info.beta is True

    def test_tool_policy_map(self):
        info = AntigravityBackend.info()
        assert info.tool_policy_map["run_command"] == "shell"
        assert info.tool_policy_map["edit_file"] == "filesystem"


class TestRegistryWiring:
    def test_listed_in_registry(self):
        from pocketpaw.agents.registry import list_backends

        assert "antigravity" in list_backends()

    def test_get_backend_class(self):
        from pocketpaw.agents.registry import get_backend_class

        cls = get_backend_class("antigravity")
        assert cls is not None
        assert cls.__name__ == "AntigravityBackend"

    def test_get_backend_info(self):
        from pocketpaw.agents.registry import get_backend_info

        info = get_backend_info("antigravity")
        assert info is not None
        assert info.name == "antigravity"


class TestApiKeyResolution:
    def _backend(self, **kwargs):
        backend = AntigravityBackend.__new__(AntigravityBackend)
        backend.settings = Settings(**kwargs)
        return backend

    def test_prefers_dedicated_key(self):
        backend = self._backend(
            antigravity_api_key="antg", gemini_api_key="gem", google_api_key="goog"
        )
        assert backend._resolve_api_key() == "antg"

    def test_falls_back_to_gemini(self):
        backend = self._backend(gemini_api_key="gem", google_api_key="goog")
        assert backend._resolve_api_key() == "gem"

    def test_falls_back_to_google(self):
        backend = self._backend(google_api_key="goog")
        assert backend._resolve_api_key() == "goog"

    def test_none_when_unset(self):
        backend = self._backend()
        assert backend._resolve_api_key() is None


class TestRunGuards:
    async def test_run_without_sdk_yields_install_error(self):
        backend = AntigravityBackend.__new__(AntigravityBackend)
        backend.settings = Settings(antigravity_api_key="k")
        backend._sdk_available = False
        backend._stop_flag = False

        events = [ev async for ev in backend.run("hi")]
        assert len(events) == 1
        assert events[0].type == "error"
        assert "not installed" in events[0].content

    async def test_run_without_key_yields_key_error(self):
        backend = AntigravityBackend.__new__(AntigravityBackend)
        backend.settings = Settings()  # no keys
        backend._sdk_available = True
        backend._stop_flag = False

        events = [ev async for ev in backend.run("hi")]
        assert len(events) == 1
        assert events[0].type == "error"
        assert "API key" in events[0].content


class TestStatus:
    async def test_get_status_shape(self):
        backend = AntigravityBackend.__new__(AntigravityBackend)
        backend.settings = Settings()
        backend._sdk_available = False
        backend._stop_flag = False

        status = await backend.get_status()
        assert status["backend"] == "antigravity"
        assert status["available"] is False
        assert "model" in status
