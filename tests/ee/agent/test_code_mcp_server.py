# test_code_mcp_server.py — the ``code_mode`` in-process MCP tool (CD-2).
#
# Created 2026-07-22 (feat/code-mode-tool). Covers the single tool the main chat
# agent uses to reach the user's project: it resolves the workspace from the
# per-stream ContextVars, hands the task to CD-1's delegate channel, and turns
# whatever comes back into an MCP response.
#
# Two properties carry most of the weight here.
#
# FIRST, the mode default fails SAFE. Every way of not-saying-a-mode — omitted,
# null, empty, misspelled, wrong type — has to land on read-only ``ask``. The
# failure mode of a forgotten field must be "cannot edit", never "can edit
# unexpectedly", so these are enumerated rather than sampled.
#
# SECOND, no failure path raises. A raise inside an in-process tool handler
# reaches the model as a broken tool; an error RESPONSE gives it a true sentence
# to say. So a timeout, a dead stream, and a missing workspace all have to come
# back as ``is_error`` payloads carrying the channel's own message.
#
# The delegate channel itself is not re-tested here (see
# tests/cloud/test_codeagent_delegates.py) — these drive the handler against a
# stub and assert on what the handler does with the outcome.
from __future__ import annotations

import json

import pytest
from pocketpaw_ee.agent.mcp_servers import code as code_mcp
from pocketpaw_ee.agent.mcp_servers.code import (
    CODE_MODE_TOOL_ID,
    CODE_TOOL_IDS,
    SERVER_NAME,
    _code_mode_handler,
    build_code_server,
)
from pocketpaw_ee.cloud.codeagent.delegates import DelegateOutcome

WS = "ws-1"


@pytest.fixture
def in_workspace(monkeypatch):
    """Put a workspace in scope, as the cloud chat stream would."""
    monkeypatch.setattr(code_mcp, "_workspace_id", lambda: WS)


@pytest.fixture
def captured_delegate(monkeypatch):
    """Replace the delegate channel with a stub that records its arguments and
    returns a successful outcome. Patched at the SOURCE module, because the
    handler imports it inside the function body to avoid an import cycle."""
    calls: list[dict] = []

    async def _stub(workspace_id, task, mode):
        calls.append({"workspace_id": workspace_id, "task": task, "mode": mode})
        return DelegateOutcome(ok=True, result={"summary": "done", "filesRead": ["a.ts"]})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_to_browser",
        _stub,
    )
    return calls


def _payload(response: dict) -> dict:
    """Pull the JSON body back out of an MCP success response."""
    return json.loads(response["content"][0]["text"])


# ── the id contract with the /code SurfaceProfile ───────────────────────────


def test_tool_id_matches_the_literal_the_surface_profile_hardcodes():
    """CD-3 shipped first and could not import this constant, so it spelled the
    id as a literal. They are matched by STRING: a rename here scopes the /code
    surface to a tool nothing provides, and it fails silently — the agent simply
    has no way to reach the code. Pin both halves."""
    assert SERVER_NAME == "pocketpaw_code"
    assert CODE_MODE_TOOL_ID == "mcp__pocketpaw_code__code_mode"
    assert CODE_TOOL_IDS == (CODE_MODE_TOOL_ID,)


def test_the_surface_profile_actually_carries_this_id():
    """The other end of the same contract, read from the real profile rather
    than restated. If either side drifts, this fails instead of the surface
    quietly losing its only tool."""
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
    from pocketpaw_ee.cloud.surface.service import resolve_profile

    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    assert profile.allow_mcp_tool_ids is not None
    assert CODE_MODE_TOOL_ID in profile.allow_mcp_tool_ids


