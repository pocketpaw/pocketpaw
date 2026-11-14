# tests/ee/sites/test_prompt_enhance_agent_model.py — Enhance runs on the Sites
# agent's own backend and model, never on the gateway's first catalog model.
#
# Drives ``prompt_enhance.enhance_prompt`` through the real
# ``chat.side_completion.agent_complete`` with a fake agent pool, the gates
# patched open, and a gateway catalog whose first chat model is the one that
# timed out live (``Qwen3.8-27B``). The pool records what it was asked to run.

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pocketpaw_ee.catalog import service as catalog_service
from pocketpaw_ee.catalog.models import Modality, ModelCatalogEntry
from pocketpaw_ee.cloud.auth import guest_budget
from pocketpaw_ee.cloud.byok.service import TurnCredentials
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.cloud.credits import guards
from pocketpaw_ee.cloud.studio import prompt_soften
from pocketpaw_ee.sites import prompt_enhance

from pocketpaw.agents import pool as agent_pool
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.security.rate_limiter import RateLimiter

WS = "ws-enhance-agent"
USER = "user-enhance-agent"
AGENT = "agent-sites-default"
ORIGINAL = 'A cosy site for my bakery "Crumb & Co" in Leeds, open since 2009.'
BRIEF = (
    'A cosy one-page site for my bakery "Crumb & Co" in Leeds, open since 2009. '
    "Primary action: visit the shop. Placeholders: [your opening hours]."
)


class _ToolsBackend:
    async def run(self, message, *, tools_enabled=True, **_kw):  # pragma: no cover
        yield None


class _Pool:
    def __init__(self, backend: str, model: str):
        self.instance = SimpleNamespace(
            config={"backend": backend, "model": model}, backend=_ToolsBackend()
        )
        self.got: list[str] = []
        self.calls: list[dict] = []

    async def get(self, agent_id):
        self.got.append(agent_id)
        return self.instance

    def run(self, agent_id, message, session_key, **kwargs):
        self.calls.append({"agent_id": agent_id, **kwargs})

        async def _events():
            yield AgentEvent(type="message", content=json.dumps({"prompt": BRIEF}))
            yield AgentEvent(type="done", content="")

        return _events()


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(gateway_models=[], catalog_reads=0)
    monkeypatch.setattr(prompt_enhance, "_limiter", RateLimiter(rate=1.0, capacity=100))
    monkeypatch.setattr(
        prompt_enhance, "_transport", httpx.MockTransport(lambda r: httpx.Response(404))
    )

    async def _catalog():
        state.catalog_reads += 1
        return [
            ModelCatalogEntry(
                id="Qwen3.8-27B", display_name="Qwen", provider="qwen", modality=Modality.CHAT
            ),
            ModelCatalogEntry(
                id="claude-haiku-4-5",
                display_name="Haiku",
                provider="anthropic",
                modality=Modality.CHAT,
            ),
        ]

    async def _gateway(*, model, system, user):
        state.gateway_models.append(model)
        return json.dumps({"prompt": BRIEF})

    async def _default(_ws):
        return AGENT

    async def _platform(_ws):
        return TurnCredentials(source="platform")

    async def _source(*_a, **_kw):
        return "platform"

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(catalog_service, "list_models", _catalog)
    monkeypatch.setattr(prompt_soften, "_chat_completion", _gateway)
    monkeypatch.setattr(agent_service, "_get_default_workspace_agent_id", _default)
    monkeypatch.setattr(guards, "resolve_payer", _platform)
    monkeypatch.setattr(guards, "billing_source", _source)
    monkeypatch.setattr(guards, "assert_within_billing", _none)
    monkeypatch.setattr(guards, "free_model_refusal", _none)
    monkeypatch.setattr(guards, "free_run_bounds", _none)
    monkeypatch.setattr(guest_budget, "load_guest", _none)
    pool = _Pool("claude_agent_sdk", "claude-sonnet-4-6")
    monkeypatch.setattr(agent_pool, "get_agent_pool", lambda: pool)
    state.pool = pool
    return state


async def test_enhance_runs_the_requested_model_on_the_workspace_agent(world):
    result = await prompt_enhance.enhance_prompt(
        ORIGINAL, user_id=USER, workspace_id=WS, model="claude-haiku-4-5"
    )

    assert (result.reason, result.prompt) == ("enhanced", BRIEF)
    assert world.pool.got == [AGENT]
    (call,) = world.pool.calls
    assert call["model_override"] == "claude-haiku-4-5"
    assert call["tools_enabled"] is False
    assert world.gateway_models == []


async def test_enhance_without_a_model_never_takes_the_first_catalog_model(world):
    result = await prompt_enhance.enhance_prompt(ORIGINAL, user_id=USER, workspace_id=WS)

    assert result.reason == "enhanced"
    assert "Qwen3.8-27B" not in world.gateway_models
    assert world.catalog_reads == 0
    (call,) = world.pool.calls
    assert "model_override" not in call  # the agent's own model serves
