# tests/ee/agent/test_ask_mcp_server/test_mcp_tool.py
# Created: 2026-07-07 (feat/sites-ask-user-ui) — coverage for the in-process
# ``pocketpaw_ask`` MCP server (the interactive ask_user question tool). Mirrors
# the palette / icons test layout: registration assertions (server name, tool id
# namespacing, provider allowlist publication), pure-function handler tests (no
# network, no SDK) for the happy path + validation fail-soft, plus two wiring
# guards — the run_core emit literal must equal ASK_USER_TOOL_ID (drift), and the
# /sites allow-list must publish the ask tool id so it is actually callable.
"""MCP registration + handler tests for the ask_user interactive question tool."""

from __future__ import annotations

import json

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
