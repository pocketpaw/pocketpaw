# test_code_mcp_server.py — the /code file tools (feat/code-mode-file-tools).
#
# Created 2026-07-22 (feat/code-mode-tool) for the single ``code_mode`` tool.
# Rewritten 2026-07-24 when that coarse tool was replaced by the four file verbs
# the MAIN agent drives — ``readFile`` / ``search`` / ``listDir`` / ``writeFile``.
# Each resolves the workspace from the per-stream ContextVars, delegates ONE call
# to CD-1's browser channel, and turns whatever comes back into an MCP response.
#
# Three properties carry most of the weight here.
#
# FIRST, a malformed call never reaches the browser. A missing path, a non-string
# query, an over-long rewrite — each is rejected as an MCP error before a delegate
# is parked, because parking on a bad call would burn the channel budget on
# something knowable at once.
#
# SECOND, no failure path raises. A raise inside an in-process tool handler
# reaches the model as a broken tool; an error RESPONSE gives it a true sentence
# to say. So a timeout, a dead stream, and a missing workspace all come back as
# ``is_error`` payloads carrying the channel's own message.
#
# THIRD, ``writeFile`` is honest about staging. It delegates the proposed content
# and relays the browser's staged-change sentence; the response is what the model
# reads, and it must not read as "the file was written".
#
# The delegate channel itself is not re-tested here (see
# tests/cloud/test_codeagent_delegates.py) — these drive the handlers against a
# stub and assert on what each does with the outcome.
from __future__ import annotations

import pytest
from pocketpaw_ee.agent.mcp_servers import code as code_mcp
from pocketpaw_ee.agent.mcp_servers.code import (
    CODE_TOOL_IDS,
    LIST_DIR_TOOL_ID,
    READ_FILE_TOOL_ID,
    SEARCH_TOOL_ID,
    SERVER_NAME,
    WRITE_FILE_TOOL_ID,
    _list_dir_handler,
    _read_file_handler,
    _search_handler,
    _write_file_handler,
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

    async def _stub(workspace_id, tool, tool_input):
        calls.append({"workspace_id": workspace_id, "tool": tool, "input": tool_input})
        return DelegateOutcome(ok=True, result={"output": "the file contents", "isError": False})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser",
        _stub,
    )
    return calls


def _text(response: dict) -> str:
    """Pull the relayed text back out of an MCP response."""
    return response["content"][0]["text"]


# ── the id contract with the /code SurfaceProfile ───────────────────────────


def test_tool_ids_match_the_literals_the_surface_profile_hardcodes():
    """The profile shipped before this module exported constants, so it spells
    the ids as literals. They are matched by STRING: a rename here scopes the
    /code surface to a tool nothing provides, and it fails silently — the agent
    simply has no way to reach the code. Pin both halves."""
    assert SERVER_NAME == "pocketpaw_code"
    assert READ_FILE_TOOL_ID == "mcp__pocketpaw_code__readFile"
    assert SEARCH_TOOL_ID == "mcp__pocketpaw_code__search"
    assert LIST_DIR_TOOL_ID == "mcp__pocketpaw_code__listDir"
    assert WRITE_FILE_TOOL_ID == "mcp__pocketpaw_code__writeFile"
    assert set(CODE_TOOL_IDS) == {
        READ_FILE_TOOL_ID,
        SEARCH_TOOL_ID,
        LIST_DIR_TOOL_ID,
        WRITE_FILE_TOOL_ID,
    }


def test_the_surface_profile_actually_carries_every_id():
    """The other end of the same contract, read from the real profile rather
    than restated. If either side drifts, this fails instead of the surface
    quietly losing a tool."""
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
    from pocketpaw_ee.cloud.surface.service import resolve_profile

    profile = resolve_profile(SurfaceKind.CODE, SurfaceMeta())
    assert profile.allow_mcp_tool_ids is not None
    for tool_id in CODE_TOOL_IDS:
        assert tool_id in profile.allow_mcp_tool_ids


