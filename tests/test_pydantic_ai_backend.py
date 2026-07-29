"""Tests for the Pydantic AI agent backend.

Created 2026-07-29 (feat/pydantic-ai-backend).

The load-bearing test in this file is
``test_a_new_run_does_not_resurrect_a_stopped_one``. ``AgentPool`` caches ONE
backend instance per agent and drives concurrent runs through it, so any
cancellation state kept on the instance truncates siblings. That is not
hypothetical: the 2026-07-29 load-test rig saw 33 of 49 concurrent runs on
``deep_agents`` return a clean ``stream_end`` carrying no content.

It is that test and not the obvious one. ``test_concurrent_runs_each_produce_content``
looks like the guard but a mutation probe showed it PASSING against a faithful
shared-instance-flag reproduction — no ``stop()`` lands between those runs, so
nothing distinguishes the two shapes. Only the ordering test does, and it was
written after the probe, not before.

Everything here runs against pydantic-ai's ``TestModel`` / ``FunctionModel``, so
no provider key and no network are required.
"""

from __future__ import annotations

import asyncio

import pytest

from pocketpaw.agents.pydantic_ai import PydanticAIBackend, _RunHandle
from pocketpaw.config import Settings

pytest.importorskip("pydantic_ai", reason="pocketpaw[pydantic-ai] not installed")

from pydantic_ai.messages import ModelMessage  # noqa: E402
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402


def _settings(**overrides) -> Settings:
    base = {
        "pydantic_ai_model": "litellm:test-model",
        "litellm_api_base": "http://localhost:4000",
        "litellm_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(**base)


def _backend_with_model(model, **overrides) -> PydanticAIBackend:
    """Build a backend whose ``_build_model`` returns *model*.

    Patching the model factory (not the Agent) keeps every other layer real:
    tool bridging, event mapping, history conversion and the run lifecycle all
    execute exactly as they do in production.

    Both tool caches are pinned EMPTY, and both matter:

    * ``_mcp_tools`` — otherwise a test reads the developer's real
      ``load_mcp_config()`` and spawns their stdio servers (the suite hung on a
      live ``pocketpaw-discord`` server).
    * ``_custom_tools`` — ``TestModel`` calls every tool it is handed, so the
      real 49-tool builtin surface gets EXECUTED: ~58s per run, 1479 events, and
      ``code_mode`` trying to bind a unix socket on Windows.

    Tests that exercise tools install their own; ``test_bridged_tools_are_all_async``
    covers the real bridge separately without running a model.
    """
    backend = PydanticAIBackend(_settings(**overrides))
    backend._build_model = lambda: model  # type: ignore[method-assign]
    backend._mcp_tools = []
    backend._custom_tools = []
    return backend


async def _collect(backend: PydanticAIBackend, message: str, **kwargs) -> list:
    return [ev async for ev in backend.run(message, **kwargs)]


# --------------------------------------------------------------------------
# registry + protocol conformance
# --------------------------------------------------------------------------


def test_backend_is_registered():
    from pocketpaw.agents.registry import get_backend_class, list_backends

    assert "pydantic_ai" in list_backends()
    assert get_backend_class("pydantic_ai") is PydanticAIBackend


def test_satisfies_agent_backend_protocol():
    from pocketpaw.agents.backend import AgentBackend

    assert isinstance(PydanticAIBackend(_settings()), AgentBackend)


def test_model_routing_entry_exists():
    """``route_model`` must know this backend's settings field.

    A backend missing from ``_BACKEND_MODEL_ATTR`` makes ``route_model`` a
    silent no-op: the per-agent ``config.model`` is dropped and every agent
    quietly runs the global default (the MCG-3 bug).
    """
    from pocketpaw.llm.providers.base import _BACKEND_MODEL_ATTR, resolve_model, route_model

    assert _BACKEND_MODEL_ATTR["pydantic_ai"] == "pydantic_ai_model"

    settings = _settings()
    assert route_model(settings, "pydantic_ai", "anthropic:claude-haiku-4-5-20251001") is True
    assert settings.pydantic_ai_model == "anthropic:claude-haiku-4-5-20251001"
    assert resolve_model(settings, "pydantic_ai", "anthropic") == (
        "anthropic:claude-haiku-4-5-20251001"
    )


# --------------------------------------------------------------------------
# model / provider resolution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_setting", "provider_setting", "expected"),
    [
        ("litellm:claude-sonnet-4-6", "auto", ("litellm", "claude-sonnet-4-6")),
        ("anthropic:claude-haiku-4-5", "auto", ("anthropic", "claude-haiku-4-5")),
        ("gpt-5.2", "openai", ("openai", "gpt-5.2")),
        ("some-model", "auto", ("litellm", "some-model")),
    ],
)
def test_parse_provider_model(model_setting, provider_setting, expected):
    backend = PydanticAIBackend(
        _settings(pydantic_ai_model=model_setting, pydantic_ai_provider=provider_setting)
    )
    assert backend._parse_provider_model() == expected


