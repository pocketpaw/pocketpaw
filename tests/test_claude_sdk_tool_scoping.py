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

import re
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from pocketpaw.agents.claude_sdk import POCKET_CREATION_GRANT, ClaudeSDKBackend
from pocketpaw.ripple import POCKET_CREATION_PROMPT_MCP, POCKET_INTERACTION_PROMPT_MCP
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
# Composio's provider allowlists its whole server with the bare id.
_COMPOSIO = "mcp__composio"
_SEND_EMAIL = "mcp__composio__GMAIL_SEND_EMAIL"

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
# Pinned after the built-ins on every turn with tools: the CLI's own plumbing.
_INFRA = ["ToolSearch", "WaitForMcpServers"]
# The real prompt /api/v1/pockets/chat sends on a creation turn.
_POCKET = POCKET_CREATION_PROMPT_MCP


def _cli_spelling(name: str) -> str:
    """How Claude Code spells an MCP server name in a tool id (read off the CLI's
    own normalizer): every char outside ``[A-Za-z0-9_-]`` becomes ``_``, runs kept."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def _config(*names: str) -> list[SimpleNamespace]:
    """External MCP config rows, shaped like ``load_mcp_config`` returns them."""
    return [
        SimpleNamespace(
            name=name, transport="stdio", command="x", args=[], env={}, url="", enabled=True
        )
        for name in names
    ]


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
    external: tuple[str, ...] = (),
    provider_env: dict[str, str] | None = None,
    **scope: Any,
) -> ClaudeAgentOptions:
    """Build one turn's options. ``scope`` overrides the ``_build_options`` kwargs.

    ``pool=None`` keeps the backend's real MCP id collection AND real server
    registration (the parity test); otherwise both are stubbed. ``external`` is
    the raw names in the external MCP config, never the real ``~/.pocketpaw``
    file; a stubbed pool gains the bare entry the backend adds for each.
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
        stack.enter_context(
            patch("pocketpaw.mcp.config.load_mcp_config", return_value=_config(*external))
        )
        if pool is not None:
            ids = [*pool, *(f"mcp__{_cli_spelling(name)}" for name in external)]
            stack.enter_context(patch.object(backend, "_collect_mcp_tool_ids", return_value=ids))
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


async def test_a_denied_id_under_a_bare_server_grant_is_refused() -> None:
    """A pocket room saves ``deny_mcp_tool_ids=[GMAIL_SEND_EMAIL]``. Composio is
    allowlisted as the bare ``mcp__composio``, so subtracting the id from
    ``allowed_tools`` changes nothing: the gate itself must check the deny first."""
    options = await _build(
        pool=[*_POOL, _COMPOSIO],
        deny_mcp_tool_ids=frozenset({_SEND_EMAIL, "mcp__foo__drop"}),
    )

    assert not await _allows(options, _SEND_EMAIL)
    assert not await _allows(options, "mcp__foo__drop"), "same for an external server"
    assert await _allows(options, "mcp__composio__GMAIL_FETCH_EMAILS"), "only the id is denied"
    assert await _allows(options, "mcp__foo__keep")


async def test_a_bare_server_deny_refuses_every_tool_on_that_server() -> None:
    options = await _build(
        pool=[*_POOL, _COMPOSIO, _SEND_EMAIL], deny_mcp_tool_ids=frozenset({_COMPOSIO})
    )

    assert not await _allows(options, _SEND_EMAIL)
    assert _SEND_EMAIL not in options.allowed_tools, "the subtraction covers it too"
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


async def test_an_exclusive_turn_keeps_a_declared_id_on_a_server_granted_wholesale() -> None:
    """Composio and external servers reach the allowlist only as a bare entry, so a
    tool an exclusive agent declares by full id is never literally there. The
    filter kept literal matches only and dropped the declared tool."""
    options = await _build(
        pool=[*_POOL, _COMPOSIO],
        external=("My Notes",),
        exclusive_mcp_tools=True,
        allow_mcp_tool_ids=frozenset({_SEND_EMAIL, "mcp__My Notes__search", _READ_FILE}),
    )

    assert await _allows(options, _SEND_EMAIL)
    assert await _allows(options, "mcp__My_Notes__search")
    assert await _allows(options, _READ_FILE)
    assert not await _allows(options, "mcp__composio__GMAIL_DELETE_MESSAGE"), "the id only"
    assert not await _allows(options, "mcp__My_Notes__delete_note")
    assert _COMPOSIO not in options.allowed_tools