@pytest.mark.asyncio
async def test_file_tools_reach_the_effective_allowlist_and_the_builtins_do_not():
    """The whole seam, end to end, through the REAL allowlist computation.

    Asserting both directions at once is the point. Presence alone would pass on
    a surface that also still had ``Bash``; absence alone was already green on a
    surface with no way to reach the code at all. Only together do they say the
    agent has exactly the file tools and nothing that addresses the wrong
    machine."""
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
    from pocketpaw_ee.cloud.surface.service import resolve_profile

    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings

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

    for tool_id in CODE_TOOL_IDS:
        assert tool_id in effective, (
            "the /code agent is missing a file tool. Either this server is not "
            "registered via the pocketpaw.mcp_servers entry point (a fresh "
            "checkout needs `uv sync --dev --group ee` to pick up the entry "
            "point), or an id drifted from the literal the profile allows. "
            f"Missing: {tool_id}. Effective MCP ids: "
            f"{sorted(t for t in effective if t.startswith('mcp__'))}"
        )
    for tool in ("Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent"):
        assert tool not in effective, f"{tool} addresses the backend's disk, not the user's project"
    assert not [t for t in effective if t.startswith("mcp__pocketpaw_daytona__")]


# ── each verb round-trips to the delegate with the right shape ──────────────


@pytest.mark.asyncio
async def test_read_file_delegates_and_relays(in_workspace, captured_delegate):
    response = await _read_file_handler({"path": "  src/App.tsx  "})

    assert captured_delegate == [
        {"workspace_id": WS, "tool": "readFile", "input": {"path": "src/App.tsx"}}
    ]
    assert not response.get("is_error")
    assert _text(response) == "the file contents"


@pytest.mark.asyncio
async def test_search_delegates_the_query(in_workspace, captured_delegate):
    await _search_handler({"query": "Sidebar"})
    assert captured_delegate == [
        {"workspace_id": WS, "tool": "search", "input": {"query": "Sidebar"}}
    ]


@pytest.mark.asyncio
async def test_list_dir_delegates_the_path(in_workspace, captured_delegate):
    await _list_dir_handler({"path": "src/components"})
    assert captured_delegate[0]["tool"] == "listDir"
    assert captured_delegate[0]["input"] == {"path": "src/components"}


@pytest.mark.asyncio
async def test_list_dir_allows_the_empty_root_path(in_workspace, captured_delegate):
    """Listing the project root is a normal request — the one verb where the
    empty string is a value, not a malformed argument."""
    await _list_dir_handler({"path": ""})
    assert captured_delegate == [{"workspace_id": WS, "tool": "listDir", "input": {"path": ""}}]


@pytest.mark.asyncio
async def test_write_file_delegates_the_full_content(in_workspace, captured_delegate):
    await _write_file_handler({"path": "src/button.tsx", "content": "export const B = 1;\n"})
    assert captured_delegate == [
        {
            "workspace_id": WS,
            "tool": "writeFile",
            "input": {"path": "src/button.tsx", "content": "export const B = 1;\n"},
        }
    ]


@pytest.mark.asyncio
async def test_write_file_relays_the_staged_sentence_as_success(in_workspace, monkeypatch):
    """writeFile stages a proposal; the browser answers with a sentence
    describing it. The handler relays that verbatim as a NON-error, so the model
    can repeat it — 'I've proposed…', not 'I wrote…'."""

    async def _staged(workspace_id, tool, tool_input):
        return DelegateOutcome(
            ok=True,
            result={
                "output": "Proposed 2 changes to `src/button.tsx` for review.",
                "isError": False,
            },
        )

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _staged
    )

    response = await _write_file_handler({"path": "src/button.tsx", "content": "x"})
    assert not response.get("is_error")
    assert "Proposed 2 changes" in _text(response)


@pytest.mark.asyncio
async def test_write_file_allows_blanking_a_file(in_workspace, captured_delegate):
    """Empty ``content`` is a legitimate rewrite (clearing a file), so it is
    required-present but allowed-empty — only a non-string is rejected."""
    await _write_file_handler({"path": "a.ts", "content": ""})
    assert captured_delegate[0]["input"] == {"path": "a.ts", "content": ""}


# ── malformed calls are rejected before they reach the browser ──────────────


