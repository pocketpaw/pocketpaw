"""Tests for bridging PocketPaw's IN-PROCESS MCP servers into pydantic-ai.

Created 2026-07-31.

These servers (sites, pocket, connectors, media, ...) are registered through the
``pocketpaw.mcp_servers`` entry points and built with the Claude Agent SDK's
``create_sdk_mcp_server``. Until this bridge they reached the Claude SDK backend
and nothing else, so an agent on ``pydantic_ai`` asked to build a site had no
``create_svelte_site`` to call. "The model wrote a file instead of calling the
tool" is what a missing tool looks like from the outside.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai", reason="pocketpaw[pydantic-ai] not installed")
pytest.importorskip("mcp", reason="mcp not installed")

from pocketpaw.agents.tool_bridge import build_inprocess_mcp_toolsets  # noqa: E402
from pocketpaw.tools.policy import ToolPolicy  # noqa: E402

_OBJ_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "object"}},
        "spec": {"type": "object"},
    },
    "required": ["source"],
}


async def _tool_names(toolsets: list) -> set[str]:
    """The names a MODEL would see - prefixes applied, exactly as at run time."""
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    seen: set[str] = set()

    async def capture(messages, info):
        seen.update(t.name for t in info.function_tools)
        yield "ok"

    agent = Agent(FunctionModel(stream_function=capture), toolsets=toolsets)
    async with agent.run_stream_events("hi") as stream:
        async for _ in stream:
            pass
    return seen


async def _call_one(toolsets: list, tool_name: str, json_args: str = "{}") -> str:
    """Drive one real tool call through an agent and return the transcript."""
    from pydantic_ai import Agent
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    async def stream_fn(messages, info: AgentInfo):
        if len(messages) == 1:
            yield {0: DeltaToolCall(name=tool_name, json_args=json_args, tool_call_id="c1")}
        else:
            yield "done"

    agent = Agent(FunctionModel(stream_function=stream_fn), toolsets=toolsets)
    events = []
    async with agent.run_stream_events("go") as stream:
        async for event in stream:
            events.append(event)
    return str(events)


def _fake_provider(server_name: str, tools: list):
    """A provider shaped like the real ones, backed by a real MCP Server."""
    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server

    server = Server(server_name)

    @server.list_tools()
    async def _list():
        return [
            mcp_types.Tool(name=n, description="The " + n + " tool.", inputSchema=schema)
            for n, schema in tools
        ]

    @server.call_tool()
    async def _call(name: str, arguments: dict):
        return [mcp_types.TextContent(type="text", text=name + " got " + repr(sorted(arguments)))]

    class _Provider:
        def build_server(self):
            return (server_name, {"type": "sdk", "name": server_name, "instance": server})

        def tool_ids(self):
            return ["mcp__" + server_name + "__" + n for n, _ in tools]

    return _Provider()


@pytest.fixture
def only_fake_providers(monkeypatch):
    """Replace the entry-point registry so a test does not depend on installs."""

    def _install(*providers):
        import pocketpaw.agents.tool_bridge as bridge

        monkeypatch.setattr(
            bridge, "_ext_providers_for_test", lambda group: list(providers), raising=False
        )
        import pocketpaw._registry as registry

        monkeypatch.setattr(registry, "providers", lambda group: list(providers))
        return providers

    return _install


async def test_a_bridged_tool_is_named_the_way_a_surface_spells_it(only_fake_providers):
    """``PrefixedToolset`` yields ``<server>_<tool>``, which is what
    ``_expand_tool_ids`` turns ``mcp__<server>__<tool>`` into. Any other naming
    and every surface deny/allow id silently matches nothing."""
    from pocketpaw.agents.pydantic_ai import _expand_tool_ids

    only_fake_providers(_fake_provider("pocketpaw_sites_manager", [("create_svelte_site", {})]))

    names = await _tool_names(await build_inprocess_mcp_toolsets())
    assert names == {"pocketpaw_sites_manager_create_svelte_site"}
    ids = frozenset({"mcp__pocketpaw_sites_manager__create_svelte_site"})
    assert _expand_tool_ids(ids) <= names


async def test_a_bridged_tool_really_calls_the_server(only_fake_providers):
    """Not a stub returning a plausible string - the server handler runs."""
    only_fake_providers(_fake_provider("srv", [("publish", _OBJ_SCHEMA)]))

    transcript = await _call_one(
        await build_inprocess_mcp_toolsets(), "srv_publish", '{"source": "x"}'
    )
    assert "publish got ['source']" in transcript


async def test_the_servers_own_schema_survives(only_fake_providers):
    """Synthesizing a signature flattens every argument to ``str``.
    ``edit_svelte_component`` takes a list of edits and ``create_dynamic_site``
    a whole spec object - as strings they reach the handler unusable."""
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    only_fake_providers(_fake_provider("srv", [("create", _OBJ_SCHEMA)]))
    toolsets = await build_inprocess_mcp_toolsets()

    captured: dict = {}

    async def capture(messages, info):
        captured.update({t.name: t.parameters_json_schema for t in info.function_tools})
        yield "ok"

    agent = Agent(FunctionModel(stream_function=capture), toolsets=toolsets)
    async with agent.run_stream_events("hi") as s:
        async for _ in s:
            pass

    schema = captured["srv_create"]
    assert schema["properties"]["edits"]["type"] == "array"
    assert schema["properties"]["spec"]["type"] == "object"
    assert schema["required"] == ["source"]


async def test_a_failing_server_does_not_take_the_others_with_it(only_fake_providers):
    class _Broken:
        def build_server(self):
            raise RuntimeError("stale editable install")

    only_fake_providers(_Broken(), _fake_provider("good", [("works", {})]))
    assert await _tool_names(await build_inprocess_mcp_toolsets()) == {"good_works"}


async def test_a_tool_error_is_returned_to_the_model_not_raised(only_fake_providers):
    """The model can read the reason and correct its arguments. Raising burns a
    retry on an error it never saw."""
    import mcp.types as mcp_types
    from mcp.server.lowlevel import Server

    server = Server("srv")

    @server.list_tools()
    async def _list():
        return [mcp_types.Tool(name="boom", description="Explodes.", inputSchema={})]

    @server.call_tool()
    async def _call(name: str, arguments: dict):
        raise ValueError("needs workspace context")

    class _P:
        def build_server(self):
            return ("srv", {"instance": server})

    only_fake_providers(_P())
    transcript = await _call_one(await build_inprocess_mcp_toolsets(), "srv_boom")
    assert "workspace context" in transcript


async def test_opt_in_servers_stay_off_unless_the_agent_named_them(only_fake_providers):
    """Mirrors the SDK backend's allowlist rule rather than inventing a second."""
    from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS

    opt_in = next(iter(OPT_IN_MCP_SERVERS))
    only_fake_providers(_fake_provider(opt_in, [("thing", {})]))

    assert await _tool_names(await build_inprocess_mcp_toolsets(ToolPolicy())) == set()

    allowed = ToolPolicy(mcp_servers_allow=frozenset({opt_in}))
    assert await _tool_names(await build_inprocess_mcp_toolsets(allowed)) == {opt_in + "_thing"}


# -- integration: the REAL servers -------------------------------------------


async def test_the_real_sites_tools_reach_the_backend():
    """The whole point, against the shipped servers rather than doubles.

    Both engines: /sites builds Svelte AND html sites, so a bridge that carried
    only ``create_svelte_site`` would leave half the surface broken.
    """
    pytest.importorskip("pocketpaw_ee", reason="pocketpaw-ee not installed")
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend
    from pocketpaw.config import Settings

    # Through the BACKEND, not the bridge function. Calling the bridge directly
    # passes even when nothing wires it into a run — a mutation probe deleting
    # exactly that line went undetected until this changed.
    backend = PydanticAIBackend(Settings(pydantic_ai_skills_enabled=False))
    names = await _tool_names(await backend._build_mcp_tools())

    for tool in (
        "create_svelte_site",
        "create_html_site",
        "create_landing_site",
        "create_dynamic_site",
        "edit_svelte_component",
        "publish",
    ):
        assert "pocketpaw_sites_manager_" + tool in names, tool + " missing"
