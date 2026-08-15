# Bridge-level tests for humanized tool narration (HTN-1).
# Created: 2026-08-15 — proves a real ``tool_use`` event reaches the wire as
# "Searching the web for quarterly filings" instead of a bare tool name.
#
# Updated: 2026-08-15 (HTN-2) — the narration lookup no longer instantiates a
# tool from a one-entry name -> class map, so what the bridge emits changed in
# three ways:
#   - an unannotated tool now derives a phrase ("Publishing the site") where it
#     previously emitted no narration field at all;
#   - the interpolated search phrase is asserted against ``litellm_web_search``,
#     the name the CLOUD path actually emits, which resolves through the
#     override table;
#   - the builtin ``web_search`` still renders its DECLARED phrase, now read off
#     the live instance in the agent's own ``ToolRegistry`` (resolved through
#     ``tool_bridge.narration_registry_for``) instead of off a tool the lookup
#     constructed. A backend that bridges no tools — the Claude SDK one, whose
#     tools come over MCP — has no registry to resolve and derives instead;
#     both paths have a test.
#
# The events here use the shape the backends ACTUALLY emit — ``content`` is a
# prose string ("Using web_search...") and ``metadata`` carries the bare name
# plus the call's input (see ``agents/claude_sdk.py`` and ``agents/pydantic_ai.py``).
# Handing the bridge a tidier hand-built event would test the function rather
# than the surface, and would certify a payload no backend ever produces.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _done_event():
    return SimpleNamespace(type="done", content="", metadata={})


def _tool_use_event(name: str, tool_input: dict):
    """A ``tool_use`` event shaped exactly like the SDK backends emit one."""
    return SimpleNamespace(
        type="tool_use",
        content=f"Using {name}...",
        metadata={"name": name, "input": tool_input},
    )


def _bridged_backend():
    """A backend whose bridged tool surface is reachable, as a real one is.

    Mirrors production: ``tool_bridge`` builds the registry under the agent's
    ToolPolicy and keeps it there, and every bridged backend exposes
    ``get_tool_policy``. Building it through the real helper means this carries
    the live ``WebSearchTool`` instance, not a stub of one — the point of the
    seam is that the phrase is read off the instance the agent actually holds.
    """
    from pocketpaw.agents.tool_bridge import _build_tool_registry
    from pocketpaw.tools.policy import ToolPolicy

    policy = ToolPolicy(profile="full")
    _build_tool_registry("pydantic_ai", policy)
    return SimpleNamespace(get_tool_policy=lambda: policy)


async def _emitted_tool_use(events: list, backend=None) -> list[dict]:
    """Drive ``_run_agent_response`` over ``events`` and return the payloads of
    every ``agent.tool_use`` it emitted.

    ``backend`` rides on the agent instance the pool hands back, which is how
    the narration lookup reaches the agent's own tool registry. Defaults to
    None — a backend that bridges no tools, like the Claude SDK one.
    """
    from pocketpaw_ee.cloud.shared import agent_bridge

    instance = SimpleNamespace(agent_name="Test Agent", backend=backend)
    pool = MagicMock()
    pool.get = AsyncMock(return_value=instance)
    pool.observe = AsyncMock()

    async def fake_run(*_args, **_kwargs):
        for event in events:
            yield event

    pool.run = fake_run

    from pocketpaw_ee.cloud.models.message import Message as _RealMessage

    # Beanie isn't initialized in unit tests; stub the history query so the
    # bridge never reaches a real database. The run yields no text, so the
    # bridge short-circuits before the persistence branch.
    to_list_mock = AsyncMock(return_value=[])
    limit_mock = MagicMock()
    limit_mock.to_list = to_list_mock
    sort_mock = MagicMock()
    sort_mock.limit = MagicMock(return_value=limit_mock)
    find_mock = MagicMock()
    find_mock.sort = MagicMock(return_value=sort_mock)

    with (
        patch("pocketpaw_ee.cloud.shared.agent_bridge.emit", new=AsyncMock()) as m_emit,
        patch.multiple(
            _RealMessage,
            create=True,
            group=MagicMock(),
            deleted=MagicMock(),
            createdAt=MagicMock(),
        ),
        patch.object(_RealMessage, "find", MagicMock(return_value=find_mock)),
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch(
            "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context",
            new=AsyncMock(return_value=""),
        ),
    ):
        await agent_bridge._run_agent_response(
            agent_id="agent-1",
            group_id="group-1",
            workspace_id="ws-1",
            user_message="what were the quarterly filings?",
            group_members=["user-1"],
        )

    return [
        call.args[0].data
        for call in m_emit.await_args_list
        if call.args and call.args[0].type == "agent.tool_use"
    ]


