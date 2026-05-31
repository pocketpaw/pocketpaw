"""Tests for deep_agents streaming-event emission.

Covers two recent fixes:
  * ``thinking`` events are surfaced from message-chunk content blocks
    (Anthropic + DeepSeek both feed this).
  * ``tool_use`` events fire as soon as the first ``tool_call_chunk``
    carries a name, and the same tool_call_id is not announced twice
    across the messages + updates paths.

These are pure unit tests against the extraction helpers + a small
fake astream stream. No real model, no MCP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.deep_agents import (
    DeepAgentsBackend,
    _extract_content_text,
    _split_content_text_and_thinking,
)
from pocketpaw.config import Settings

# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------


class TestSplitContentTextAndThinking:
    def test_plain_string_goes_to_text(self):
        text, thinking = _split_content_text_and_thinking("hello world")
        assert text == "hello world"
        assert thinking == ""

    def test_anthropic_blocks_split_correctly(self):
        content = [
            {"type": "thinking", "thinking": "let me reason about this..."},
            {"type": "text", "text": "Here's the answer:"},
            {"type": "text", "text": " 42."},
        ]
        text, thinking = _split_content_text_and_thinking(content)
        assert text == "Here's the answer: 42."
        assert thinking == "let me reason about this..."

    def test_deepseek_thinking_via_langchain_litellm_wrap(self):
        """langchain_litellm wraps DeepSeek reasoning_content as an
        Anthropic-style thinking block — same extraction path works."""
        content = [{"type": "thinking", "thinking": "step 1: parse intent"}]
        text, thinking = _split_content_text_and_thinking(content)
        assert text == ""
        assert thinking == "step 1: parse intent"

    def test_redacted_thinking_falls_into_thinking_stream(self):
        content = [{"type": "redacted_thinking", "data": "[redacted]"}]
        text, thinking = _split_content_text_and_thinking(content)
        assert thinking == "[redacted]"

    def test_backward_compat_extract_text_still_drops_thinking(self):
        """The original _extract_content_text contract is preserved —
        callers that only want text continue to get only text."""
        content = [
            {"type": "thinking", "thinking": "noise"},
            {"type": "text", "text": "signal"},
        ]
        assert _extract_content_text(content) == "signal"


# ---------------------------------------------------------------------------
# Streaming-loop integration: thinking + tool_use dedup
# ---------------------------------------------------------------------------


class _FakeChunk:
    """Minimal stand-in for AIMessageChunk."""

    def __init__(self, content: Any = "", tool_call_chunks: list | None = None):
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []


class _FakeToolMessage:
    type = "tool"
    name = "create_pocket"
    content = "ok"


async def _drive_stream(backend: DeepAgentsBackend, chunks: list[dict]) -> list:
    """Run backend.run() against a hand-built astream sequence and
    return the list of AgentEvents produced."""

    async def fake_astream(*_a: Any, **_k: Any) -> AsyncIterator[dict]:
        for c in chunks:
            yield c

    fake_agent = MagicMock()
    fake_agent.astream = fake_astream

    backend._sdk_available = True
    backend._cached_agent = fake_agent
    # Mirror the _get_or_create_agent cache-key shape (model, skills,
    # memory, is_pocket_session). The default test prompt "hi" carries
    # no <pocket-scope> marker so is_pocket_session is False.
    backend._cached_model_key = (
        backend.settings.deep_agents_model,
        tuple(backend.settings.deep_agents_skills or []),
        tuple(backend.settings.deep_agents_memory or []),
        False,
    )

    events = []
    with (
        patch.object(backend, "_build_model", return_value=MagicMock()),
        patch.object(backend, "_build_mcp_tools", return_value=[]),
    ):
        async for evt in backend.run("hi"):
            events.append(evt)
    return events


@pytest.mark.asyncio
async def test_thinking_chunks_emit_thinking_events():
    backend = DeepAgentsBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))

    chunks = [
        {
            "type": "messages",
            "data": (
                _FakeChunk(
                    content=[
                        {"type": "thinking", "thinking": "weighing options"},
                    ]
                ),
                {},
            ),
        },
        {
            "type": "messages",
            "data": (_FakeChunk(content=[{"type": "text", "text": "Done."}]), {}),
        },
    ]
    events = await _drive_stream(backend, chunks)
    types = [(e.type, e.content) for e in events]
    assert ("thinking", "weighing options") in types
    assert ("message", "Done.") in types
    # And done at the end.
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_early_tool_use_fires_on_first_tool_call_chunk():
    """The messages path should announce a tool_use the moment the
    chunk carries a name, BEFORE the updates path would have emitted
    the full call."""
    backend = DeepAgentsBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))

    chunks = [
        {
            "type": "messages",
            "data": (
                _FakeChunk(
                    tool_call_chunks=[{"name": "create_pocket", "id": "call_001", "args": "{}"}]
                ),
                {},
            ),
        },
        # ...then the updates path fires later with the full tool_call
        # carrying the same id — must NOT double-announce.
        {
            "type": "updates",
            "data": {
                "agent": {
                    "messages": [
                        MagicMock(
                            tool_calls=[
                                {
                                    "name": "create_pocket",
                                    "id": "call_001",
                                    "args": {"name": "Todos"},
                                }
                            ],
                            type="ai",
                        )
                    ]
                }
            },
        },
    ]
    events = await _drive_stream(backend, chunks)
    tool_use_events = [e for e in events if e.type == "tool_use"]
    assert len(tool_use_events) == 1, (
        f"expected 1 tool_use event after dedup, got {len(tool_use_events)}"
    )
    assert tool_use_events[0].metadata["name"] == "create_pocket"


@pytest.mark.asyncio
async def test_tool_use_without_id_emits_anyway():
    """Some providers omit tool_call ids in tool_call_chunks. When id
    is missing we can't dedup, so the late updates-path emission
    proceeds (preserves the pre-refactor single-emit behavior)."""
    backend = DeepAgentsBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))

    chunks = [
        {
            "type": "updates",
            "data": {
                "agent": {
                    "messages": [
                        MagicMock(
                            tool_calls=[{"name": "x", "args": {}}],
                            type="ai",
                        )
                    ]
                }
            },
        },
    ]
    events = await _drive_stream(backend, chunks)
    tool_uses = [e for e in events if e.type == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].metadata["name"] == "x"
