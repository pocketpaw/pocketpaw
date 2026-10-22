# tests/ee/agent/test_ask_mcp_server/test_mcp_tool.py — coverage for the
# in-process ``pocketpaw_ask`` MCP server (the interactive ask_user question
# tool). Registration assertions (server name, tool id namespacing, provider
# allowlist publication), pure-function handler tests (no network, no SDK) for
# the happy path + validation fail-soft, two wiring guards (the run_core id must
# equal ASK_USER_TOOL_ID; /sites must publish the id), and the run_core frame
# contract: canned backend events through the real ``_drive_agent_loop`` must
# yield exactly one ``ask_user_question`` frame per valid call (never for the
# claude_sdk ``input_pending`` placeholder, never for a call the handler would
# reject) and no generic ``tool_start`` chip for the ask tool.
"""MCP registration + handler tests for the ask_user interactive question tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.agent.mcp_servers import ask as ask_mcp  # noqa: E402


def _decode(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``."""
    assert "content" in envelope
    assert envelope["content"][0]["type"] == "text"
    return json.loads(envelope["content"][0]["text"])


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #


def test_server_name_and_tool_id_namespacing() -> None:
    assert ask_mcp.SERVER_NAME == "pocketpaw_ask"
    assert ask_mcp.ASK_USER_TOOL_ID == "mcp__pocketpaw_ask__ask_user"
    assert ask_mcp.ASK_TOOL_IDS == (ask_mcp.ASK_USER_TOOL_ID,)


def test_provider_publishes_tool_id() -> None:
    from pocketpaw_ee.extensions import CloudAskMcpProvider

    assert CloudAskMcpProvider().tool_ids() == [ask_mcp.ASK_USER_TOOL_ID]


def test_sites_allow_list_includes_ask_tool() -> None:
    # The per-surface allow-list is a hard whitelist; an id absent from
    # sites_allow is silently unreachable on /sites. Guard that wiring.
    from pocketpaw_ee.cloud.surface.surface_registry import _mcp_tool_ids

    ids = _mcp_tool_ids()
    if not ids.loaded:  # EE agent import degraded (SDK absent) — nothing to assert
        pytest.skip("MCP tool ids not loaded in this environment")
    assert ask_mcp.ASK_USER_TOOL_ID in ids.sites_allow


def test_run_core_emit_literal_matches_tool_id() -> None:
    # run_core spells the id as a literal to stay decoupled; guard against drift.
    from pocketpaw_ee.cloud.chat.runs.run_core import _ASK_USER_TOOL_ID

    assert _ASK_USER_TOOL_ID == ask_mcp.ASK_USER_TOOL_ID


# --------------------------------------------------------------------------- #
# Handler — happy path                                                         #
# --------------------------------------------------------------------------- #


async def test_ask_user_happy_path() -> None:
    out = await ask_mcp._ask_user_handler(
        {"question": "Which vibe?", "options": ["Clean & modern", "Warm & friendly"]}
    )
    assert "is_error" not in out
    body = _decode(out)
    assert body["ok"] is True
    assert body["shown"] is True
    assert body["question"] == "Which vibe?"
    assert body["options"] == ["Clean & modern", "Warm & friendly"]
    # The ack must tell the agent to stop and wait.
    assert "END your turn" in body["note"]


async def test_ask_user_normalizes_dict_options() -> None:
    out = await ask_mcp._ask_user_handler(
        {
            "question": "Pick one",
            "options": [{"label": "A"}, {"text": "B"}, {"value": "C"}, "D"],
        }
    )
    body = _decode(out)
    assert body["options"] == ["A", "B", "C", "D"]


async def test_ask_user_truncates_over_max_options() -> None:
    many = [f"opt{i}" for i in range(10)]
    out = await ask_mcp._ask_user_handler({"question": "Q?", "options": many})
    body = _decode(out)
    assert len(body["options"]) == ask_mcp._MAX_OPTIONS


# --------------------------------------------------------------------------- #
# Handler — validation fail-soft (never raises into the agent)                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [{}, {"question": ""}, {"question": "   "}])
async def test_ask_user_rejects_missing_question(bad: dict) -> None:
    out = await ask_mcp._ask_user_handler({**bad, "options": ["A", "B"]})
    assert out["is_error"] is True
    assert "question" in out["content"][0]["text"]