async def test_a_mode_allow_list_keeps_a_declared_id_on_a_server_granted_wholesale() -> None:
    options = await _build(allow_mcp_tool_ids=frozenset({"mcp__foo__lookup"}))

    assert await _allows(options, "mcp__foo__lookup")
    assert not await _allows(options, "mcp__foo__other"), "foo is not always allowed"


async def test_a_declared_id_does_not_bring_back_a_denied_one() -> None:
    options = await _build(
        pool=[*_POOL, _COMPOSIO],
        exclusive_mcp_tools=True,
        allow_mcp_tool_ids=frozenset({_SEND_EMAIL, "mcp__foo__lookup"}),
        deny_mcp_tool_ids=frozenset({_SEND_EMAIL, _EXTERNAL}),
    )

    assert _SEND_EMAIL not in options.allowed_tools
    assert "mcp__foo__lookup" not in options.allowed_tools, "its server grant was denied"


async def test_a_bare_server_entry_admits_that_server_and_no_other() -> None:
    options = await _build()

    assert await _allows(options, "mcp__foo__anything")
    assert not await _allows(options, "mcp__foobar__x")


def test_an_external_server_entry_is_spelled_the_way_the_cli_names_its_tools() -> None:
    """Claude Code names an MCP tool ``mcp__<server>__<tool>`` with every character
    of the server name outside ``[A-Za-z0-9_-]`` replaced by ``_``, runs kept. An
    entry built from the raw config name would never match, and the gate would
    refuse every tool on a server called, say, "My Notes.v2"."""
    names = ("My Notes.v2", "Google Drive (work)", "acme__crm")
    with patch("pocketpaw.mcp.config.load_mcp_config", return_value=_config(*names)):
        ids = _backend()._collect_mcp_tool_ids()

    for entry in ("mcp__My_Notes_v2", "mcp__Google_Drive__work_", "mcp__acme__crm"):
        assert entry in ids


@pytest.mark.parametrize(
    ("server", "tool_name"),
    [
        ("Google Drive (work)", "mcp__Google_Drive__work___search"),
        ("acme__crm", "mcp__acme__crm__list_deals"),
        ("My Notes.v2", "mcp__My_Notes_v2__search"),
        ("plugin:design:refero", "mcp__plugin_design_refero__search_styles"),
    ],
)
async def test_every_tool_on_an_external_server_passes_the_gate(
    server: str, tool_name: str
) -> None:
    """A spelled server name can carry ``__`` ("Google Drive (work)" becomes
    ``Google_Drive__work_``). The gate used to tell a bare server entry from a full
    tool id by counting ``__`` and refused every tool on two of these servers."""
    options = await _build(external=(server,))

    assert await _allows(options, tool_name)


async def test_a_server_name_with_a_double_underscore_grants_that_server_only() -> None:
    options = await _build(external=("acme__crm",))

    assert await _allows(options, "mcp__acme__crm__list_deals")
    assert not await _allows(options, "mcp__acme__other")
    assert not await _allows(options, "mcp__acme__crmx__list")


async def test_a_mode_grant_naming_an_external_server_by_its_raw_name_keeps_it() -> None:
    """/sites grants the servers in POCKETPAW_SITES_MCP_SERVERS as ``mcp__<raw
    name>``, and plugin servers are named ``plugin:<plugin>:<server>``. pydantic_ai
    matches the raw name, so the producers stay raw and this backend rewrites the
    token to the CLI's spelling before it filters on it."""
    options = await _build(
        external=("plugin:design:refero", "My Notes"),
        allow_mcp_tool_ids=frozenset({"mcp__plugin:design:refero", "mcp__My Notes", _RUN_SCENARIO}),
    )

    assert await _allows(options, "mcp__plugin_design_refero__search_styles")
    assert await _allows(options, "mcp__My_Notes__search")
    assert not await _allows(options, _CREATE_TASK), "the mode list still scopes the rest"