def test_litellm_base_url_gets_v1_suffix():
    """The OpenAI client appends only ``/chat/completions``.

    ``deep_agents`` passes ``litellm_api_base`` WITHOUT ``/v1`` because the
    LiteLLM SDK appends the path itself. One setting, two contracts — without
    the suffix here every request 404s.
    """
    backend = PydanticAIBackend(_settings(litellm_api_base="http://proxy:4000"))
    base, key, _ = backend._resolve_openai_compatible("litellm", "m")
    assert base == "http://proxy:4000/v1"
    assert key == "sk-test"


def test_litellm_base_url_not_double_suffixed():
    backend = PydanticAIBackend(_settings(litellm_api_base="http://proxy:4000/v1"))
    base, _, _ = backend._resolve_openai_compatible("litellm", "m")
    assert base == "http://proxy:4000/v1"


def test_unsupported_provider_raises():
    backend = PydanticAIBackend(_settings(pydantic_ai_model="wat:some-model"))
    with pytest.raises(ValueError, match="unsupported provider"):
        backend._build_model()


# --------------------------------------------------------------------------
# streaming + event mapping
# --------------------------------------------------------------------------


async def test_run_streams_chunks_then_done():
    backend = _backend_with_model(TestModel(custom_output_text="hello world"))
    events = await _collect(backend, "hi")

    assert [e.type for e in events][-1] == "done"
    text = "".join(e.content for e in events if e.type == "message")
    assert text == "hello world"


async def test_run_emits_tool_use_and_tool_result_in_order():
    async def stream_fn(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            yield {
                0: DeltaToolCall(name="echo_tool", json_args='{"value": "x"}', tool_call_id="c1")
            }
        else:
            yield "done"

    backend = _backend_with_model(FunctionModel(stream_function=stream_fn))

    from pydantic_ai.tools import Tool

    async def echo_tool(value: str) -> str:
        """Echo a value."""
        return f"echoed {value}"

    backend._custom_tools = [Tool(echo_tool, name="echo_tool", description="Echo a value.")]

    events = await _collect(backend, "call the tool")
    kinds = [e.type for e in events]

    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert kinds.index("tool_use") < kinds.index("tool_result")

    use = next(e for e in events if e.type == "tool_use")
    assert use.metadata["name"] == "echo_tool"
    result = next(e for e in events if e.type == "tool_result")
    assert "echoed x" in result.content


async def test_tool_use_announced_once_per_call_id():
    """The early PartStartEvent signal and the authoritative
    FunctionToolCallEvent describe the SAME call — the UI must see one."""

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            yield {0: DeltaToolCall(name="ping", json_args="{}", tool_call_id="dup-1")}
        else:
            yield "ok"

    backend = _backend_with_model(FunctionModel(stream_function=stream_fn))

    from pydantic_ai.tools import Tool

    async def ping() -> str:
        """Ping."""
        return "pong"

    backend._custom_tools = [Tool(ping, name="ping", description="Ping.")]

    events = await _collect(backend, "ping it")
    assert sum(1 for e in events if e.type == "tool_use") == 1


async def test_history_is_threaded_into_the_model():
    seen: dict = {}

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo):
        seen["count"] = len(messages)
        yield "ok"

    backend = _backend_with_model(FunctionModel(stream_function=stream_fn))
    await _collect(
        backend,
        "third",
        history=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ],
    )
    # two history messages + the current user turn
    assert seen["count"] == 3


async def test_missing_sdk_yields_error_not_crash():
    backend = PydanticAIBackend(_settings())
    backend._sdk_available = False
    events = await _collect(backend, "hi")
    assert [e.type for e in events] == ["error"]
    assert "pocketpaw[pydantic-ai]" in events[0].content


