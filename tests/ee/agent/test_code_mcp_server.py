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
# THIRD, ``writeFile`` relays what the browser says it did. It delegates the
# content and hands the browser's own sentence back verbatim; that sentence is
# the model's only evidence of what happened, so the handler must not embellish
# it in either direction. Until 2026-07-25 that meant guarding against a write
# reading as done when it had only been staged for review; the review gate is
# gone and the sentence is now a plain past-tense "Wrote …".
#
# The delegate channel itself is not re-tested here (see
# tests/cloud/test_codeagent_delegates.py) — these drive the handlers against a
# stub and assert on what each does with the outcome.
#
# Extended 2026-07-24 (CX-1) with three ``_build_options`` cases proving the
# ``exclusive_mcp_tools`` cap: (a) exclusive + a declared allow-list yields
# EXACTLY those ids (grant suppressed), (b) exclusive + None strips ALL mcp ids,
# (c) the default path still applies the pocket/widget/atlas grant. A
# deterministic candidate pool is injected so the suppression is provable.
from __future__ import annotations

import pytest
from pocketpaw_ee.agent.mcp_servers import code as code_mcp
from pocketpaw_ee.agent.mcp_servers.code import (
    CODE_TOOL_IDS,
    EDIT_FILE_TOOL_ID,
    LIST_DIR_TOOL_ID,
    READ_FILE_TOOL_ID,
    SEARCH_TOOL_ID,
    SERVER_NAME,
    WRITE_FILE_TOOL_ID,
    _edit_file_handler,
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
    assert EDIT_FILE_TOOL_ID == "mcp__pocketpaw_code__editFile"
    assert WRITE_FILE_TOOL_ID == "mcp__pocketpaw_code__writeFile"
    assert set(CODE_TOOL_IDS) == {
        READ_FILE_TOOL_ID,
        SEARCH_TOOL_ID,
        LIST_DIR_TOOL_ID,
        EDIT_FILE_TOOL_ID,
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


# ── exclusive_mcp_tools caps the surface to exactly the declared ids (CX-1) ──


def _mcp_ids(built) -> set[str]:
    """The ``mcp__*`` ids that survived ``_build_options`` scoping."""
    return {t for t in built.options_kwargs["allowed_tools"] if t.startswith("mcp__")}


async def _build_with(backend, *, allow, exclusive):
    """Run ``_build_options`` with a deterministic candidate MCP pool.

    The pool is monkeypatched (via the fixture) to contain the four code file
    ids PLUS one widget id, one atlas id, and one planner (grant) id — so the
    suppression is provable: an exclusive turn must strip the grant ids that a
    default turn keeps. A test where the grant id was never a candidate would
    prove nothing."""
    return await backend._build_options(
        "refactor this",
        system_prompt="you are on the code surface",
        history=None,
        session_key=None,
        deny_mcp_tool_ids=frozenset(),
        allow_sdk_tools=frozenset(),
        allow_mcp_tool_ids=allow,
        skill_names=frozenset(),
        stderr_sink=[],
        exclusive_mcp_tools=exclusive,
    )


@pytest.fixture
def backend_with_grant_pool(monkeypatch):
    """A backend whose ``_collect_mcp_tool_ids`` yields a deterministic pool:
    the four code file ids + a widget id + an atlas id + a planner grant id."""
    from pocketpaw_ee.cloud.surface.surface_registry import _CODE_FILE_TOOL_IDS

    from pocketpaw.agents.claude_sdk import POCKET_CREATION_GRANT, ClaudeSDKBackend
    from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
    from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS
    from pocketpaw.config import get_settings

    widget_id = next(iter(WIDGET_TOOL_IDS))
    atlas_id = next(iter(ATLAS_TOOL_IDS))
    planner_id = "mcp__pocketpaw_pocket_planner__plan_pocket"
    assert planner_id in POCKET_CREATION_GRANT

    pool = [*sorted(_CODE_FILE_TOOL_IDS), widget_id, atlas_id, planner_id]
    backend = ClaudeSDKBackend(get_settings())
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: list(pool))
    return backend, widget_id, atlas_id, planner_id


@pytest.mark.asyncio
async def test_exclusive_caps_to_exactly_the_declared_ids(backend_with_grant_pool):
    """(a) exclusive + a declared allow-list ⇒ the effective MCP surface is
    EXACTLY those ids; the universal pocket/widget/atlas grant is NOT unioned
    back in."""
    from pocketpaw_ee.cloud.surface.surface_registry import _CODE_FILE_TOOL_IDS

    backend, widget_id, atlas_id, planner_id = backend_with_grant_pool
    built = await _build_with(backend, allow=_CODE_FILE_TOOL_IDS, exclusive=True)

    assert _mcp_ids(built) == set(_CODE_FILE_TOOL_IDS)
    # the grant ids that were candidates are gone
    assert widget_id not in _mcp_ids(built)
    assert atlas_id not in _mcp_ids(built)
    assert planner_id not in _mcp_ids(built)
    assert not [t for t in _mcp_ids(built) if t.startswith("mcp__pocketpaw_pocket")]


@pytest.mark.asyncio
async def test_exclusive_with_no_allowlist_strips_all_mcp(backend_with_grant_pool):
    """(b) exclusive + ``allow_mcp_tool_ids=None`` ⇒ the empty permitted set
    strips EVERY ``mcp__`` id. This is the precedence rule that lets an
    exclusive agent win over even a broad surface."""
    backend, *_ = backend_with_grant_pool
    built = await _build_with(backend, allow=None, exclusive=True)

    assert _mcp_ids(built) == set()


@pytest.mark.asyncio
async def test_default_path_still_applies_the_grant(backend_with_grant_pool):
    """(c) the DEFAULT path (signal off) is unchanged: the grant still applies,
    so a widget id and an atlas id present in the candidate pool SURVIVE the
    same allow-list that case (a) capped away."""
    from pocketpaw_ee.cloud.surface.surface_registry import _CODE_FILE_TOOL_IDS

    backend, widget_id, atlas_id, planner_id = backend_with_grant_pool
    built = await _build_with(backend, allow=_CODE_FILE_TOOL_IDS, exclusive=False)

    effective = _mcp_ids(built)
    # the declared ids are still there
    assert set(_CODE_FILE_TOOL_IDS) <= effective
    # and the grant ids that (a) stripped now survive — proving suppression
    assert widget_id in effective
    assert atlas_id in effective
    assert planner_id in effective


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
async def test_write_file_relays_the_browsers_sentence_as_success(in_workspace, monkeypatch):
    """The browser writes the file and answers with a sentence saying so. The
    handler relays that verbatim as a NON-error, so the model repeats what
    actually happened rather than a phrasing this layer invented."""

    async def _wrote(workspace_id, tool, tool_input):
        return DelegateOutcome(
            ok=True,
            result={"output": "Wrote `src/button.tsx`.", "isError": False},
        )

    monkeypatch.setattr("pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _wrote)

    response = await _write_file_handler({"path": "src/button.tsx", "content": "x"})
    assert not response.get("is_error")
    assert "Wrote `src/button.tsx`." in _text(response)


# ── readFile paging + editFile (fix/code-truncated-read-destroys-file) ──────
#
# The browser caps a read at 30_000 characters. Until 2026-07-28 that cap had no
# counterpart on this side: ``writeFile`` asked for a file's ENTIRE new contents,
# so on a larger file the model's only way to comply was to invent the part it
# had not been shown — reported from a live session as the agent "fabricating".
# ``offset`` lets it read the rest; ``editFile`` lets it change a file it never
# held. The tests below pin what actually crosses to the browser, since that is
# the half this module owns.


@pytest.mark.asyncio
async def test_read_file_forwards_a_positive_offset(in_workspace, captured_delegate):
    await _read_file_handler({"path": "big.ts", "offset": 30000})
    assert captured_delegate[0]["input"] == {"path": "big.ts", "offset": 30000}


@pytest.mark.asyncio
async def test_read_file_omits_offset_when_absent_or_zero(in_workspace, captured_delegate):
    """A read from the start sends no ``offset`` at all, so the common call is
    byte-identical to what it was before paging existed."""
    await _read_file_handler({"path": "a.ts"})
    await _read_file_handler({"path": "a.ts", "offset": 0})
    assert captured_delegate[0]["input"] == {"path": "a.ts"}
    assert captured_delegate[1]["input"] == {"path": "a.ts"}


@pytest.mark.asyncio
async def test_read_file_ignores_a_nonsense_offset_rather_than_failing(
    in_workspace, captured_delegate
):
    """A bad offset must not cost the model a turn. It reads from the start —
    the browser reports which window it actually returned, so the model can
    correct itself from the answer instead of from an error."""
    await _read_file_handler({"path": "a.ts", "offset": "banana"})
    assert captured_delegate[0]["input"] == {"path": "a.ts"}


@pytest.mark.asyncio
async def test_edit_file_delegates_the_exact_span(in_workspace, captured_delegate):
    await _edit_file_handler(
        {"path": "big.ts", "oldString": "const a = 1;", "newString": "const a = 2;"}
    )
    assert captured_delegate == [
        {
            "workspace_id": WS,
            "tool": "editFile",
            "input": {
                "path": "big.ts",
                "oldString": "const a = 1;",
                "newString": "const a = 2;",
            },
        }
    ]


@pytest.mark.asyncio
async def test_edit_file_allows_an_empty_new_string(in_workspace, captured_delegate):
    """An empty ``newString`` is how a span is DELETED, so it is required-present
    and allowed-empty — the same asymmetry ``writeFile``'s content has."""
    await _edit_file_handler({"path": "a.ts", "oldString": "dead code\n", "newString": ""})
    assert captured_delegate[0]["input"]["newString"] == ""


@pytest.mark.asyncio
async def test_edit_file_rejects_an_empty_old_string_before_the_browser(
    in_workspace, captured_delegate
):
    """An empty ``oldString`` matches everywhere and nowhere. Refusing here keeps
    a meaningless edit off the wire entirely."""
    response = await _edit_file_handler({"path": "a.ts", "oldString": "", "newString": "x"})
    assert response.get("is_error")
    assert captured_delegate == []


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

    monkeypatch.setattr("pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _timeout)

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
        return DelegateOutcome(ok=True, result={"output": "no such file: a.ts", "isError": True})

    monkeypatch.setattr("pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _iserror)

    response = await _read_file_handler({"path": "a.ts"})
    assert response["is_error"] is True
    assert "no such file" in _text(response)


@pytest.mark.asyncio
async def test_an_empty_success_output_is_still_a_success(in_workspace, monkeypatch):
    """An empty file, or a listing of an empty directory, is a real answer — not
    a failure. ``ok`` is the signal, not truthiness of the output."""

    async def _empty(workspace_id, tool, tool_input):
        return DelegateOutcome(ok=True, result={"output": "", "isError": False})

    monkeypatch.setattr("pocketpaw_ee.cloud.codeagent.delegates.delegate_call_to_browser", _empty)

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