async def test_a_deny_or_allow_naming_an_external_server_by_its_raw_name_applies() -> None:
    options = await _build(
        external=("My Notes", "Google Drive (work)"),
        deny_mcp_tool_ids=frozenset({"mcp__My Notes__delete_note", "mcp__Google Drive (work)"}),
        allow_sdk_tools=frozenset({"mcp__My Notes__search"}),
    )

    assert not await _allows(options, "mcp__My_Notes__delete_note")
    assert await _allows(options, "mcp__My_Notes__search")
    assert not await _allows(options, "mcp__Google_Drive__work___search"), "a bare deny"
    assert "mcp__My_Notes__delete_note" in options.disallowed_tools, "the CLI's spelling"
    assert "mcp__My_Notes__search" in options.allowed_tools
    assert "mcp__My Notes__search" not in options.allowed_tools


# ── built-ins: pinned, so the CLI's other tools never reach the agent ────────


async def test_a_turn_pins_its_builtins_plus_the_infra_tools() -> None:
    options = await _build()

    assert options.tools == [*_BUILTINS, *_INFRA]
    for leaked in ("CronCreate", "SendMessage", "Workflow", "RemoteTrigger", "PushNotification"):
        assert leaked not in options.tools
        assert not await _allows(options, leaked)


async def test_every_infra_tool_the_gate_admits_is_pinned() -> None:
    """The CLI refuses a base tool that ``tools=`` does not name, so an infra tool
    the gate waves through but the pinned list omits can never run."""
    from pocketpaw.agents.claude_sdk import _INFRA_TOOLS

    options = await _build()

    assert list(_INFRA_TOOLS) == _INFRA
    for name in _INFRA_TOOLS:
        assert name in options.tools
        assert await _allows(options, name)


async def test_a_pocket_session_pins_only_delegation_web_and_skills() -> None:
    options = await _build(system_prompt=_POCKET)

    assert options.tools == ["Agent", "WebSearch", "WebFetch", "Skill", *_INFRA]
    assert not await _allows(options, "Bash")
    assert await _allows(options, _WIDGET_SPEC), "pocket MCP tools stay reachable"


async def test_a_pocket_creation_turn_can_load_the_create_pocket_skill() -> None:
    """The creation prompt calls the pocketpaw-create-pocket skill the preferred
    entry point; a lock without Skill refused it on every default creation turn."""
    assert "pocketpaw-create-pocket" in _POCKET
    options = await _build(system_prompt=_POCKET)

    assert "Skill" in options.tools
    assert await _allows(options, "Skill")
    assert not await _allows(options, "Bash"), "still a pocket session"


async def test_the_real_pocket_prompts_open_a_pocket_session() -> None:
    interaction = f"persona\n\n{POCKET_INTERACTION_PROMPT_MCP}"
    for prompt in (_POCKET, interaction):
        options = await _build(system_prompt=prompt)

        assert "Bash" not in options.tools
        assert not await _allows(options, "Bash")


@pytest.mark.parametrize(
    "prompt",
    [
        "identity\n\n# Key Knowledge\n- pocket prompts open with a <pocket-scope> block\n",
        "identity\nWrap the scope in <pocket-scope> tags when the user asks.",
        "<pocket-scope>p1</pocket-scope>\nidentity",
    ],
)
async def test_a_prompt_that_only_mentions_the_tag_is_not_a_pocket_session(prompt: str) -> None:
    """The tag turns up in soul knowledge lines and agent instructions. Only the
    scope block the pocket prompts emit, its tag alone on a line, locks a turn."""
    options = await _build(system_prompt=prompt)

    assert "Bash" in options.tools
    assert await _allows(options, "Bash")


async def test_the_pinned_builtins_are_policy_filtered() -> None:
    backend = _backend(policy=ToolPolicy(profile="full", deny=["shell"]))
    options = await _build(backend, system_prompt=_POCKET)

    assert options.tools == ["WebSearch", "WebFetch", "Skill", *_INFRA]
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

    matchers = [m.matcher for m in options.hooks["PreToolUse"] if m.matcher is not None]
    # The CLI reads a matcher of plain names split on "|" as an exact-name list.
    assert matchers == ["Bash|PowerShell"]


