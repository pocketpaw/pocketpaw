# tests/test_claude_sdk_tool_scoping.py — the Claude SDK backend enforces a
# turn's tool scope at CALL time, not only in the allowlist.
#
# The backend runs the CLI under ``permission_mode="bypassPermissions"``, and
# under bypass ``allowed_tools`` is only an auto-approve list: a tool missing
# from it is still offered and still runs (Agent SDK permissions docs; probed
# with the real CLI, where an in-process MCP tool off the allowlist executed).
# So every restriction computed into ``allowed_tools`` (the browser deny off
# /browser, the /sites and /code built-in denies, the /code exclusive cap,
# per-mode allow-lists, the pocket-session lock) did nothing on this backend.
#
# What these tests pin, all derived from the turn's FINAL allowed set:
#   * one PreToolUse gate (``matcher=None``) that denies any tool outside it;
#   * ``tools=`` pinned to the turn's built-ins plus ``ToolSearch`` (without it
#     the CLI turns tool search off and loads every MCP schema up front);
#   * ``disallowed_tools`` carrying the surface deny set;
#   * ``ENABLE_CLAUDEAI_MCP_SERVERS=false`` in the subprocess env, so a
#     claude.ai login cannot pour the account's connectors into the agent.
# Plus the regression guard for the fix itself: on a broad surface the gate
# allows every tool every registered in-process MCP server exposes.
#
# Options are built with the REAL ``ClaudeAgentOptions`` / ``HookMatcher`` so a
# misspelled option field fails here instead of at spawn time.
#
# Mutations: tests/mutations/claude_sdk_tool_scoping.json.

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from pocketpaw.agents.claude_sdk import POCKET_CREATION_GRANT, ClaudeSDKBackend
from pocketpaw.tools.policy import ToolPolicy
from tests.test_claude_sdk_model_override import _make_settings

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"

_CLICK = "mcp__pocketpaw_browser__click"
_NAVIGATE = "mcp__pocketpaw_browser__navigate"
_READ_FILE = "mcp__pocketpaw_code__readFile"
_RUN_SCENARIO = "mcp__pocketpaw_foresight__run_scenario"
_CREATE_TASK = "mcp__pocketpaw_tasks__create_task"
_PLAN_POCKET = "mcp__pocketpaw_pocket_planner__plan_pocket"
_WIDGET_SPEC = "mcp__pocketpaw_widgets__get_widget_spec"
# An external config server is allowlisted as a bare ``mcp__<server>`` entry.
_EXTERNAL = "mcp__foo"

# What ``_collect_mcp_tool_ids`` yields in these tests: fixed, so every scoping
# rule has a real candidate to strip.
_POOL = [
    _CLICK,
    _NAVIGATE,
    _READ_FILE,
    _RUN_SCENARIO,
    _CREATE_TASK,
    _PLAN_POCKET,
    _WIDGET_SPEC,
    _EXTERNAL,
]

_BUILTINS = ["Agent", "Bash", "Read", "Write", "Edit", "Glob", "Grep"]
_BUILTINS += ["WebSearch", "WebFetch", "Skill"]
_POCKET = "<pocket-scope>p1</pocket-scope>\nidentity"


def _backend(*, policy: ToolPolicy | None = None) -> ClaudeSDKBackend:
    with patch.object(ClaudeSDKBackend, "_initialize"):
        backend = ClaudeSDKBackend(_make_settings(), policy=policy)
    backend._sdk_available = True
    backend._cli_available = True
    backend._ClaudeAgentOptions = ClaudeAgentOptions
    backend._HookMatcher = HookMatcher
    backend._StreamEvent = None
    return backend


def _llm(env: dict[str, str]) -> MagicMock:
    llm = MagicMock()
    llm.is_ollama = llm.is_openai_compatible = llm.is_gemini = False
    llm.is_litellm = llm.is_openrouter = False
    llm.to_sdk_env.return_value = dict(env)
    return llm


