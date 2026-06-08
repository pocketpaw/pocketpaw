"""Tests for the Google Antigravity backend.

Created: 2026-06-06 (feat/antigravity-backend). The ``google-antigravity`` SDK
is an optional dependency; these tests cover metadata, registry wiring, API-key
resolution, and the run-time guards that fire WITHOUT a live API call, so they
pass whether or not the SDK wheel is installed in the test env.

Updated: 2026-06-08 — added TestTurnLimit covering the antigravity_max_turns
wiring. The SDK exposes no native turn cap, so the backend bounds the agentic
loop with a pre_tool_call_decide hook that denies tool calls past the budget;
those tests need the SDK and importorskip when it is absent.
"""

import pytest

from pocketpaw.agents.antigravity import AntigravityBackend
from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings

# pytest is used by TestTurnLimit (importorskip); keep the import alive.
_ = pytest


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


class TestTurnLimit:
    """antigravity_max_turns is enforced via a pre_tool_call_decide hook.

    The SDK runs the agentic loop inside Agent.chat() with no native turn cap,
    so the backend counts tool calls (each drives another model step) and denies
    once the budget is spent.
    """

    def _backend(self, max_turns):
        backend = AntigravityBackend.__new__(AntigravityBackend)
        backend.settings = Settings(antigravity_api_key="k", antigravity_max_turns=max_turns)
        backend._turn_state = {"count": 0, "limit_hit": False}
        return backend

    def test_no_hook_when_unlimited(self):
        # 0 == unlimited: no turn-limit hook is registered.
        assert self._backend(0)._build_hooks() == []

    async def test_hook_allows_within_budget_then_denies(self):
        pytest.importorskip("google.antigravity")
        backend = self._backend(2)
        hooks = backend._build_hooks()
        assert len(hooks) == 1
        hook = hooks[0]

        # First two tool calls allowed; the third is denied once the cap is hit.
        r1 = await hook.run(context=None, data=object())
        r2 = await hook.run(context=None, data=object())
        r3 = await hook.run(context=None, data=object())
        assert r1.allow is True
        assert r2.allow is True
        assert r3.allow is False
        assert "2" in r3.message
        assert backend._turn_state["limit_hit"] is True

    def test_build_config_includes_turn_limit_hook(self):
        pytest.importorskip("google.antigravity")
        backend = self._backend(5)
        backend._sdk_available = True
        backend._custom_tools = []
        backend._policy = None  # _build_mcp_servers short-circuits without config
        config = backend._build_config("sys")
        assert len(config.hooks) >= 1

    async def test_run_surfaces_limit_notice(self):
        pytest.importorskip("google.antigravity")
        from unittest.mock import patch

        backend = self._backend(1)
        backend._sdk_available = True
        backend._stop_flag = False
        backend._custom_tools = []
        backend._policy = None

        class _Resp:
            usage_metadata = None

            def __aiter__(self):
                async def gen():
                    yield "done"

                return gen()

        class _FakeAgent:
            def __init__(self, config):
                pass

            async def __aenter__(self):
                # Simulate the hook having fired mid-run.
                backend._turn_state["limit_hit"] = True
                return self

            async def __aexit__(self, *a):
                return False

            async def chat(self, message):
                return _Resp()

        with patch("google.antigravity.Agent", _FakeAgent):
            events = [ev async for ev in backend.run("hi")]

        types = [e.type for e in events]
        assert "error" in types  # limit notice surfaced
        assert types[-1] == "done"