async def test_model_failure_yields_error_then_done():
    def boom():
        raise RuntimeError("proxy unreachable")

    backend = PydanticAIBackend(_settings())
    backend._build_model = boom  # type: ignore[method-assign]
    events = await _collect(backend, "hi")
    assert [e.type for e in events] == ["error", "done"]
    assert "proxy unreachable" in events[0].content


# --------------------------------------------------------------------------
# concurrency — the failure mode this backend was shaped around
# --------------------------------------------------------------------------


async def test_concurrent_runs_each_produce_content():
    """N concurrent runs on ONE cached backend instance must each yield content.

    This is the shape of the observed production failure (33 of 49 runs
    returning an empty ``stream_end``), so it is worth asserting directly — but
    it does NOT on its own prove cancellation is per-run: a mutation probe
    showed it passing against a shared instance flag, because no ``stop()``
    lands between these runs. ``test_a_new_run_does_not_resurrect_a_stopped_one``
    is the test that holds that property; this one guards the empty-content
    symptom and the fan-out path.
    """
    n = 25
    backend = _backend_with_model(TestModel(custom_output_text="content"))

    async def one(i: int) -> list:
        return [ev async for ev in backend.run(f"run {i}")]

    results = await asyncio.gather(*(one(i) for i in range(n)))

    assert len(results) == n
    empty = [
        i
        for i, evs in enumerate(results)
        if not any(e.type == "message" and e.content for e in evs)
    ]
    assert not empty, f"{len(empty)}/{n} runs terminated with no content: {empty}"
    assert all(evs[-1].type == "done" for evs in results)


async def test_agent_is_built_once_across_runs():
    """The compiled agent must be cached across runs on one instance.

    Regression: the cache key was computed BEFORE ``_build_custom_tools()``
    populated ``_custom_tools``, so the key compared ``id(None)`` against
    ``id(list)`` on the next run and never matched. Every run then re-instantiated
    all 49 builtin tools (~2.4s cold) — a per-run cost on the backend whose whole
    purpose is a low per-run cost, and invisible except as latency.
    """
    backend = _backend_with_model(TestModel(custom_output_text="ok"))

    await _collect(backend, "one")
    first = backend._cached_agent
    key_after_first = backend._cached_agent_key

    await _collect(backend, "two")

    assert backend._cached_agent is first, "agent rebuilt on the second run"
    assert backend._cached_agent_key == key_after_first


async def test_stop_does_not_poison_a_later_run():
    """A run started AFTER ``stop()`` must not be born already-cancelled."""
    backend = _backend_with_model(TestModel(custom_output_text="fresh"))

    await backend.stop()

    events = await _collect(backend, "after stop")
    assert any(e.type == "message" and e.content for e in events)
    assert events[-1].type == "done"


async def test_a_new_run_does_not_resurrect_a_stopped_one():
    """Starting run B must not un-cancel in-flight run A.

    THIS is the test that distinguishes per-run state from the ``deep_agents``
    shape, and it was written only after a mutation probe showed the other
    concurrency tests here passing against a shared instance flag — they were
    holding the fixture, not the property.

    The shared-flag shape fails this in both directions: ``run()`` entry does
    ``self._stop_flag = False``, so B's *start* resurrects the stopped A, and
    ``stop()`` sets one flag, so stopping A would also truncate B.

    Interleaving is explicit (``__anext__`` by hand, no ``gather``) because the
    bug is an ordering bug — a shared flag survives concurrent runs fine right
    up until a ``stop()`` lands between them.
    """
    backend = _backend_with_model(TestModel(custom_output_text="aaaa bbbb cccc"))

    run_a = backend.run("run A").__aiter__()
    first = await run_a.__anext__()
    assert first.type in {"message", "tool_use", "thinking"}

    # Stop everything in flight — right now that is only A.
    await backend.stop()

    # B starts fresh and must complete normally.
    events_b = await _collect(backend, "run B")
    assert any(e.type == "message" and e.content for e in events_b)

    # A must still be stopped. Under a shared flag, B's entry reset revives it
    # and A keeps streaming content here.
    tail_a = [ev async for ev in run_a]
    assert not [e for e in tail_a if e.type == "message" and e.content], (
        "stopped run resumed after a sibling run started — cancellation is "
        "shared instance state, not per-run"
    )


