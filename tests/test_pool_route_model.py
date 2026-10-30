# tests/test_pool_route_model.py
# Created: 2026-06-26 (feat/mcg-3-pool-route-model) — regression coverage for the
# AgentPool model-routing seam (MCG-3). The old ``AgentPool._build`` mapped a
# per-agent model onto a Settings field with a brittle ``"claude"/"openai"/
# "google" in backend`` substring chain that SILENTLY DROPPED the per-agent
# model for codex_cli / opencode / deep_agents / copilot_sdk / langchain_react
# (and wrote OpenAI Agents' model to the wrong field). These tests pin:
#   1. ``route_model`` writes the model to the correct Settings field for EVERY
#      registered backend (including the langchain_react -> deep_agents_model
#      alias), preserves the composite provider:model / provider/model formats
#      verbatim, and no-ops on an empty model.
#   2. A thin ``_build`` integration check drives the real build path with a fake
#      backend class and asserts the per-agent model reaches the right Settings
#      field for the previously-dropped backends — i.e. it is NOT dropped.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.registry import list_backends
from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import (
    _BACKEND_MODEL_ATTR,
    _BACKEND_MODEL_ATTR_ALIASES,
    route_model,
)

# ---------------------------------------------------------------------------
# 1. route_model — the single source of truth for backend -> Settings field
# ---------------------------------------------------------------------------

# (backend name, model string, the Settings attr it must land in)
_ROUTING_CASES = [
    ("claude_agent_sdk", "claude-opus-4-8", "claude_sdk_model"),
    ("openai_agents", "gpt-5.2", "openai_agents_model"),
    ("google_adk", "gemini-3-pro-preview", "google_adk_model"),
    ("codex_cli", "gpt-5.3-codex", "codex_cli_model"),
    ("copilot_sdk", "gpt-5.2", "copilot_sdk_model"),
    ("opencode", "anthropic/claude-sonnet-4-6", "opencode_model"),
    ("deep_agents", "anthropic:claude-sonnet-4-6", "deep_agents_model"),
    # langchain_react has no field of its own — it reads deep_agents_model.
    ("langchain_react", "ollama:llama3.2", "deep_agents_model"),
]


@pytest.mark.parametrize(("backend", "model", "attr"), _ROUTING_CASES)
def test_route_model_writes_correct_field(backend: str, model: str, attr: str) -> None:
    settings = Settings.load()
    wrote = route_model(settings, backend, model)
    assert wrote is True
    # The model landed in the right field, byte-for-byte (composite formats kept).
    assert getattr(settings, attr) == model


def test_route_model_covers_every_registered_backend() -> None:
    """Every backend in the registry must route — otherwise its per-agent model
    is silently dropped (the exact bug MCG-3 fixes)."""
    for backend in list_backends():
        settings = Settings.load()
        assert route_model(settings, backend, "some-model:value") is True, (
            f"backend {backend!r} does not route a per-agent model -> it would be dropped"
        )


def test_route_model_previously_dropped_backends_not_silently_dropped() -> None:
    """Focused guard on the five backends the substring chain dropped."""
    dropped = {
        "codex_cli": "codex_cli_model",
        "opencode": "opencode_model",
        "deep_agents": "deep_agents_model",
        "copilot_sdk": "copilot_sdk_model",
        "langchain_react": "deep_agents_model",
    }
    for backend, attr in dropped.items():
        settings = Settings.load()
        # Field starts unset (or default) — prove the model actually arrives.
        route_model(settings, backend, "mymodel:tag")
        assert getattr(settings, attr) == "mymodel:tag", f"{backend!r} dropped its per-agent model"


def test_route_model_preserves_provider_model_for_deep_agents() -> None:
    """deep_agents / langchain_react take a single ``provider:model`` string in
    deep_agents_model — route_model must NOT split it into provider/model."""
    settings = Settings.load()
    route_model(settings, "deep_agents", "anthropic:claude-sonnet-4-6")
    assert settings.deep_agents_model == "anthropic:claude-sonnet-4-6"

    settings2 = Settings.load()
    route_model(settings2, "langchain_react", "ollama:llama3.2")
    # langchain_react inherits the deep_agents slot, single composite string.
    assert settings2.deep_agents_model == "ollama:llama3.2"


