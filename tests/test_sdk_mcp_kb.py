# tests/test_sdk_mcp_kb.py
# Created: 2026-05-14 (feat/ripple-recipes-poc) — covers the
# find_recipe MCP handler that explicitly queries kb-go for the
# bundled ripple-recipes scope. Companion to the auto-injection
# path in bootstrap.context_builder._get_kb_context — this surface
# is what the chat agent CALLS, the other is what the orchestrator
# silently injects.
"""Tests for ``pocketpaw.agents.sdk_mcp_kb._find_recipe_handler``.

The handler shells out ``kb search --json`` and translates the
result into MCP content. We exercise the handler with a mocked
``_run_kb_search`` so the tests don't need a kb-go binary or a
populated scope to run in CI.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_empty_query_returns_error_envelope() -> None:
    """find_recipe(query='') must surface a clear error rather than
    shelling out to kb-go with an empty query — kb-go would just
    return zero hits but the error message is more useful to the
    agent than 'no results'."""
    from pocketpaw.agents.sdk_mcp_kb import _find_recipe_handler

    out = await _find_recipe_handler({"query": ""})
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "non-empty" in text
    assert "intent" in text


@pytest.mark.asyncio
async def test_subprocess_error_surfaces_to_agent() -> None:
    """When kb-go subprocess fails (binary missing, timeout, exit
    non-zero), the handler returns is_error=True with the failure
    reason so the agent can fall back to first-principles drafting
    instead of looping on the broken tool."""
    from pocketpaw.agents.sdk_mcp_kb import _find_recipe_handler

    with patch(
        "pocketpaw.agents.sdk_mcp_kb._run_kb_search",
        new=AsyncMock(return_value={"error": "kb binary not found on PATH"}),
    ):
        out = await _find_recipe_handler({"query": "sales pipeline dashboard"})
    assert out.get("is_error") is True
    text = out["content"][0]["text"]
    assert "kb binary not found" in text
    assert "first principles" in text


@pytest.mark.asyncio
async def test_no_matches_returns_first_principles_hint() -> None:
    """Empty results list isn't an error — it just means the
    recipe library doesn't cover this brief. Agent should draft
    from first principles using the pocket-creator guidance."""
    from pocketpaw.agents.sdk_mcp_kb import _find_recipe_handler

    with patch(
        "pocketpaw.agents.sdk_mcp_kb._run_kb_search",
        new=AsyncMock(return_value={"results": []}),
    ):
        out = await _find_recipe_handler({"query": "rare brief no recipe ships for"})
    assert out.get("is_error") is None or out["is_error"] is False
    text = out["content"][0]["text"]
    assert "No recipes matched" in text
    assert "first principles" in text


@pytest.mark.asyncio
async def test_match_returns_structured_results() -> None:
    """When kb-go finds a match the handler passes the structured
    payload through as JSON-stringified content — the agent parses
    it to read title / summary / content / concepts."""
    from pocketpaw.agents.sdk_mcp_kb import _find_recipe_handler

    fake_results = [
        {
            "title": "Sales Pipeline Dashboard",
            "summary": "Single pipeline-dashboard widget at the root",
            "content": "# Sales Pipeline Dashboard\n\nUse pipeline-dashboard...",
            "concepts": ["pipeline-dashboard", "sales-pipeline"],
            "score": 1.23,
        }
    ]
    with patch(
        "pocketpaw.agents.sdk_mcp_kb._run_kb_search",
        new=AsyncMock(return_value={"results": fake_results}),
    ):
        out = await _find_recipe_handler(
            {"query": "sales pipeline dashboard", "scope": "ripple-recipes"}
        )

    assert out.get("is_error") is None or out["is_error"] is False
    text = out["content"][0]["text"]
    parsed = json.loads(text)
    assert parsed["scope"] == "ripple-recipes"
    assert parsed["query"] == "sales pipeline dashboard"
    assert parsed["results"] == fake_results


@pytest.mark.asyncio
async def test_scope_default_is_ripple_recipes() -> None:
    """The default scope is ``ripple-recipes`` — the bundled
    library. Agents that don't pass an explicit scope get the
    canonical recipe scope without extra ceremony."""
    from pocketpaw.agents.sdk_mcp_kb import _find_recipe_handler

    captured: dict[str, Any] = {}

    async def _capture(*, query, scope, limit, binary):  # noqa: ANN001 - test stub
        captured["scope"] = scope
        return {"results": []}

    with patch(
        "pocketpaw.agents.sdk_mcp_kb._run_kb_search",
        new=_capture,
    ):
        await _find_recipe_handler({"query": "anything"})
    assert captured.get("scope") == "ripple-recipes"


def test_server_builds_when_sdk_available() -> None:
    """The server factory returns ``(name, server)`` when
    claude_agent_sdk is importable. If the SDK is missing it
    returns None silently — the chat agent boot path must keep
    working in environments without the SDK."""
    pytest.importorskip("claude_agent_sdk")
    from pocketpaw.agents.sdk_mcp_kb import build_kb_server

    result = build_kb_server()
    assert result is not None
    name, _server = result
    assert name == "pocketpaw_kb"