async def test_stop_signals_only_runs_live_at_that_moment():
    backend = PydanticAIBackend(_settings())
    live, done = _RunHandle(), _RunHandle()
    backend._active.add(live)

    await backend.stop()

    assert live.stopped is True
    # A handle never registered (or already finished) is untouched.
    assert done.stopped is False


async def test_run_handle_is_released_on_completion():
    backend = _backend_with_model(TestModel(custom_output_text="ok"))
    await _collect(backend, "hi")
    assert backend._active == set(), "a finished run must not leak its handle"


async def test_run_handle_is_released_on_error():
    def boom():
        raise RuntimeError("nope")

    backend = PydanticAIBackend(_settings())
    backend._build_model = boom  # type: ignore[method-assign]
    await _collect(backend, "hi")
    assert backend._active == set()


async def test_status_reports_active_runs():
    backend = PydanticAIBackend(_settings())
    status = await backend.get_status()
    assert status["backend"] == "pydantic_ai"
    assert status["active_runs"] == 0
    assert status["running"] is False


# --------------------------------------------------------------------------
# specialist eligibility
# --------------------------------------------------------------------------


def test_mcp_is_on_by_default():
    """On by default, now that servers are held open for the instance lifetime.

    ``test_mcp_servers_spawn_once_across_many_runs`` is the measurement that
    earns this default; before the exit-stack hold, MCP shipped off.
    """
    assert PydanticAIBackend(_settings()).settings.pydantic_ai_mcp_enabled is True


class _SpawnCountingServer:
    """Stand-in for an MCP toolset that records start/stop like the real one.

    Mirrors ``MCPToolset``'s refcount contract (``mcp.py:_running_count``):
    ``__aenter__`` starts the process only on the 0 -> 1 transition, and
    ``__aexit__`` stops it on 1 -> 0. Counting spawns against this measures OUR
    lifecycle handling, which is the thing under test — a real stdio server
    would measure fastmcp's ``keep_alive`` instead, and would need a live MCP
    binary on the box.
    """

    def __init__(self) -> None:
        self.spawns = 0
        self.stops = 0
        self._count = 0

    async def __aenter__(self):
        # The sleep is load-bearing, not padding. Starting a real MCP server
        # does I/O and therefore suspends; without a suspension point here the
        # "concurrent" runs never actually interleave, and the cold-start-lock
        # test silently passes with the lock removed (confirmed by probe).
        if self._count == 0:
            await asyncio.sleep(0.02)
            self.spawns += 1
        self._count += 1
        return self

    async def __aexit__(self, *exc):
        self._count -= 1
        if self._count == 0:
            self.stops += 1
        return False

    @property
    def running(self) -> bool:
        return self._count > 0


class _FakeCfg:
    def __init__(self, name="fake", **kw):
        self.name = name
        self.enabled = True
        self.transport = "stdio"
        self.command = "node"
        self.args = []
        self.env = None
        self.url = None
        self.__dict__.update(kw)


def _drive_real_mcp(monkeypatch, servers, *, n_configs=1):
    """Make the REAL ``_start_mcp_servers`` build *servers*.

    Deliberately does NOT stub ``_start_mcp_servers`` itself. An earlier version
    of these tests replaced that method with a helper carrying its own copy of
    the exit-stack hold, so the mutation probe passed — the test was measuring
    the fixture, not the code. Only the leaf collaborators are stubbed here
    (config loading and toolset construction) so the hold, the failure
    handling and the lock all execute for real.
    """
    monkeypatch.setattr(
        "pocketpaw.mcp.config.load_mcp_config",
        lambda: [_FakeCfg(name=f"s{i}") for i in range(n_configs)],
    )
    it = iter(servers)
    monkeypatch.setattr("pydantic_ai.mcp.MCPToolset", lambda client, **kw: next(it))
    monkeypatch.setattr("pydantic_ai.toolsets.PrefixedToolset", lambda ts, prefix: ts)