def test_route_model_empty_model_is_noop() -> None:
    """Empty model leaves the default untouched (fall-through to resolve_model)."""
    settings = Settings.load()
    default = settings.deep_agents_model  # "anthropic:claude-sonnet-4-6"
    assert route_model(settings, "deep_agents", "") is False
    assert settings.deep_agents_model == default

    # codex_cli keeps its own default too.
    codex_default = settings.codex_cli_model
    assert route_model(settings, "codex_cli", "") is False
    assert settings.codex_cli_model == codex_default


def test_route_model_unknown_backend_is_noop() -> None:
    settings = Settings.load()
    assert route_model(settings, "no_such_backend", "x") is False


def test_alias_map_targets_a_real_settings_field() -> None:
    """The langchain_react alias must point at a field that exists on Settings."""
    settings = Settings.load()
    for backend, attr in _BACKEND_MODEL_ATTR_ALIASES.items():
        assert hasattr(settings, attr), (
            f"alias for {backend!r} -> {attr!r} is not a real Settings field"
        )


def test_backend_model_attr_fields_exist_on_settings() -> None:
    """Sanity: every primary backend->field mapping names a real Settings field."""
    settings = Settings.load()
    for backend, attr in _BACKEND_MODEL_ATTR.items():
        assert hasattr(settings, attr), f"{backend!r} -> {attr!r} is not a real Settings field"


# ---------------------------------------------------------------------------
# 2. _build integration — the real pool path routes the model end-to-end
# ---------------------------------------------------------------------------


class _CapturingBackend:
    """Stand-in backend that records the Settings it was built with."""

    last_settings = None  # class attr — read after _build

    def __init__(self, settings, **_kwargs) -> None:
        type(self).last_settings = settings

    async def stop(self) -> None:  # pragma: no cover — teardown path
        pass


def _fake_agent_doc(backend: str, model: str):
    """A minimal Agent-doc stand-in for ``_build`` (soul disabled to skip I/O)."""
    cfg = {
        "backend": backend,
        "model": model,
        "soul_enabled": False,
        "tools": [],
    }
    return SimpleNamespace(
        id="64b7f0000000000000000001",
        name=f"agent-{backend}",
        slug=f"agent-{backend}",
        workspace="ws-test",
        updatedAt=datetime.now(UTC),
        config=SimpleNamespace(model_dump=lambda: dict(cfg)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "model", "attr"),
    [
        ("codex_cli", "gpt-5.3-codex", "codex_cli_model"),
        ("opencode", "anthropic/claude-sonnet-4-6", "opencode_model"),
        ("deep_agents", "anthropic:claude-sonnet-4-6", "deep_agents_model"),
        ("langchain_react", "ollama:llama3.2", "deep_agents_model"),
    ],
)
async def test_build_routes_model_to_backend_settings(
    monkeypatch, backend: str, model: str, attr: str
) -> None:
    """The real ``_build`` must land the per-agent model on the right Settings
    field for backends the old substring chain dropped."""
    from pocketpaw.agents import pool as pool_mod
    from pocketpaw.agents import registry as registry_mod

    _CapturingBackend.last_settings = None
    # ``_build`` imports ``get_backend_class`` locally from the registry module,
    # so patch it at its source. Stubs backend resolution so the test doesn't
    # import optional SDKs or hit a network.
    monkeypatch.setattr(registry_mod, "get_backend_class", lambda _name: _CapturingBackend)

    p = pool_mod.AgentPool()
    instance = await p._build(_fake_agent_doc(backend, model))

    settings = _CapturingBackend.last_settings
    assert settings is not None, "_build never instantiated the backend"
    assert settings.agent_backend == backend
    # The proof: the per-agent model reached the field the backend reads.
    assert getattr(settings, attr) == model
    # And it's the same Settings the instance carries downstream.
    assert instance.config["model"] == model