# ── Windows: the shell can be PowerShell ─────────────────────────────────────


async def test_on_windows_powershell_is_pinned_and_allowed_with_bash() -> None:
    """Without Git Bash the CLI's only shell on Windows is the ``PowerShell`` tool.
    A pinned list naming only "Bash" withheld it, and the gate refused it."""
    with patch("sys.platform", "win32"):
        options = await _build()

    assert "Bash" in options.tools
    assert "PowerShell" in options.tools
    assert await _allows(options, "PowerShell")


async def test_on_windows_a_shell_deny_holds_powershell_too() -> None:
    """/sites and /code deny the shell by the bare name "Bash"."""
    with patch("sys.platform", "win32"):
        denied = await _build(deny_mcp_tool_ids=frozenset({"Bash"}))
        no_shell = await _build(_backend(policy=ToolPolicy(profile="full", deny=["shell"])))
        pocket = await _build(system_prompt=_POCKET)

    for options in (denied, no_shell, pocket):
        assert "PowerShell" not in options.tools
        assert not await _allows(options, "PowerShell")


async def test_powershell_stays_off_other_platforms() -> None:
    with patch("sys.platform", "darwin"):
        options = await _build()

    assert "PowerShell" not in options.tools
    assert not await _allows(options, "PowerShell")


def test_powershell_is_in_the_shell_policy_group() -> None:
    assert ClaudeSDKBackend._TOOL_POLICY_MAP["PowerShell"] == "shell"


async def test_the_dangerous_command_hook_checks_powershell_commands() -> None:
    call = {"tool_name": "PowerShell", "tool_input": {"command": "rm -rf /"}}

    out = await _backend()._block_dangerous_hook(call, None, None)

    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


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
    from pocketpaw.agents.claude_sdk import _server_grant_prefixes, _tool_gate_allows

    allowed = frozenset({"Read", "mcp__srv__tool", "mcp__ext", "mcp__", "mcp__acme__crm"})
    # Without the config, ``mcp__acme__crm`` reads as a full tool id: no grant.
    assert _server_grant_prefixes(allowed, frozenset()) == ("mcp__ext__",)
    grants = _server_grant_prefixes(allowed, frozenset({"mcp__acme__crm"}))
    assert grants == ("mcp__acme__crm__", "mcp__ext__")

    allows = ("Read", "mcp__srv__tool", "mcp__ext__anything", "mcp__acme__crm__x")
    for name in (*allows, "ToolSearch", "WaitForMcpServers"):
        assert _tool_gate_allows(name, allowed, grants=grants), name
    refuses = ("Bash", "mcp__srv__other", "mcp__srv__tool__x", "mcp__extra__x", "mcp____x")
    for name in (*refuses, "mcp__acme__x"):
        assert not _tool_gate_allows(name, allowed, grants=grants), name
    for junk in (None, "", 42, {"x": 1}, ["Read"]):
        assert not _tool_gate_allows(junk, allowed, grants=grants), junk


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


async def test_a_deny_the_allowlist_cannot_show_still_changes_the_warm_client_key() -> None:
    """The deny saved mid-session names an id under the bare ``mcp__composio``
    grant, so ``allowed_tools`` is identical before and after. A key built from
    ``allowed_tools`` alone reused the warm client, whose gate never saw the deny."""
    backend = _backend()
    pool = [*_POOL, _COMPOSIO]

    async def key(deny: frozenset[str]) -> tuple[list[str], str]:
        options = await _build(backend, pool=pool, deny_mcp_tool_ids=deny)
        return options.allowed_tools, ClaudeSDKBackend._client_cache_key(options, session_key="s1")

    tools_before, before = await key(frozenset())
    tools_after, after = await key(frozenset({_SEND_EMAIL}))

    assert tools_after == tools_before, "precondition: the deny is invisible in allowed_tools"
    assert after != before


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