async def test_mcp_servers_spawn_once_across_many_runs(monkeypatch):
    """A server must start ONCE per backend instance, not once per run.

    This is the measurement that earns MCP being on by default. pydantic-ai's
    MCP toolsets are refcounted, so a cached-but-unheld server tears down the
    moment concurrent runs reach zero and respawns on the next one — at sparse
    traffic, a stdio subprocess spawn on every single turn.

    Mutation-checked: drop the ``AsyncExitStack`` hold in ``_start_mcp_servers``
    and this fails (the server is stopped straight after being started).
    """
    pytest.importorskip("fastmcp", reason="pydantic-ai-slim[mcp] not installed")

    server = _SpawnCountingServer()
    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=True))
    backend._build_model = lambda: TestModel(custom_output_text="ok")  # type: ignore[method-assign]
    backend._custom_tools = []
    _drive_real_mcp(monkeypatch, [server])

    for i in range(5):
        await _collect(backend, f"run {i}")

    assert server.spawns == 1, f"server spawned {server.spawns} times across 5 runs"
    assert server.stops == 0, "server torn down between runs — it will respawn on the next"
    assert server.running is True, "refcount fell to zero; the next run pays a spawn"

    # Only stop() releases it.
    await backend.stop()
    assert server.stops == 1
    assert backend._mcp_stack is None


async def test_concurrent_first_runs_do_not_double_spawn(monkeypatch):
    """Two runs racing on a cold instance must not each start the server set.

    ``_start_mcp_servers`` awaits, so without ``_mcp_lock`` both runs see an
    empty cache and each spawns a full set of subprocesses.

    Mutation-checked: replace the lock with a no-op context manager and the
    spawn count rises with concurrency.
    """
    pytest.importorskip("fastmcp", reason="pydantic-ai-slim[mcp] not installed")

    servers = [_SpawnCountingServer() for _ in range(4)]
    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=True))
    backend._build_model = lambda: TestModel(custom_output_text="ok")  # type: ignore[method-assign]
    backend._custom_tools = []
    _drive_real_mcp(monkeypatch, servers)

    await asyncio.gather(*(_collect(backend, f"run {i}") for i in range(4)))

    started = [s for s in servers if s.spawns]
    assert len(started) == 1, f"cold-start race started {len(started)} server sets, expected 1"
    assert started[0].spawns == 1


async def test_mcp_server_that_fails_to_start_is_dropped_not_fatal(monkeypatch):
    """MCP is additive to the tool surface, never load-bearing."""
    pytest.importorskip("fastmcp", reason="pydantic-ai-slim[mcp] not installed")

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("no such command")

        async def __aexit__(self, *exc):
            return False

    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=True))
    backend._build_model = lambda: TestModel(custom_output_text="ok")  # type: ignore[method-assign]
    backend._custom_tools = []
    _drive_real_mcp(monkeypatch, [_Broken()])

    events = await _collect(backend, "hi")

    assert events[-1].type == "done"
    assert not any(e.type == "error" for e in events)
    assert backend._mcp_tools == []
    assert backend._mcp_stack is None, "an all-failed start must not leak an open stack"


async def test_one_broken_server_does_not_take_down_a_healthy_one(monkeypatch):
    pytest.importorskip("fastmcp", reason="pydantic-ai-slim[mcp] not installed")

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("no such command")

        async def __aexit__(self, *exc):
            return False

    healthy = _SpawnCountingServer()
    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=True))
    backend._build_model = lambda: TestModel(custom_output_text="ok")  # type: ignore[method-assign]
    backend._custom_tools = []
    _drive_real_mcp(monkeypatch, [_Broken(), healthy], n_configs=2)

    await _collect(backend, "hi")

    assert backend._mcp_tools == [healthy]
    assert healthy.running is True


def test_mcp_transport_mapping():
    """stdio must go through an explicit StdioTransport with keep_alive on."""
    pytest.importorskip("fastmcp", reason="pydantic-ai-slim[mcp] not installed")

    class _Cfg:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    stdio = PydanticAIBackend._mcp_client_for(
        _Cfg(transport="stdio", command="node", args=["s.js"], env=None, url=None)
    )
    assert stdio.command == "node"
    assert stdio.keep_alive is True, "keep_alive off means the child dies between sessions"

    http = PydanticAIBackend._mcp_client_for(
        _Cfg(transport="streamable-http", url="https://x/mcp", command=None, args=None, env=None)
    )
    assert http == "https://x/mcp"

    assert (
        PydanticAIBackend._mcp_client_for(
            _Cfg(transport="stdio", command=None, args=None, env=None, url=None)
        )
        is None
    )


