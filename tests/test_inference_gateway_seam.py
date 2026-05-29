# Tests for the RFC 11 inference-gateway integration seam.
# Created: 2026-05-29 — TDD coverage for the OSS-core additive seam that lets a
#   closed pocketpaw_igw package override Settings per run and meter token usage:
#     - InferenceGatewayProvider Protocol + registry discovery (pocketpaw.inference_gateway)
#     - AgentPool.run(settings_override=...) threading down to the backend
#     - ClaudeSDKBackend.run(settings_override=...) building a per-call effective
#       Settings via model_copy while leaving self.settings untouched.
#
# These tests use fakes/stubs; no `claude` subprocess is spawned.

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Protocol + registry
# ---------------------------------------------------------------------------


class _FakeIGWProvider:
    """Minimal object satisfying the InferenceGatewayProvider Protocol."""

    def settings_override(self, ctx: Any) -> dict[str, Any]:
        return {
            "claude_sdk_model": "claude-haiku-test",
            "claude_sdk_provider": "openai_compatible",
            "smart_routing_enabled": False,
        }

    def record_usage(
        self,
        ctx: Any,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        return None


def test_inference_gateway_protocol_is_runtime_checkable():
    from pocketpaw.extensions import InferenceGatewayProvider

    fake = _FakeIGWProvider()
    assert isinstance(fake, InferenceGatewayProvider)


def test_inference_gateway_protocol_rejects_non_conforming_object():
    from pocketpaw.extensions import InferenceGatewayProvider

    class _Missing:
        # has settings_override but not record_usage
        def settings_override(self, ctx: Any) -> dict[str, Any]:
            return {}

    assert not isinstance(_Missing(), InferenceGatewayProvider)


def test_registry_returns_none_when_no_gateway_registered():
    from pocketpaw import _registry

    _registry.clear_cache()
    assert _registry.first("pocketpaw.inference_gateway") is None


def test_registry_discovers_registered_gateway(monkeypatch):
    """When an entry point is registered under pocketpaw.inference_gateway,
    `first` returns the instantiated provider."""
    from importlib.metadata import EntryPoint

    from pocketpaw import _registry

    # Stash the fake provider class somewhere importable by EntryPoint.load().
    mod = types.ModuleType("_igw_fake_mod")
    mod._FakeIGWProvider = _FakeIGWProvider  # type: ignore[attr-defined]
    sys.modules["_igw_fake_mod"] = mod

    ep = EntryPoint(
        name="inference_gateway",
        value="_igw_fake_mod:_FakeIGWProvider",
        group="pocketpaw.inference_gateway",
    )

    def _fake_entry_points(*, group: str):
        return [ep] if group == "pocketpaw.inference_gateway" else []

    monkeypatch.setattr("pocketpaw._registry.entry_points", _fake_entry_points)
    _registry.clear_cache()
    try:
        provider = _registry.first("pocketpaw.inference_gateway")
        assert provider is not None
        assert isinstance(provider, _FakeIGWProvider)
    finally:
        _registry.clear_cache()
        sys.modules.pop("_igw_fake_mod", None)


# ---------------------------------------------------------------------------
# AgentPool.run(settings_override=...)
# ---------------------------------------------------------------------------


class _StubBackend:
    """Captures the settings the pool hands it via a per-call effective clone.

    The pool does NOT pass settings_override into the backend constructor; it
    passes it as a kwarg to backend.run(). This stub records what it received.
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.received_override: dict[str, Any] | None = None

    async def run(
        self, message, *, system_prompt=None, history=None, session_key=None, settings_override=None
    ):
        self.received_override = settings_override
        # Compute the same effective the real backend would, so the test can
        # assert the override actually shapes the run.
        effective = (
            self.settings.model_copy(update=settings_override)
            if settings_override
            else self.settings
        )
        from pocketpaw.agents.backend import AgentEvent

        yield AgentEvent(
            type="effective_probe",
            content="",
            metadata={
                "claude_sdk_model": effective.claude_sdk_model,
                "claude_sdk_provider": effective.claude_sdk_provider,
                "smart_routing_enabled": effective.smart_routing_enabled,
            },
        )
        yield AgentEvent(type="done", content="")


@pytest.fixture
def _pool_with_stub(monkeypatch):
    """Build an AgentPool whose `get` returns an instance backed by _StubBackend."""
    from pocketpaw.agents.pool import AgentInstance, AgentPool
    from pocketpaw.config import Settings

    pool = AgentPool()
    settings = Settings()
    settings.claude_sdk_model = "claude-default"
    settings.claude_sdk_provider = "anthropic"
    settings.smart_routing_enabled = True

    backend = _StubBackend(settings)
    instance = AgentInstance(
        agent_id="agent-1",
        agent_name="Agent One",
        config={},
        backend=backend,
        soul_manager=None,
        memory_namespace="agent-1",
    )

    async def _fake_get(agent_id: str):
        return instance

    monkeypatch.setattr(pool, "get", _fake_get)
    return pool, backend


async def test_pool_run_threads_settings_override_to_backend(_pool_with_stub):
    pool, backend = _pool_with_stub
    override = {
        "claude_sdk_model": "claude-haiku-test",
        "claude_sdk_provider": "openai_compatible",
        "smart_routing_enabled": False,
    }

    events = [
        ev async for ev in pool.run("agent-1", "hello", "session-1", settings_override=override)
    ]

    assert backend.received_override == override
    probe = next(e for e in events if e.type == "effective_probe")
    assert probe.metadata["claude_sdk_model"] == "claude-haiku-test"
    assert probe.metadata["claude_sdk_provider"] == "openai_compatible"
    assert probe.metadata["smart_routing_enabled"] is False
    # The instance's own settings must be untouched (per-call only).
    assert backend.settings.claude_sdk_model == "claude-default"
    assert backend.settings.smart_routing_enabled is True


async def test_pool_run_without_override_passes_none(_pool_with_stub):
    pool, backend = _pool_with_stub
    _events = [ev async for ev in pool.run("agent-1", "hi", "session-1")]
    assert backend.received_override is None


# ---------------------------------------------------------------------------
# ClaudeSDKBackend.run(settings_override=...)
# ---------------------------------------------------------------------------


def _make_backend_settings() -> Any:
    from pocketpaw.config import Settings

    s = Settings()
    s.claude_sdk_model = "claude-sonnet-default"
    s.claude_sdk_provider = "anthropic"
    s.smart_routing_enabled = True
    return s


def _spy_llm():
    """A MagicMock standing in for the resolved LLM client.

    All provider-kind flags are False so `is_non_anthropic` stays False and
    `run()` follows the Anthropic path. `to_sdk_env` returns an empty dict so
    no real env is built. The run still fails downstream when the (absent) SDK
    options class is constructed — that failure is caught inside run() and
    surfaced as an `error` event, which is fine: by then `effective` has
    already been captured by the resolve spy.
    """
    llm = MagicMock()
    llm.is_ollama = False
    llm.is_openai_compatible = False
    llm.is_gemini = False
    llm.is_litellm = False
    llm.is_openrouter = False
    llm.api_key = ""
    llm.to_sdk_env.return_value = {}
    llm.format_api_error.return_value = "stubbed error"
    return llm


async def test_claude_sdk_run_builds_effective_clone_and_leaves_self_unchanged(monkeypatch):
    """run(settings_override=...) must build a model_copy and read provider
    resolution off the clone, never mutating self.settings."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    backend = ClaudeSDKBackend(_make_backend_settings())
    # Get past the early availability guards without spawning anything.
    backend._sdk_available = True
    backend._cli_available = True

    captured: dict[str, Any] = {}

    def _spy_resolve(settings, *, force_provider=None):
        captured["settings"] = settings
        captured["force_provider"] = force_provider
        return _spy_llm()

    monkeypatch.setattr("pocketpaw.llm.client.resolve_llm_client", _spy_resolve)

    override = {
        "claude_sdk_model": "claude-haiku-test",
        "claude_sdk_provider": "openai_compatible",
        "smart_routing_enabled": False,
    }

    # Drain the stream — run() ultimately fails building the (unloaded) SDK
    # options, which is caught and surfaced as an error event, so iteration
    # completes without raising.
    _events = [
        ev
        async for ev in backend.run(
            "hello",
            system_prompt=None,
            history=None,
            session_key="s1",
            settings_override=override,
        )
    ]

    eff = captured["settings"]
    assert eff.claude_sdk_model == "claude-haiku-test"
    assert eff.claude_sdk_provider == "openai_compatible"
    assert eff.smart_routing_enabled is False
    # force_provider must come off the effective clone, not self.settings.
    assert captured["force_provider"] == "openai_compatible"
    # effective is a distinct object from self.settings (a model_copy).
    assert eff is not backend.settings

    # self.settings is unchanged: per-call override only.
    assert backend.settings.claude_sdk_model == "claude-sonnet-default"
    assert backend.settings.claude_sdk_provider == "anthropic"
    assert backend.settings.smart_routing_enabled is True


async def test_claude_sdk_run_without_override_uses_self_settings(monkeypatch):
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    backend = ClaudeSDKBackend(_make_backend_settings())
    backend._sdk_available = True
    backend._cli_available = True

    captured: dict[str, Any] = {}

    def _spy_resolve(settings, *, force_provider=None):
        captured["settings"] = settings
        return _spy_llm()

    monkeypatch.setattr("pocketpaw.llm.client.resolve_llm_client", _spy_resolve)

    _events = [
        ev async for ev in backend.run("hi", system_prompt=None, history=None, session_key="s1")
    ]

    # With no override, effective IS self.settings (same object).
    assert captured["settings"] is backend.settings