@pytest.mark.parametrize(
    "handler,args",
    [
        pytest.param(_read_file_handler, {}, id="read-missing-path"),
        pytest.param(_read_file_handler, {"path": ""}, id="read-empty-path"),
        pytest.param(_read_file_handler, {"path": "   "}, id="read-whitespace-path"),
        pytest.param(_read_file_handler, {"path": 42}, id="read-non-string-path"),
        pytest.param(_search_handler, {}, id="search-missing-query"),
        pytest.param(_search_handler, {"query": ""}, id="search-empty-query"),
        pytest.param(_search_handler, {"query": None}, id="search-null-query"),
        pytest.param(_list_dir_handler, {"path": 3}, id="listdir-non-string-path"),
        pytest.param(_write_file_handler, {"content": "x"}, id="write-missing-path"),
        pytest.param(_write_file_handler, {"path": "a.ts"}, id="write-missing-content"),
        pytest.param(
            _write_file_handler, {"path": "a.ts", "content": 5}, id="write-non-string-content"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_call_is_rejected_without_reaching_the_browser(
    handler, args, in_workspace, captured_delegate
):
    response = await handler(args)
    assert response["is_error"] is True
    assert captured_delegate == [], "a malformed call must not park a delegate"


@pytest.mark.asyncio
async def test_an_oversized_write_is_rejected_with_advice(in_workspace, captured_delegate):
    response = await _write_file_handler({"path": "a.ts", "content": "x" * 200_000})
    assert response["is_error"] is True
    assert "too long" in _text(response)
    assert captured_delegate == []


# ── every failure is a response, never a raise ──────────────────────────────


@pytest.mark.asyncio
async def test_no_workspace_in_scope_is_an_error_not_a_crash(monkeypatch, captured_delegate):
    """Outside a cloud stream — a CLI run, a background job — there is no tenant
    to scope the return leg to, so the tool must decline rather than park."""
    monkeypatch.setattr(code_mcp, "_workspace_id", lambda: None)

    response = await _read_file_handler({"path": "a.ts"})

    assert response["is_error"] is True
    assert "no active workspace" in _text(response).lower()
    assert captured_delegate == []


@pytest.mark.asyncio
async def test_a_failed_delegate_relays_the_channels_own_message(in_workspace, monkeypatch):
    """The channel distinguishes "no browser attached" from "the browser was
    slow". The model needs that difference to say something true, so the handler
    passes the message through rather than substituting a generic one."""

    async def _timeout(workspace_id, tool, tool_input):
        return DelegateOutcome(
            ok=False,
            error="timeout",
            message="The browser did not finish the delegated task in 180s.",
        )

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _timeout
    )

    response = await _read_file_handler({"path": "a.ts"})

    assert response["is_error"] is True
    assert "did not finish" in _text(response)
    assert "180s" in _text(response)


@pytest.mark.asyncio
async def test_a_browser_side_error_result_becomes_an_error_response(in_workspace, monkeypatch):
    """``ok`` says the round trip completed; ``isError`` in the payload says the
    verb itself failed (a missing file, an unreadable path). That must surface to
    the model as an error it can act on, not as a successful read of an error
    string."""

    async def _iserror(workspace_id, tool, tool_input):
        return DelegateOutcome(
            ok=True, result={"output": "no such file: a.ts", "isError": True}
        )

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _iserror
    )

    response = await _read_file_handler({"path": "a.ts"})
    assert response["is_error"] is True
    assert "no such file" in _text(response)


@pytest.mark.asyncio
async def test_an_empty_success_output_is_still_a_success(in_workspace, monkeypatch):
    """An empty file, or a listing of an empty directory, is a real answer — not
    a failure. ``ok`` is the signal, not truthiness of the output."""

    async def _empty(workspace_id, tool, tool_input):
        return DelegateOutcome(ok=True, result={"output": "", "isError": False})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _empty
    )

    response = await _read_file_handler({"path": "empty.ts"})
    assert not response.get("is_error")
    assert _text(response) == ""


# ── the server assembles ────────────────────────────────────────────────────


def test_server_builds_with_the_four_file_tools():
    built = build_code_server()
    if built is None:
        pytest.skip("claude_agent_sdk not installed")
    name, server = built
    assert name == SERVER_NAME
    assert server is not None


def test_provider_reports_every_tool_id():
    """The entry-point provider is how the ids reach the SDK allowlist. A
    provider that builds but reports the wrong ids leaves tools uncallable."""
    from pocketpaw_ee.extensions import CloudCodeMcpProvider

    assert set(CloudCodeMcpProvider().tool_ids()) == set(CODE_TOOL_IDS)
