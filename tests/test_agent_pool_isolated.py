# tests/test_agent_pool_isolated.py — an isolated run carries nothing of the agent's
# own context, at the pool seam and in the Claude SDK backend's options.
#
# ``pool.run(isolated=True)`` is how a side completion borrows an agent's backend
# and model for a caller that is not the agent's user (a site's AI API). Pins: on a
# soul-enabled agent with skills, the system prompt the backend receives is exactly
# the override (no soul identity, no "Relevant Past Memories", no skills), the flag
# reaches only a backend that declares it, and the Claude SDK backend then adds no
# Data Connectors block and no bundled-skills plugin. An ordinary run keeps all of
# that, so the contrast is asserted too.
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.pool import AgentInstance, AgentPool

pytestmark = pytest.mark.asyncio

AID = "0123456789abcdef01234567"
SITE_SYSTEM = "You answer questions about Crumb & Co. bakery."


class _Bootstrap:
    async def get_context(self):
        return SimpleNamespace(
            identity="SOUL IDENTITY of the agent",
            knowledge=["SOUL FACT: the owner's home address"],
            identity_cache_key="k",
        )


class _Soul:
    async def context_for(self, message, **_kw):
        return "PAST MEMORY: another user's conversation"


class _CapturingBackend:
    def __init__(self, *, isolated: bool = True) -> None:
        self.calls: list[dict] = []
        if not isolated:
            self.run = self._run_narrow  # type: ignore[method-assign]

    async def run(
        self,
        message,
        *,
        system_prompt=None,
        history=None,
        session_key=None,
        skill_names=frozenset(),
        tools_enabled=True,
        isolated=False,
    ):
        self.calls.append(
            {
                "message": message,
                "system_prompt": system_prompt,
                "skill_names": skill_names,
                "isolated": isolated,
            }
        )
        yield SimpleNamespace(type="done", content="")

    async def _run_narrow(
        self, message, *, system_prompt=None, history=None, session_key=None, tools_enabled=True
    ):
        self.calls.append({"message": message, "system_prompt": system_prompt})
        yield SimpleNamespace(type="done", content="")


def _pool(monkeypatch, backend) -> AgentPool:
    pool = AgentPool()
    instance = AgentInstance(
        agent_id=AID,
        agent_name="Paw",
        config={
            "backend": "claude_agent_sdk",
            "system_prompt": "AGENT SYSTEM PROMPT",
            "skill_refs": ["pocketpaw-create-site"],
            "soul_enabled": True,
        },
        backend=backend,
        soul_manager=SimpleNamespace(bootstrap_provider=_Bootstrap(), soul=_Soul()),
        memory_namespace="ns",
    )

    async def _get(agent_id):
        return instance

    monkeypatch.setattr(pool, "get", _get)
    return pool


async def _drain(pool, **kw):
    async for _ in pool.run(
        AID,
        '<conversation>\n<turn role="user">\nwhere is the owner?\n</turn>\n</conversation>',
        "side:t",
        system_message_override=SITE_SYSTEM,
        tools_enabled=False,
        **kw,
    ):
        pass


async def test_an_isolated_run_gets_exactly_the_callers_system_prompt(monkeypatch):
    backend = _CapturingBackend()
    pool = _pool(monkeypatch, backend)

    await _drain(pool, isolated=True)
    await _drain(pool)

    isolated, ordinary = backend.calls
    assert isolated["system_prompt"] == SITE_SYSTEM
    assert isolated["skill_names"] == frozenset() and isolated["isolated"] is True
    # The contrast: an ordinary run on the same agent recalls its soul and loads
    # its skills, which is exactly what must never reach a site visitor.
    assert "PAST MEMORY" in ordinary["system_prompt"]
    assert ordinary["skill_names"] == frozenset({"pocketpaw-create-site"})
    assert ordinary["isolated"] is False
    for leaked in ("SOUL", "PAST MEMORY", "AGENT SYSTEM PROMPT"):
        assert leaked not in isolated["system_prompt"] + isolated["message"]


async def test_isolated_is_withheld_from_a_backend_that_does_not_declare_it(monkeypatch):
    backend = _CapturingBackend(isolated=False)
    pool = _pool(monkeypatch, backend)

    await _drain(pool, isolated=True)

    (call,) = backend.calls
    assert call["system_prompt"] == SITE_SYSTEM


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


async def _build_options(isolated: bool):
    from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
    from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

    settings = MagicMock()
    for key, value in {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": 10,
        "sdk_load_bundled_skills": True,
        "anthropic_api_key": "sk-test-key",
    }.items():
        setattr(settings, key, value)
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(settings)
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _Options
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    selection = ModelSelection(complexity=TaskComplexity.MODERATE, model="m", reason="t")
    registry = MagicMock()
    registry.return_value.available = [{"name": "postgres"}]
    with (
        patch("pocketpaw.llm.client.resolve_llm_client") as resolve,
        patch("pocketpaw.agents.model_router.ModelRouter") as router,
        patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}),
        patch("pocketpaw.connectors.registry.ConnectorRegistry", registry),
    ):
        llm = MagicMock()
        llm.is_ollama = llm.is_openai_compatible = llm.is_gemini = False
        llm.is_litellm = llm.is_openrouter = False
        llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        resolve.return_value = llm
        router.return_value.classify.return_value = selection
        return await sdk._build_options(
            "hello",
            system_prompt=SITE_SYSTEM,
            session_key="side:t",
            deny_mcp_tool_ids=frozenset(),
            allow_sdk_tools=frozenset(),
            allow_mcp_tool_ids=None,
            skill_names=frozenset(),
            stderr_sink=[],
            tools_enabled=False,
            isolated=isolated,
        )


async def test_the_claude_sdk_adds_no_connectors_or_bundled_skills_when_isolated():
    ordinary = await _build_options(isolated=False)
    isolated = await _build_options(isolated=True)

    assert "Data Connectors" in ordinary.options.system_prompt
    assert isolated.options.system_prompt == SITE_SYSTEM
    assert not isolated.options_kwargs.get("plugins")