async def _build(
    backend: ClaudeSDKBackend | None = None,
    *,
    pool: list[str] | None = _POOL,
    provider_env: dict[str, str] | None = None,
    **scope: Any,
) -> ClaudeAgentOptions:
    """Build one turn's options. ``scope`` overrides the ``_build_options`` kwargs.

    ``pool=None`` keeps the backend's real MCP id collection AND real server
    registration (the parity test); otherwise both are stubbed.
    """
    backend = backend or _backend()
    kwargs: dict[str, Any] = {
        "system_prompt": "identity",
        "session_key": "s1",
        "deny_mcp_tool_ids": frozenset(),
        "allow_sdk_tools": frozenset(),
        "allow_mcp_tool_ids": None,
        "skill_names": frozenset(),
        "stderr_sink": [],
        **scope,
    }
    with ExitStack() as stack:
        stack.enter_context(patch(_LLM_CLIENT, return_value=_llm(provider_env or {})))
        if pool is not None:
            stack.enter_context(
                patch.object(backend, "_collect_mcp_tool_ids", return_value=list(pool))
            )
            stack.enter_context(patch.object(backend, "_get_mcp_servers", return_value={}))
        built = await backend._build_options("hello", **kwargs)
    return built.options


def _gate(options: ClaudeAgentOptions):
    gates = [m for m in options.hooks.get("PreToolUse", []) if m.matcher is None]
    assert len(gates) == 1, (
        "expected exactly one PreToolUse hook that matches EVERY tool; under "
        "bypassPermissions it is the only thing that enforces allowed_tools"
    )
    (hook,) = gates[0].hooks
    return hook