@pytest.mark.parametrize(
    "opts",
    [None, "not-a-list", [], ["only-one"], [""], [{"nope": "x"}]],
)
async def test_ask_user_rejects_bad_options(opts) -> None:
    out = await ask_mcp._ask_user_handler({"question": "Q?", "options": opts})
    assert out["is_error"] is True
    assert "options" in out["content"][0]["text"]


# --------------------------------------------------------------------------- #
# run_core: tool_use -> ask_user_question frame                               #
# --------------------------------------------------------------------------- #


async def _drive(monkeypatch, events: list[Any]) -> list[tuple[str, Any]]:
    """Run ``_drive_agent_loop`` over canned backend events (harness from
    tests/ee/agent/test_surfaces_mcp_server.py)."""
    from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
    from pocketpaw_ee.cloud.chat.runs import run_core

    class _FakePool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A", backend=None)

        def run(self, *_a, **_k):
            async def _gen():
                for ev in events:
                    yield ev

            return _gen()

    async def _empty(*_a, **_k):
        return ""

    async def _never_cancelled():
        return False

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _FakePool())
    monkeypatch.setattr(run_core, "build_knowledge_context", _empty)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)

    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    return [
        (name, data)
        async for name, data in run_core._drive_agent_loop(
            ctx,
            user_content="build me a site",
            attachments_in=None,
            mentions_in=None,
            history=None,
            is_cancelled=_never_cancelled,
            emit_stream_start=False,
        )
    ]


def _ask_use(tool_input: dict, *, pending: bool = False) -> SimpleNamespace:
    """A ``tool_use`` event in the claude_sdk backend's shape."""
    return SimpleNamespace(
        type="tool_use",
        content=f"Using {ask_mcp.ASK_USER_TOOL_ID}...",
        metadata={
            "name": ask_mcp.ASK_USER_TOOL_ID,
            "input": tool_input,
            "input_pending": pending,
        },
    )


_DONE = SimpleNamespace(type="done", content="")


def _frames(frames: list[tuple[str, Any]], kind: str) -> list[Any]:
    return [d for n, d in frames if n == kind]


async def test_pending_placeholder_then_real_call_emits_one_frame(monkeypatch) -> None:
    # claude_sdk announces every tool twice: a content_block_start placeholder
    # (input {} + input_pending) and then the AssistantMessage with real args.
    frames = await _drive(
        monkeypatch,
        [
            _ask_use({}, pending=True),
            _ask_use({"question": "Which vibe?", "options": ["Clean", "Warm"]}),
            _DONE,
        ],
    )
    assert _frames(frames, "ask_user_question") == [
        {"question": "Which vibe?", "options": ["Clean", "Warm"]}
    ]
    assert _frames(frames, "tool_start") == []


@pytest.mark.parametrize(
    "bad",
    [
        {"question": "Which vibe?", "options": ["Only one"]},
        {"question": "Which vibe?", "options": ["Clean", ""]},
        {"question": "", "options": ["Clean", "Warm"]},
        {"question": "   ", "options": ["Clean", "Warm"]},
        {"options": ["Clean", "Warm"]},
        {"question": "Which vibe?", "options": "Clean, Warm"},
        {"question": "Which vibe?"},
    ],
)
async def test_call_the_tool_rejects_emits_no_frame(monkeypatch, bad: dict) -> None:
    # The UI must never show a question the handler then refuses.
    rejected = await ask_mcp._ask_user_handler(bad)
    assert rejected["is_error"] is True
    frames = await _drive(monkeypatch, [_ask_use(bad), _DONE])
    assert _frames(frames, "ask_user_question") == []
    assert _frames(frames, "tool_start") == []


async def test_valid_call_emits_one_normalized_frame_and_no_tool_start(monkeypatch) -> None:
    many = [{"label": " A "}, {"text": "B"}, "", *[f"opt{i}" for i in range(10)]]
    frames = await _drive(
        monkeypatch,
        [_ask_use({"question": "  Pick one  ", "options": many}), _DONE],
    )
    ack = _decode(await ask_mcp._ask_user_handler({"question": "  Pick one  ", "options": many}))
    # The frame carries exactly what the handler acks: trimmed, flattened, capped.
    assert _frames(frames, "ask_user_question") == [
        {"question": ack["question"], "options": ack["options"]}
    ]
    assert len(ack["options"]) == ask_mcp._MAX_OPTIONS
    assert _frames(frames, "tool_start") == []
