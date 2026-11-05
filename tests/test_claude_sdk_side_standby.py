# tests/test_claude_sdk_side_standby.py — isolated, tool-less side completions
# (a site's AI API) on the Claude SDK backend run on pre-connected standby
# clients, one conversation per client.
#
# Production (2026-11-05): every site AI call spent 4-6 s before its first token
# because each one used a fresh ``side:<uuid>`` session key, so the backend
# connected a new CLI client per call (evicting the chat turn's warm client on
# the way). These tests pin: no connect on a warm call's critical path, the chat
# client left alone, no content crossing from one call to the next, and bounded,
# closable standbys.

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents import claude_sdk_standby
from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.claude_sdk_standby import SideStandbys

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"


def _settings():
    mock = MagicMock()
    for k, v in {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": "claude-haiku-5-5",
        "claude_sdk_max_turns": None,
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
        "claude_sdk_connect_timeout": 5.0,
    }.items():
        setattr(mock, k, v)
    return mock


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])


class _Result:
    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


class _World:
    """Every fake client the backend built, and whether a run was in flight when
    each one connected."""

    def __init__(self):
        self.clients: list[_Client] = []
        self.in_run = False
        self.connects_in_run = 0


class _Client:
    def __init__(self, world: _World, options=None, **_kw):
        self.world = world
        self.options = options
        self.queries: list[str] = []
        self.connected = False
        self.disconnected = False
        world.clients.append(self)

    async def connect(self, prompt=None):
        if self.world.in_run:
            self.world.connects_in_run += 1
        self.connected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt if isinstance(prompt, str) else repr(prompt))

    async def receive_messages(self):
        yield _Result()

    async def disconnect(self):
        self.connected = False
        self.disconnected = True

    async def interrupt(self):
        pass


@pytest.fixture(autouse=True)
def _fresh_global_count(monkeypatch):
    monkeypatch.setattr(SideStandbys, "_global_count", 0)


def _sdk(world: _World) -> ClaudeAgentSDK:
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(_settings())
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _Options
    sdk._ResultMessage = _Result
    sdk._ClaudeSDKClient = lambda **kw: _Client(world, **kw)
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    return sdk


async def _run(sdk, world: _World, message: str, *, system="SITE PROMPT", **kw) -> list:
    out = []
    llm = MagicMock()
    for flag in ("is_ollama", "is_openai_compatible", "is_gemini", "is_litellm", "is_openrouter"):
        setattr(llm, flag, False)
    llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
    kw.setdefault("isolated", True)
    kw.setdefault("tools_enabled", False)
    world.in_run = True
    try:
        with (
            patch(_LLM_CLIENT, return_value=llm),
            patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}),
        ):
            async for ev in sdk.run(message, system_prompt=system, **kw):
                out.append(ev)
    finally:
        world.in_run = False
    return out


async def _settle(sdk) -> None:
    standbys = getattr(sdk, "_side_standbys", None)
    if standbys is not None:
        await standbys.settle()


async def test_a_warm_side_call_connects_no_client_on_its_path() -> None:
    """Call 2 for the same site prompt must find a client already connected.

    FAILS before the fix: every side call connected its own CLI client inside the
    call (the 4-6 s visitors waited before the first token)."""
    world = _World()
    sdk = _sdk(world)

    await _run(sdk, world, "first visitor", session_key="side:one")
    await _settle(sdk)
    world.connects_in_run = 0

    await _run(sdk, world, "second visitor", session_key="side:two")

    assert world.connects_in_run == 0, "the second call spawned a CLI on its own path"


async def test_a_side_call_leaves_the_chat_warm_client_alone() -> None:
    """A site visitor's call must not evict the agent's chat warm client.

    FAILS before the fix: the side call's ``side:<uuid>`` key missed the chat
    client's key, so the backend disconnected it and connected its own."""
    world = _World()
    sdk = _sdk(world)
    chat = _Client(world)
    chat.connected = True
    sdk._client = chat
    sdk._client_options_key = "chat-key"
    sdk._client_session_key = "websocket:chat"

    await _run(sdk, world, "a visitor question", session_key="side:x")
    await _settle(sdk)

    assert sdk._client is chat
    assert not chat.disconnected
    assert chat.queries == []