async def _allows(options: ClaudeAgentOptions, tool_name: str) -> bool:
    """Ask the turn's gate about one call, the way the CLI does."""
    out = await _gate(options)(
        {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {}},
        "toolu_test",
        {"signal": None},
    )
    if out == {}:
        return True
    decision = out["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert tool_name in decision["permissionDecisionReason"], "a denial must name the tool"
    return False


# ── the bug: a denied tool still ran ─────────────────────────────────────────


async def test_a_denied_mcp_tool_is_refused_at_call_time() -> None:
    """Every surface but /browser denies the browser tools. Before the gate the
    id was only dropped from ``allowed_tools``, which bypassPermissions ignores,
    so the call ran anyway."""
    options = await _build(deny_mcp_tool_ids=frozenset({_CLICK}))

    assert not await _allows(options, _CLICK)
    assert await _allows(options, _NAVIGATE), "only the denied id is refused"


async def test_without_a_deny_the_same_tool_is_allowed() -> None:
    options = await _build()

    assert await _allows(options, _CLICK)


async def test_an_exclusive_turn_reaches_only_its_declared_mcp_tools() -> None:
    options = await _build(exclusive_mcp_tools=True, allow_mcp_tool_ids=frozenset({_READ_FILE}))

    assert await _allows(options, _READ_FILE)
    assert not await _allows(options, _RUN_SCENARIO), "another registered id leaked in"
    for grant_id in POCKET_CREATION_GRANT:
        assert not await _allows(options, grant_id), "the universal grant must not apply"


async def test_a_mode_allow_list_refuses_other_modes_tools_but_keeps_the_grant() -> None:
    options = await _build(allow_mcp_tool_ids=frozenset({_RUN_SCENARIO}))

    assert await _allows(options, _RUN_SCENARIO)
    assert not await _allows(options, _CREATE_TASK), "outside the mode set and the grant"
    assert await _allows(options, _PLAN_POCKET), "the pocket-creation grant survives"


async def test_a_bare_server_entry_admits_that_server_and_no_other() -> None:
    options = await _build()

    assert await _allows(options, "mcp__foo__anything")
    assert not await _allows(options, "mcp__foobar__x")


# ── built-ins: pinned, so the CLI's other tools never reach the agent ────────


async def test_a_turn_pins_its_builtins_plus_tool_search() -> None:
    options = await _build()

    assert options.tools == [*_BUILTINS, "ToolSearch"]
    for leaked in ("CronCreate", "SendMessage", "Workflow", "RemoteTrigger", "PushNotification"):
        assert leaked not in options.tools
        assert not await _allows(options, leaked)


async def test_a_pocket_session_pins_only_delegation_and_web() -> None:
    options = await _build(system_prompt=_POCKET)

    assert options.tools == ["Agent", "WebSearch", "WebFetch", "ToolSearch"]
    assert not await _allows(options, "Bash")
    assert await _allows(options, _WIDGET_SPEC), "pocket MCP tools stay reachable"


async def test_the_pinned_builtins_are_policy_filtered() -> None:
    backend = _backend(policy=ToolPolicy(profile="full", deny=["shell"]))
    options = await _build(backend, system_prompt=_POCKET)

    assert options.tools == ["WebSearch", "WebFetch", "ToolSearch"]
    assert not await _allows(options, "Agent")


async def test_an_allowed_sdk_builtin_is_pinned_and_an_mcp_grant_is_not() -> None:
    options = await _build(
        system_prompt=_POCKET, allow_sdk_tools=frozenset({"Read", "mcp__bar__baz"})
    )

    assert "Read" in options.tools
    assert not [t for t in options.tools if t.startswith("mcp__")]
    assert await _allows(options, "Read")
    assert await _allows(options, "mcp__bar__baz")


async def test_a_denied_builtin_is_unpinned_disallowed_and_refused() -> None:
    """/sites and /code deny Bash, Read and the rest by bare name."""
    options = await _build(deny_mcp_tool_ids=frozenset({"Bash"}))

    assert "Bash" not in options.tools
    assert "Bash" in options.disallowed_tools
    assert not await _allows(options, "Bash")
    assert await _allows(options, "Read")


async def test_disallowed_tools_carries_the_whole_deny_set() -> None:
    deny = frozenset({_CLICK, "Bash", "mcp__never__registered"})
    options = await _build(deny_mcp_tool_ids=deny)

    assert options.disallowed_tools == sorted(deny)


async def test_no_deny_means_no_disallowed_tools() -> None:
    options = await _build()

    assert not options.disallowed_tools


async def test_the_dangerous_command_hook_is_still_registered() -> None:
    options = await _build()

    assert len([m for m in options.hooks["PreToolUse"] if m.matcher == "Bash"]) == 1


# ── claude.ai connectors stay out of the subprocess ───────────────────────────


async def test_claude_ai_connectors_are_off_on_a_default_turn(monkeypatch) -> None:
    """Parent env already carries the MCP output cap and there is no provider
    env: the one turn shape where the backend used to send no ``env`` at all."""
    monkeypatch.setenv("MAX_MCP_OUTPUT_TOKENS", "1000")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    options = await _build()

    assert options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


async def test_claude_ai_connectors_are_off_alongside_a_provider_env(monkeypatch) -> None:
    monkeypatch.delenv("MAX_MCP_OUTPUT_TOKENS", raising=False)
    provider = {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_BASE_URL": "http://gateway"}
    options = await _build(provider_env=provider)

    assert options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    for key, value in provider.items():
        assert options.env[key] == value


async def test_nothing_switches_claude_ai_connectors_back_on(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_CLAUDEAI_MCP_SERVERS", "true")
    backend = _backend()
    backend.attach_subprocess_env({"ENABLE_CLAUDEAI_MCP_SERVERS": "true"})
    options = await _build(backend)

    # The SDK layers ``options.env`` over the parent env, so this wins over both.
    assert options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


# ── tools off, malformed input, warm-client key ──────────────────────────────


async def test_a_tools_off_turn_keeps_its_empty_set_and_the_gate_refuses_all() -> None:
    options = await _build(tools_enabled=False)

    assert options.tools == []
    assert options.allowed_tools == []
    for name in ("Bash", "Read", _WIDGET_SPEC, "mcp__foo__anything"):
        assert not await _allows(options, name)
    assert await _allows(options, "ToolSearch")


@pytest.mark.parametrize(
    "input_data",
    [None, {}, {"tool_name": None}, {"tool_name": {"x": 1}}, {"tool_name": ""}, "Bash", 42],
)
async def test_the_gate_denies_malformed_input_instead_of_raising(input_data) -> None:
    """An exception in a hook tears down the CLI stream, so the gate fails closed."""
    options = await _build()

    out = await _gate(options)(input_data, None, None)

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_gate_rule() -> None:
    from pocketpaw.agents.claude_sdk import _tool_gate_allows

    allowed = frozenset({"Read", "mcp__srv__tool", "mcp__ext", "mcp__"})

    for name in ("Read", "mcp__srv__tool", "mcp__ext__anything", "ToolSearch", "WaitForMcpServers"):
        assert _tool_gate_allows(name, allowed), name
    for name in ("Bash", "mcp__srv__other", "mcp__srv__tool__x", "mcp__extra__x", "mcp____x"):
        assert not _tool_gate_allows(name, allowed), name
    for junk in (None, "", 42, {"x": 1}, ["Read"]):
        assert not _tool_gate_allows(junk, allowed), junk


async def test_the_warm_client_key_tells_apart_turns_whose_scope_differs() -> None:
    """The gate, the pinned built-ins and ``disallowed_tools`` are frozen into a
    warm client at connect(); a turn with a different scope must not reuse it."""

    backend = _backend()  # one backend, so cwd and model match across turns

    async def key(**scope: Any) -> str:
        return ClaudeSDKBackend._client_cache_key(await _build(backend, **scope), session_key="s1")

    base = await key()
    assert await key() == base, "an identical turn must still reuse the client"
    assert await key(deny_mcp_tool_ids=frozenset({_CLICK})) != base
    assert await key(deny_mcp_tool_ids=frozenset({"Bash"})) != base
    assert await key(tools_enabled=False) != base
    assert await key(exclusive_mcp_tools=True, allow_mcp_tool_ids=frozenset({_READ_FILE})) != base


# ── the regression guard: a broad surface loses nothing ──────────────────────


async def test_on_a_broad_surface_the_gate_allows_every_registered_tool() -> None:
    """Every tool the turn's in-process MCP servers actually expose must pass the
    gate. A tool that fails here worked before the gate (bypass approved it) and
    would silently stop working: its id is missing from ``allowed_tools``, so fix
    the provider's ``tool_ids()``, not the gate.

    Real registration, planner opted in, external config stubbed out (external
    servers have no in-process tool list; the bare-entry test covers them)."""
    from mcp import types

    from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_studio import STUDIO_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

    backend = _backend(
        policy=ToolPolicy(profile="full", mcp_servers_allow=frozenset({"pocketpaw_planner"}))
    )
    with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
        options = await _build(backend, pool=None)

    exposed: list[str] = []
    for server, cfg in sorted(options.mcp_servers.items()):
        if cfg.get("type") != "sdk":
            continue
        handler = cfg["instance"].request_handlers[types.ListToolsRequest]
        listed = await handler(types.ListToolsRequest(method="tools/list"))
        exposed += [f"mcp__{server}__{tool.name}" for tool in listed.root.tools]

    floor = len(WIDGET_TOOL_IDS) + len(ATLAS_TOOL_IDS) + len(STUDIO_TOOL_IDS)
    assert len(exposed) >= floor, f"enumerated only {exposed}; the guard measures nothing"
    refused = [t for t in exposed if not await _allows(options, t)]
    assert not refused, f"registered tools the gate would refuse: {refused}"
    assert await _allows(options, "ToolSearch")