async def test_mcp_loading_is_cached_per_instance_not_per_run(monkeypatch):
    """Whatever MCP costs, it must be paid once per instance — never per run."""
    # NOT importorskip: ``pydantic_ai.mcp`` imports fine and defers the
    # "install fastmcp" ImportError to attribute access, so only touching the
    # class tells you whether the extra is really there.
    try:
        from pydantic_ai.mcp import MCPToolset  # noqa: F401
    except ImportError:
        pytest.skip("pydantic-ai-slim[mcp] not installed")

    calls = {"n": 0}

    def counting_loader():
        calls["n"] += 1
        return []

    monkeypatch.setattr("pocketpaw.mcp.config.load_mcp_config", counting_loader)

    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=True))
    backend._build_model = lambda: TestModel(custom_output_text="ok")  # type: ignore[method-assign]
    # Not _backend_with_model: this test needs MCP loading live, so it pins the
    # builtin tool surface itself (TestModel would otherwise execute all 49).
    backend._custom_tools = []

    await _collect(backend, "one")
    await _collect(backend, "two")
    await _collect(backend, "three")

    assert calls["n"] == 1, "MCP config re-read per run — that is the subprocess-per-run shape"


async def test_mcp_disabled_returns_empty():
    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=False))
    assert await backend._build_mcp_tools() == []
    assert backend._mcp_stack is None


# --------------------------------------------------------------------------
# harness capabilities
# --------------------------------------------------------------------------


def test_harness_capabilities_are_wired():
    """The four capabilities that fit a dispatch-only agent must be attached."""
    pytest.importorskip("pydantic_ai_harness", reason="harness not installed")

    caps = PydanticAIBackend(_settings())._build_capabilities()
    names = {type(c).__name__ for c in caps}

    assert {
        "SlidingWindow",
        "ClearToolResults",
        "Planning",
        "StepPersistence",
        "OverflowingToolOutput",
    } <= names, names