@pytest.mark.asyncio
async def test_search_tool_use_carries_a_humanized_narration():
    """The headline behaviour: a real search call reaches the wire with an
    interpolated plain-language phrase, and the tool name rides alongside it.

    The name is ``litellm_web_search`` rather than ``web_search`` because that
    is what the CLOUD path — the only consumer of this bridge — actually emits:
    with LiteLLM's search interception off, the model calls the proxy's search
    tool under that name (see the LiteLLM finding in
    ``docs/design/2026-08-15-humanized-tool-narration-tasks.md``). It resolves
    through the narration override table, which needs no registry handle.
    """
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("litellm_web_search", {"query": "quarterly filings"}),
            _done_event(),
        ]
    )

    assert len(payloads) == 1, f"expected one agent.tool_use emit, got {payloads}"
    payload = payloads[0]

    assert payload["narration"] == "Searching the web for quarterly filings"
    # Additive only — clients keyed on ``tool`` keep working untouched.
    assert payload["tool"] == "litellm_web_search"
    assert payload["agent_id"] == "agent-1"
    assert payload["group_id"] == "group-1"


@pytest.mark.asyncio
async def test_builtin_web_search_renders_its_declared_phrase_through_the_registry():
    """THE no-regression test at the bridge.

    ``WebSearchTool`` declares the interpolated phrase. Before HTN-2 the bridge
    reached it through a name -> (module, class) map that INSTANTIATED the tool;
    that map is gone, so this now resolves the agent's own ``ToolRegistry`` and
    reads the declaration off the live instance. If the seam breaks, this drops
    to the derived "Searching the web" — the silent downgrade of the one tool
    that already worked.
    """
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("web_search", {"query": "quarterly filings"}),
            _done_event(),
        ],
        backend=_bridged_backend(),
    )

    assert payloads[0]["tool"] == "web_search"
    assert payloads[0]["narration"] == "Searching the web for quarterly filings"


@pytest.mark.asyncio
async def test_a_backend_that_bridges_no_tools_still_narrates_by_derivation():
    """The Claude SDK backend surfaces its tools over MCP, so there is no
    registry to resolve. The chain must still answer — from the tool name."""
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("web_search", {"query": "quarterly filings"}),
            _done_event(),
        ]
    )

    assert payloads[0]["narration"] == "Searching the web"


@pytest.mark.asyncio
async def test_unannotated_tool_emits_a_derived_narration():
    """HTN-2: a tool that declares nothing still reaches the wire as English.

    This is the case the whole feature is named for — the surface used to
    render "using pocketpaw_sites_publish".
    """
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("pocketpaw_sites_publish", {"pocket_id": "p1"}),
            _done_event(),
        ]
    )

    assert len(payloads) == 1
    payload = payloads[0]

    assert payload["tool"] == "pocketpaw_sites_publish"
    assert payload["narration"] == "Publishing the site"
    # A derived phrase is built from the NAME, so no argument rides along with
    # it — the name carries no ``safe_args`` allowlist that could vet one.
    assert "p1" not in payload["narration"]


@pytest.mark.asyncio
async def test_missing_query_degrades_to_the_bare_phrase():
    """A tool call whose args don't carry the allowlisted field still narrates
    — just without interpolation."""
    payloads = await _emitted_tool_use(
        [
            _tool_use_event("web_search", {}),
            _done_event(),
        ]
    )

    assert payloads[0]["narration"] == "Searching the web"
    assert payloads[0]["tool"] == "web_search"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["litellm_web_search", "web_search"])
async def test_secrets_in_tool_input_never_reach_the_wire(tool_name):
    """The bridge hands the whole input dict to the renderer, so this is the
    end-to-end proof that only allowlisted fields survive the trip.

    Run against both search paths — the interpolating one (override) and the
    derived one — because the allowlist has to hold on whichever branch of the
    lookup answers, not just the one that happens to interpolate.
    """
    payloads = await _emitted_tool_use(
        [
            _tool_use_event(
                tool_name,
                {"query": "quarterly filings", "api_key": "sk-live-SUPERSECRET"},
            ),
            _done_event(),
        ]
    )

    assert "SUPERSECRET" not in str(payloads[0])
    assert "api_key" not in str(payloads[0])
    assert payloads[0]["narration"].startswith("Searching the web")


@pytest.mark.asyncio
async def test_event_without_metadata_still_reports_a_tool():
    """Backwards compatibility: an event carrying only ``content`` (no
    metadata) keeps falling back to the old content-based extraction."""
    payloads = await _emitted_tool_use(
        [
            SimpleNamespace(type="tool_use", content={"tool": "shell"}, metadata={}),
            _done_event(),
        ]
    )

    assert payloads[0]["tool"] == "shell"
    assert payloads[0].get("narration") is None


@pytest.mark.asyncio
async def test_thinking_event_is_unchanged():
    """The thinking branch shares the AgentToolUse wire type and must not have
    grown a narration field."""
    payloads = await _emitted_tool_use(
        [
            SimpleNamespace(type="thinking", content="pondering", metadata={}),
            _done_event(),
        ]
    )

    assert payloads[0]["tool"] == "thinking"
    assert "narration" not in payloads[0]