@pytest.mark.asyncio
async def test_code_mode_reaches_the_effective_allowlist_and_the_builtins_do_not():
    """The whole seam, end to end, through the REAL allowlist computation.

    Its sibling in ``tests/cloud/surface/test_studio_code_handlers.py`` could
    only assert ABSENCE — when CD-3 shipped, ``allow_mcp_tool_ids`` was a filter
    over tools that existed, and no server provided ``code_mode`` yet, so the id
    could not appear no matter how correct the profile was. This file is the
    change that makes it appear, so this is where presence gets pinned.

    Asserting both directions at once is the point. Presence alone would pass on
    a surface that also still had ``Bash``; absence alone was already green on a
    surface with no way to reach the code at all. Only together do they say the
    agent has exactly one door and it is the right one."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
    from pocketpaw_ee.cloud.surface.service import resolve_profile

    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    built = await ClaudeSDKBackend(get_settings())._build_options(
        "refactor this",
        system_prompt="you are on the code surface",
        history=None,
        session_key=None,
        deny_mcp_tool_ids=profile.deny_mcp_tool_ids,
        allow_sdk_tools=profile.allowed_sdk_tools or frozenset(),
        allow_mcp_tool_ids=profile.allow_mcp_tool_ids,
        skill_names=profile.skill_names,
        stderr_sink=[],
    )
    effective = set(built.options_kwargs["allowed_tools"])

    assert CODE_MODE_TOOL_ID in effective, (
        "the /code agent has no way to reach the user's code. Either this "
        "server is not registered via the pocketpaw.mcp_servers entry point "
        "(a fresh checkout needs `uv sync --dev --group ee` to pick up a NEW "
        "entry point), or the id drifted from the literal the profile allows. "
        f"Effective MCP ids: {sorted(t for t in effective if t.startswith('mcp__'))}"
    )
    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent"):
        assert tool not in effective, f"{tool} addresses the backend's disk, not the user's project"
    assert not [t for t in effective if t.startswith("mcp__pocketpaw_daytona__")]


# ── the round trip ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_round_trips_to_the_delegate_and_back(in_workspace, captured_delegate):
    response = await _code_mode_handler({"task": "add a loading state", "mode": "edit"})

    assert captured_delegate == [
        {"workspace_id": WS, "task": "add a loading state", "mode": "edit"}
    ]
    assert not response.get("is_error")
    assert _payload(response) == {"summary": "done", "filesRead": ["a.ts"]}


@pytest.mark.asyncio
async def test_task_is_stripped_before_it_crosses_the_wire(in_workspace, captured_delegate):
    await _code_mode_handler({"task": "  fix the retry logic \n"})
    assert captured_delegate[0]["task"] == "fix the retry logic"


# ── the mode default fails safe ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"task": "t"}, id="omitted"),
        pytest.param({"task": "t", "mode": None}, id="null"),
        pytest.param({"task": "t", "mode": ""}, id="empty"),
        pytest.param({"task": "t", "mode": "   "}, id="whitespace"),
        pytest.param({"task": "t", "mode": "write"}, id="hallucinated"),
        pytest.param({"task": "t", "mode": "EDIT_ALL"}, id="unrecognized"),
        pytest.param({"task": "t", "mode": 1}, id="int"),
        pytest.param({"task": "t", "mode": True}, id="bool"),
        pytest.param({"task": "t", "mode": ["edit"]}, id="list"),
    ],
)
@pytest.mark.asyncio
async def test_every_way_of_not_saying_edit_resolves_to_ask(
    args, in_workspace, captured_delegate
):
    """The one direction that matters. A model that garbles this field must lose
    the ability to edit, not gain it."""
    await _code_mode_handler(args)
    assert captured_delegate[0]["mode"] == "ask"


@pytest.mark.parametrize("raw", ["edit", "EDIT", " Edit "])
@pytest.mark.asyncio
async def test_edit_is_honoured_when_actually_asked_for(raw, in_workspace, captured_delegate):
    """The safe default must not be so eager that it swallows a real request —
    otherwise /code could never edit anything."""
    await _code_mode_handler({"task": "t", "mode": raw})
    assert captured_delegate[0]["mode"] == "edit"


# ── every failure is a response, never a raise ──────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({}, id="missing"),
        pytest.param({"task": None}, id="null"),
        pytest.param({"task": ""}, id="empty"),
        pytest.param({"task": "   "}, id="whitespace"),
        pytest.param({"task": 42}, id="int"),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_task_is_rejected_without_reaching_the_browser(
    args, in_workspace, captured_delegate
):
    response = await _code_mode_handler(args)
    assert response["is_error"] is True
    assert captured_delegate == [], "a malformed call must not park a delegate"


@pytest.mark.asyncio
async def test_an_oversized_task_is_rejected_with_advice(in_workspace, captured_delegate):
    response = await _code_mode_handler({"task": "x" * 9000})
    assert response["is_error"] is True
    assert "too long" in response["content"][0]["text"]
    assert captured_delegate == []


@pytest.mark.asyncio
async def test_no_workspace_in_scope_is_an_error_not_a_crash(monkeypatch, captured_delegate):
    """Outside a cloud stream — a CLI run, a background job — there is no tenant
    to scope the return leg to, so the tool must decline rather than park."""
    monkeypatch.setattr(code_mcp, "_workspace_id", lambda: None)

    response = await _code_mode_handler({"task": "t"})

    assert response["is_error"] is True
    assert "no active workspace" in response["content"][0]["text"].lower()
    assert captured_delegate == []


@pytest.mark.asyncio
async def test_a_failed_delegate_relays_the_channels_own_message(in_workspace, monkeypatch):
    """The channel distinguishes "no browser attached" from "the browser was
    slow". The model needs that difference to say something true, so the handler
    must pass the message through rather than substituting a generic one."""

    async def _timeout(workspace_id, task, mode):
        return DelegateOutcome(
            ok=False,
            error="timeout",
            message="The browser did not finish the delegated task in 180s.",
        )

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_to_browser", _timeout
    )

    response = await _code_mode_handler({"task": "t"})

    assert response["is_error"] is True
    assert "did not finish" in response["content"][0]["text"]
    assert "180s" in response["content"][0]["text"]


@pytest.mark.asyncio
async def test_a_failed_delegate_with_no_message_still_says_something(in_workspace, monkeypatch):
    async def _bare(workspace_id, task, mode):
        return DelegateOutcome(ok=False, error="aborted", message="")

    monkeypatch.setattr("pocketpaw_ee.cloud.codeagent.delegates.delegate_to_browser", _bare)

    response = await _code_mode_handler({"task": "t"})
    assert response["is_error"] is True
    assert response["content"][0]["text"].strip() != "Error:"


@pytest.mark.asyncio
async def test_an_empty_success_result_is_still_a_success(in_workspace, monkeypatch):
    """``ok`` is the signal, not truthiness of the payload. A sub-agent that
    answered with nothing did not fail."""

    async def _empty(workspace_id, task, mode):
        return DelegateOutcome(ok=True, result={})

    monkeypatch.setattr("pocketpaw_ee.cloud.codeagent.delegates.delegate_to_browser", _empty)

    response = await _code_mode_handler({"task": "t"})
    assert not response.get("is_error")
    assert _payload(response) == {}


# ── the server assembles ────────────────────────────────────────────────────


def test_server_builds_with_exactly_one_tool():
    built = build_code_server()
    if built is None:
        pytest.skip("claude_agent_sdk not installed")
    name, server = built
    assert name == SERVER_NAME
    assert server is not None


def test_provider_reports_the_tool_id():
    """The entry-point provider is how the id reaches the SDK allowlist. A
    provider that builds but reports no ids leaves the tool uncallable."""
    from pocketpaw_ee.extensions import CloudCodeMcpProvider

    assert CloudCodeMcpProvider().tool_ids() == [CODE_MODE_TOOL_ID]
