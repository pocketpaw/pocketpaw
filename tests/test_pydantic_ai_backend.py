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


async def test_mcp_is_off_by_default():
    """Default-off is the shipped state, not an accident of the test fixture.

    pydantic-ai's MCP servers are refcounted: a shared server tears down when
    concurrent runs reach zero and respawns on the next run, putting stdio
    subprocess churn back on the request path. Enabling it is gated on the
    subprocess-count measurement (PRD chunk 4).
    """
    backend = PydanticAIBackend(_settings())
    assert backend.settings.pydantic_ai_mcp_enabled is False
    assert await backend._build_mcp_tools() == []


async def test_mcp_loading_is_cached_per_instance_not_per_run(monkeypatch):
    """Whatever MCP costs, it must be paid once per instance — never per run."""
    # NOT importorskip: ``pydantic_ai.mcp`` imports fine and defers the
    # "install fastmcp" ImportError to attribute access, so only touching the
    # class tells you whether the extra is really there.
    try:
        from pydantic_ai.mcp import MCPServerStdio  # noqa: F401
    except ImportError:
        pytest.skip("pydantic-ai-slim[mcp] not installed — MCP ships OFF until PRD chunk 4")

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


async def test_mcp_absent_dependency_degrades_to_empty():
    """With the ``mcp`` extra uninstalled the branch must return empty, not raise.

    This is the SHIPPED configuration — the extra is deliberately excluded
    because it forces a starlette major bump for a default-OFF capability.
    """
    backend = PydanticAIBackend(_settings(pydantic_ai_mcp_enabled=True))
    assert await backend._build_mcp_tools() == []


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