async def test_no_content_crosses_from_one_call_to_the_next() -> None:
    """Each call is a fresh conversation: call 1's text never reaches the client
    that serves call 2, and the client that served call 1 is disconnected."""
    world = _World()
    sdk = _sdk(world)

    await _run(sdk, world, "SECRET-FROM-VISITOR-ONE", session_key="side:one")
    await _settle(sdk)
    await _run(sdk, world, "visitor two asks", session_key="side:two")
    await _settle(sdk)

    served = [c for c in world.clients if c.queries]
    assert len(served) == 2, "each call must be served by its own client"
    first, second = served
    assert first is not second
    assert all("SECRET-FROM-VISITOR-ONE" not in q for q in second.queries)
    assert second.queries == ["visitor two asks"]
    assert first.disconnected, "a client that served a conversation must not be kept"


async def test_a_different_site_prompt_never_takes_another_sites_standby() -> None:
    world = _World()
    sdk = _sdk(world)

    await _run(sdk, world, "hello", system="SITE A", session_key="side:a")
    await _settle(sdk)
    await _run(sdk, world, "hello", system="SITE B", session_key="side:b")
    await _settle(sdk)

    served = [c for c in world.clients if c.queries]
    assert [c.options.system_prompt for c in served] == ["SITE A", "SITE B"]


async def test_tools_on_or_not_isolated_runs_keep_the_chat_path() -> None:
    """Only isolated, tool-less calls use standbys."""
    world = _World()
    sdk = _sdk(world)

    await _run(sdk, world, "chat turn", session_key="s1", isolated=False, tools_enabled=True)
    await _settle(sdk)

    assert sdk._client is not None and sdk._client.queries == ["chat turn"]
    standbys = getattr(sdk, "_side_standbys", None)
    assert standbys is None or standbys.ready_count() == 0


async def test_cleanup_disconnects_standbys() -> None:
    world = _World()
    sdk = _sdk(world)

    await _run(sdk, world, "hi", session_key="side:1")
    await _settle(sdk)
    standby = [c for c in world.clients if c.connected and not c.queries]
    assert standby, "a standby should be waiting after the first call"

    await sdk.cleanup()
    await _settle(sdk)

    assert all(c.disconnected for c in standby)


async def test_standbys_are_bounded() -> None:
    pool = SideStandbys(per_key=1, max_keys=2, idle_s=60)
    made: list[_Client] = []
    world = _World()

    async def connect():
        c = _Client(world)
        made.append(c)
        return c

    for key in ("k1", "k2", "k3"):
        pool.refill(key, connect)
        pool.refill(key, connect)  # already filling / full: no second client
        await pool.settle()

    assert pool.ready_count() == 2
    assert pool.ready_count("k1") == 0  # least recently used key evicted
    assert sum(not c.disconnected for c in made) == 2


async def test_the_process_wide_cap_holds(monkeypatch) -> None:
    monkeypatch.setattr(claude_sdk_standby, "GLOBAL_MAX", 1)
    world = _World()

    async def connect():
        return _Client(world)

    a, b = SideStandbys(), SideStandbys()
    a.refill("k", connect)
    await a.settle()
    b.refill("k", connect)
    await b.settle()

    assert a.ready_count() + b.ready_count() == 1


async def test_a_stale_standby_is_not_used() -> None:
    pool = SideStandbys(idle_s=0.0)
    world = _World()

    async def connect():
        return _Client(world)

    pool.refill("k", connect)
    await pool.settle()
    assert pool.take("k") is None
    await pool.settle()
    assert world.clients[0].disconnected


async def test_a_refill_finishing_after_close_is_discarded() -> None:
    pool = SideStandbys()
    world = _World()
    import asyncio

    gate = asyncio.Event()

    async def connect():
        await gate.wait()
        return _Client(world)

    pool.refill("k", connect)
    await pool.close()
    gate.set()
    await pool.settle()

    assert pool.ready_count() == 0
    assert world.clients[0].disconnected
