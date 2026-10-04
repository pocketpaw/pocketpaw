# tests/test_claude_sdk_scoped_mcp_registration.py — a scoped turn starts only the
# MCP servers that own a tool the turn can call.
#
# The tool gate (tests/test_claude_sdk_tool_scoping.py) refuses an out-of-scope
# call, but every ambient server still started on every turn: each in-process
# provider, the built-in widgets / atlas / studio servers and each external config
# server. On a /code (exclusive), mode or deny turn the agent could still find the
# out-of-scope tools through ToolSearch and spend attempts on calls the gate then
# refused, and building those servers cost time (the composio provider fetches).
#
# The rule pinned here: on a scoped turn (a deny set, a mode allow-list, or an
# exclusive cap) a server registers only when its name is the server segment of
# an id in the turn's FINAL ``allowed_tools``. A bare ``mcp__<server>`` entry
# counts, spelled the way the CLI names tools. A provider that cannot qualify is
# never built. A broad turn registers exactly what ``_get_mcp_servers()`` returns.
#
# Providers and external config are faked; the widgets / atlas / studio servers
# are the real ones.
#
# Mutations: tests/mutations/claude_sdk_tool_scoping.json.

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ClaudeAgentOptions

from pocketpaw.agents.claude_sdk import _mcp_server_of
from tests.test_claude_sdk_tool_scoping import _backend, _build

_X_READ = "mcp__srv_x__read"
_X_WRITE = "mcp__srv_x__write"
_Y_SEARCH = "mcp__srv_y__search"
_BUILTIN_SERVERS = {"pocketpaw_widgets", "pocketpaw_atlas", "pocketpaw_studio"}
# An external config server; the CLI names its tools ``mcp__My_Notes_v2__*``.
_EXTERNAL = SimpleNamespace(
    name="My Notes.v2", transport="stdio", command="x", args=[], env={}, url="", enabled=True
)
_EXTERNAL_ENTRY = "mcp__My_Notes_v2"
_EVERYTHING = {"srv_x", "srv_y", "composio", _EXTERNAL.name, *_BUILTIN_SERVERS}


class _Provider:
    """A ``pocketpaw.mcp_servers`` provider that counts its builds."""

    def __init__(self, server: str, *tools: str) -> None:
        self.server = server
        # No tools = a wholesale provider, like composio's bare ``mcp__composio``.
        self.ids = [f"mcp__{server}__{t}" for t in tools] or [f"mcp__{server}"]
        self.builds = 0

    def tool_ids(self) -> list[str]:
        return list(self.ids)

    def build_server(self) -> tuple[str, dict]:
        self.builds += 1
        return self.server, {"type": "sdk", "name": self.server}


@contextmanager
def _world() -> Iterator[dict[str, _Provider]]:
    """Fake providers + one external config server for the duration."""
    providers = {
        "srv_x": _Provider("srv_x", "read", "write"),
        "srv_y": _Provider("srv_y", "search"),
        "composio": _Provider("composio"),
    }

    def registry(group: str) -> list[Any]:
        return list(providers.values()) if group == "pocketpaw.mcp_servers" else []

    with (
        patch("pocketpaw._registry.providers", side_effect=registry),
        patch("pocketpaw.mcp.config.load_mcp_config", return_value=[_EXTERNAL]),
    ):
        yield providers


async def _scoped(**scope: Any) -> tuple[ClaudeAgentOptions, dict[str, _Provider]]:
    """Build one turn with real id collection and real registration. ``_build``
    patches the external config itself, so it is handed the same server."""
    with _world() as providers:
        options = await _build(pool=None, external=(_EXTERNAL.name,), **scope)
    return options, providers


async def test_an_exclusive_turn_starts_only_the_server_its_tool_lives_on() -> None:
    options, providers = await _scoped(
        exclusive_mcp_tools=True, allow_mcp_tool_ids=frozenset({_X_READ})
    )

    assert set(options.mcp_servers) == {"srv_x"}
    assert providers["srv_y"].builds == 0, "an out-of-scope provider must not even be built"
    assert providers["composio"].builds == 0, "composio's fetch is off an exclusive turn"


async def test_a_mode_turn_keeps_its_servers_the_grant_and_the_always_allowed() -> None:
    options, _ = await _scoped(allow_mcp_tool_ids=frozenset({_Y_SEARCH}))

    assert set(options.mcp_servers) == {"srv_y", "composio", *_BUILTIN_SERVERS}


async def test_a_deny_that_covers_a_whole_server_drops_only_that_server() -> None:
    options, providers = await _scoped(deny_mcp_tool_ids=frozenset({_X_READ, _X_WRITE}))

    assert set(options.mcp_servers) == _EVERYTHING - {"srv_x"}
    assert providers["srv_x"].builds == 0


async def test_a_partly_denied_server_still_starts() -> None:
    options, _ = await _scoped(deny_mcp_tool_ids=frozenset({_X_WRITE}))

    assert set(options.mcp_servers) == _EVERYTHING


async def test_a_bare_external_entry_starts_the_server_under_its_config_name() -> None:
    options, _ = await _scoped(
        exclusive_mcp_tools=True, allow_mcp_tool_ids=frozenset({_EXTERNAL_ENTRY})
    )

    assert set(options.mcp_servers) == {_EXTERNAL.name}


async def test_on_every_scoped_turn_each_started_server_owns_an_allowed_tool() -> None:
    scopes: list[dict[str, Any]] = [
        {"exclusive_mcp_tools": True, "allow_mcp_tool_ids": frozenset({_X_READ})},
        {"exclusive_mcp_tools": True, "allow_mcp_tool_ids": None},
        {"allow_mcp_tool_ids": frozenset({_Y_SEARCH})},
        {"deny_mcp_tool_ids": frozenset({_Y_SEARCH, "mcp__composio"})},
    ]
    for scope in scopes:
        options, _ = await _scoped(**scope)
        started = {re.sub(r"[^a-zA-Z0-9_-]", "_", s) for s in options.mcp_servers}
        owners = {_mcp_server_of(t) for t in options.allowed_tools if t.startswith("mcp__")}
        assert started <= owners, f"{scope}: started with no callable tool: {started - owners}"


async def test_a_broad_turn_starts_exactly_what_it_did_before() -> None:
    """Parity: no deny, no mode list, not exclusive, so nothing is filtered."""
    backend = _backend()
    with _world():
        options = await _build(backend, pool=None, external=(_EXTERNAL.name,))
        unscoped = backend._get_mcp_servers()

    assert set(options.mcp_servers) == set(unscoped) == _EVERYTHING


async def test_a_tools_off_turn_still_starts_nothing() -> None:
    options, providers = await _scoped(tools_enabled=False)

    assert not options.mcp_servers
    assert not any(p.builds for p in providers.values())