async def test_skills_capability_engages_and_excludes_script_execution(monkeypatch):
    """Skills reach the model, and the script-execution tool does NOT.

    ``run_skill_script`` executes a skill's bundled script locally. That is the
    thing dispatch-only rules out, and on an in-process backend it has no
    per-tenant jail — so its ABSENCE is a security property, not a preference.
    """
    pytest.importorskip("pydantic_ai_skills", reason="pydantic-ai-skills not installed")

    from pocketpaw.skills.loader import Skill as PawSkill

    fake = {
        "demo": PawSkill(
            name="demo",
            description="A demo skill.",
            content="# Demo\nDo the demo thing.",
            path=__import__("pathlib").Path("."),
        )
    }
    monkeypatch.setattr("pocketpaw.skills.loader.SkillLoader.load", lambda self, force=False: fake)

    seen: dict = {}

    async def stream_fn(messages, info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        yield "ok"

    backend = _backend_with_model(FunctionModel(stream_function=stream_fn))
    await _collect(backend, "use a skill")

    assert "list_skills" in seen["tools"], seen["tools"]
    assert "load_skill" in seen["tools"], seen["tools"]
    assert "run_skill_script" not in seen["tools"], "local script execution reached the model"
    assert "read_skill_resource" not in seen["tools"], seen["tools"]


def test_skills_are_passed_programmatically_not_discovered(monkeypatch):
    """No directory / git / S3 discovery — PocketPaw's loader is the one source."""
    pytest.importorskip("pydantic_ai_skills", reason="pydantic-ai-skills not installed")

    from pocketpaw.skills.loader import Skill as PawSkill

    fake = {
        "a": PawSkill(name="a", description="d", content="c", path=__import__("pathlib").Path("."))
    }
    monkeypatch.setattr("pocketpaw.skills.loader.SkillLoader.load", lambda self, force=False: fake)

    cap = PydanticAIBackend(_settings())._build_skills_capability()
    assert cap is not None
    assert not getattr(cap, "directories", None)
    assert not getattr(cap, "registries", None)


def test_skills_respect_disable_model_invocation(monkeypatch):
    pytest.importorskip("pydantic_ai_skills", reason="pydantic-ai-skills not installed")

    from pocketpaw.skills.loader import Skill as PawSkill

    p = __import__("pathlib").Path(".")
    fake = {
        "on": PawSkill(name="on", description="d", content="c", path=p),
        "off": PawSkill(
            name="off", description="d", content="c", path=p, disable_model_invocation=True
        ),
    }
    monkeypatch.setattr("pocketpaw.skills.loader.SkillLoader.load", lambda self, force=False: fake)

    names = {s.name for s in PydanticAIBackend(_settings())._build_skills_capability().skills}
    assert names == {"on"}


def test_skills_disabled_returns_none():
    assert (
        PydanticAIBackend(_settings(pydantic_ai_skills_enabled=False))._build_skills_capability()
        is None
    )


def test_skills_absent_when_loader_yields_nothing(monkeypatch):
    """An empty capability would add tool surface for nothing."""
    pytest.importorskip("pydantic_ai_skills", reason="pydantic-ai-skills not installed")

    monkeypatch.setattr("pocketpaw.skills.loader.SkillLoader.load", lambda self, force=False: {})
    assert PydanticAIBackend(_settings())._build_skills_capability() is None


def test_harness_drops_capabilities_that_need_a_filesystem():
    """Dropped ones must be dropped, not quietly half-wired.

    ``DeduplicateFileReads`` keys off file-read tools this backend does not
    have; ``RepoContext`` scans a repo from disk; ``SubAgents`` discovers
    agents from an on-disk folder by default. A capability that can never fire
    is worse than none — it reads as covered.
    """
    pytest.importorskip("pydantic_ai_harness", reason="harness not installed")

    names = {type(c).__name__ for c in PydanticAIBackend(_settings())._build_capabilities()}
    assert "DeduplicateFileReads" not in names
    assert "RepoContext" not in names
    assert "SubAgents" not in names


def test_step_persistence_store_is_in_memory():
    """Disk-backed stores share a process-global path across tenants."""
    pytest.importorskip("pydantic_ai_harness", reason="harness not installed")

    from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence

    caps = PydanticAIBackend(_settings())._build_capabilities()
    sp = next(c for c in caps if isinstance(c, StepPersistence))
    assert isinstance(sp.store, InMemoryStepStore)


def test_harness_can_be_disabled():
    """Escape hatch: the dependency is pre-1.0 in cadence and pinned exactly."""
    assert (
        PydanticAIBackend(_settings(pydantic_ai_harness_enabled=False))._build_capabilities() == []
    )


async def test_planning_capability_engages_on_a_real_run():
    """Proof it fires, not just that it was constructed.

    Planning contributes a todo toolset, so the write-todos tool has to be
    visible to the model on an actual run.
    """
    pytest.importorskip("pydantic_ai_harness", reason="harness not installed")

    seen: dict = {}

    async def stream_fn(messages, info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        yield "ok"

    backend = _backend_with_model(FunctionModel(stream_function=stream_fn))
    await _collect(backend, "plan something")

    # write_plan comes from Planning; read_tool_result from OverflowingToolOutput.
    # Both are harness-contributed, so seeing them proves the capabilities are
    # live on the run rather than merely constructed.
    assert "write_plan" in seen["tools"], seen["tools"]
    assert "read_tool_result" in seen["tools"], seen["tools"]


async def test_harness_disabled_removes_the_capability_tools():
    """The mirror of the test above — proves the toggle is real."""
    seen: dict = {}

    async def stream_fn(messages, info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        yield "ok"

    backend = _backend_with_model(
        FunctionModel(stream_function=stream_fn), pydantic_ai_harness_enabled=False
    )
    await _collect(backend, "plan something")

    assert "write_plan" not in seen["tools"], seen["tools"]
    assert "read_tool_result" not in seen["tools"], seen["tools"]


def test_attach_specialist_tools_makes_backend_eligible():
    """``backend.py:218`` excludes any backend whose attach raises."""
    backend = PydanticAIBackend(_settings())
    sentinel = object()
    backend.attach_specialist_tools([sentinel])

    assert backend._custom_tools == [sentinel]
    # MCP loading short-circuited for the short-lived specialist run
    assert backend._mcp_tools == []


def test_attach_specialist_tools_blocks_recursion_footgun():
    """Once specialist tools are attached, the bridge must NOT re-populate.

    ``pocket_specialist__create`` is auto-injected by the bridge for every
    main-agent run. If ``_build_custom_tools`` ignored the pre-filled cache, the
    specialist would be handed a tool that re-invokes the specialist.
    """
    backend = PydanticAIBackend(_settings())
    specialist_tool = object()
    backend.attach_specialist_tools([specialist_tool])

    assert backend._build_custom_tools() == [specialist_tool]


def test_attach_subprocess_env_is_a_noop():
    backend = PydanticAIBackend(_settings())
    assert backend.attach_subprocess_env({"X": "1"}) is None


async def test_pocket_session_strips_shell_and_fs_tools():
    """Dispatch-only is mechanical, not advisory.

    PocketPaw's builtin shell/fs tools jail against a PROCESS-GLOBAL
    ``file_jail_path``, so an in-process backend granted them shares one jail
    across every tenant.
    """
    from pydantic_ai.tools import Tool

    async def shell(command: str) -> str:
        """Run a shell command."""
        return ""

    async def safe_tool(q: str) -> str:
        """A dispatch-only tool."""
        return ""

    backend = _backend_with_model(TestModel(custom_output_text="ok"))
    backend._custom_tools = [
        Tool(shell, name="shell", description="Run a shell command."),
        Tool(safe_tool, name="safe_tool", description="A dispatch-only tool."),
    ]

    agent = backend._get_or_create_agent(
        TestModel(), "<pocket-scope>build a site</pocket-scope>", []
    )
    names = {t.name for t in agent._function_toolset.tools.values()}
    assert "shell" not in names
    assert "safe_tool" in names


# --------------------------------------------------------------------------
# usage / prompt-cache telemetry
# --------------------------------------------------------------------------


def test_usage_event_reports_uncached_remainder():
    """``RunUsage.input_tokens`` is the INCLUSIVE total; ``report_savings``
    wants the uncached remainder. Getting the subtraction backwards inflates
    the hit rate — the exact number the A/B turns on."""

    class _Usage:
        input_tokens = 1000
        cache_read_tokens = 700
        cache_write_tokens = 100

    class _Result:
        usage = _Usage()

    class _Event:
        result = _Result()

    backend = PydanticAIBackend(_settings())
    event = backend._usage_event(_Event())

    assert event is not None
    assert event.type == "token_usage"
    assert event.metadata["input_tokens"] == 200  # 1000 - 700 - 100
    assert event.metadata["cache_read_tokens"] == 700
    assert event.metadata["backend"] == "pydantic_ai"


def test_usage_event_absent_when_no_cache_activity():
    class _Usage:
        input_tokens = 500
        cache_read_tokens = 0
        cache_write_tokens = 0

    class _Result:
        usage = _Usage()

    class _Event:
        result = _Result()

    assert PydanticAIBackend(_settings())._usage_event(_Event()) is None


# --------------------------------------------------------------------------
# tool bridge
# --------------------------------------------------------------------------


def test_bridge_respects_optional_parameters():
    """Optional properties must not be forced on the model."""
    import inspect

    from pocketpaw.agents.tool_bridge import _signature_from_json_schema

    params, annotations = _signature_from_json_schema(
        inspect,
        {
            "properties": {"query": {"type": "string"}, "limit": {"type": "string"}},
            "required": ["query"],
        },
    )
    by_name = {p.name: p for p in params}
    assert by_name["query"].default is inspect.Parameter.empty
    assert by_name["limit"].default is None
    assert annotations["limit"] == (str | None)


def test_bridge_preserves_nested_model_schema():
    """Rich ``args_schema`` tools must keep their real types, not flatten to str."""
    import inspect

    from pydantic import BaseModel

    from pocketpaw.agents.tool_bridge import _signature_from_model

    class Hints(BaseModel):
        palette: str | None = None

    class Args(BaseModel):
        brief: str
        hints: Hints | None = None

    params, annotations = _signature_from_model(inspect, Args)
    by_name = {p.name: p for p in params}
    assert by_name["brief"].default is inspect.Parameter.empty
    assert annotations["hints"] == (Hints | None)


def test_bridge_returns_empty_without_pydantic_ai(monkeypatch):
    import builtins

    from pocketpaw.agents import tool_bridge

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pydantic_ai.tools":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert tool_bridge.build_pydantic_ai_tools(_settings()) == []


def test_bridged_tools_are_all_async():
    """A single sync tool caps the whole process at anyio's thread pool."""
    import inspect

    from pocketpaw.agents.tool_bridge import build_pydantic_ai_tools

    tools = build_pydantic_ai_tools(_settings(), backend="pydantic_ai")
    assert tools, "expected at least one bridged builtin tool"
    for tool in tools:
        assert inspect.iscoroutinefunction(tool.function), f"{tool.name} is not async"
