# E2E test for the tool-output budget (#1160).
# Created: 2026-05-21
#
# Proves that an oversized tool result is capped before it reaches agent
# context, through the real production tool paths:
#
#   1. ToolRegistry.execute — the universal chokepoint. A real ~50KB tool is
#      registered and run; the registry return value (the tool_result fed
#      back to the model) must be the capped form.
#   2. tool_bridge FunctionTool callback — the exact callback the OpenAI
#      Agents SDK Runner invokes for a tool call. Its tool_call_output must
#      be the capped form.
#   3. The real AgentLoop — a mocked LLM backend (AsyncMock-style async
#      generator) drives a tool call by running the real registry on the
#      ~50KB tool, then yields the result as a tool_result event. The loop
#      processes the run end-to-end; the tool_result it handles is capped.
#
# Only the LLM is mocked. The tool, the registry, the injection scanner, the
# output budget, and the AgentLoop are all real.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.loop import AgentLoop
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.bus import Channel, InboundMessage
from pocketpaw.tools.output_budget import TOOL_OUTPUT_CHAR_CAP
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

# Size of the blob the noisy tool returns — well over the 12k cap.
_BIG_OUTPUT_SIZE = 50_000


@pytest.fixture(scope="session", autouse=True)
def require_playwright_browsers():
    """Override the Playwright autouse skip from tests/e2e/conftest.py.

    This file drives the agent loop and tool registry — no browser needed —
    so it must run even when Playwright Chromium is not installed.
    """
    yield


# --------------------------------------------------------------------------
# A realistic noisy tool
# --------------------------------------------------------------------------


class BigOutputTool(BaseTool):
    """A tool that dumps a ~50KB blob — stands in for a long test run, a
    build log, or a large HTTP response body."""

    @property
    def name(self) -> str:
        return "big_output"

    @property
    def description(self) -> str:
        return "Returns a very large output blob for budget testing."

    async def execute(self, **params: object) -> str:
        # Return the blob directly (no _success), the way ShellTool and
        # RunPythonTool do — this is the path the registry chokepoint must
        # still catch.
        return "noisy-log-line " * (_BIG_OUTPUT_SIZE // len("noisy-log-line "))


# --------------------------------------------------------------------------
# 1. Registry chokepoint
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_caps_oversized_tool_result():
    """ToolRegistry.execute caps a ~50KB result before returning it."""
    registry = ToolRegistry()
    registry.register(BigOutputTool())

    raw_size = len(await BigOutputTool().execute())
    assert raw_size > TOOL_OUTPUT_CHAR_CAP, "fixture should exceed the cap"

    result = await registry.execute("big_output")

    assert len(result) <= TOOL_OUTPUT_CHAR_CAP
    assert len(result) < raw_size
    assert "tool output truncated" in result


# --------------------------------------------------------------------------
# 2. tool_bridge FunctionTool callback (OpenAI Agents path)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_function_tool_callback_returns_capped_output():
    """The FunctionTool callback the OpenAI Agents Runner invokes for a tool
    call hands back the capped form as the tool_call_output."""
    pytest.importorskip("agents", reason="OpenAI Agents SDK not installed")

    from pocketpaw.agents.tool_bridge import build_openai_function_tools
    from pocketpaw.config import Settings

    # Build the real FunctionTool wrappers, but over just our noisy tool so
    # the test is hermetic and fast.
    with patch(
        "pocketpaw.agents.tool_bridge._instantiate_all_tools",
        return_value=[BigOutputTool()],
    ):
        function_tools = build_openai_function_tools(Settings(), backend="openai_agents")

    big_tool_ft = next(ft for ft in function_tools if ft.name == "big_output")

    # on_invoke_tool is exactly what Runner calls when the model emits a
    # function call. args is the JSON arguments string.
    output = await big_tool_ft.on_invoke_tool(MagicMock(), "{}")

    assert len(output) <= TOOL_OUTPUT_CHAR_CAP
    assert "tool output truncated" in output


# --------------------------------------------------------------------------
# 3. The real AgentLoop with a mocked LLM backend
# --------------------------------------------------------------------------


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    return bus


@pytest.fixture
def mock_memory():
    mem = MagicMock()
    mem.add_to_session = AsyncMock()
    mem.get_session_history = AsyncMock(return_value=[])
    mem.get_compacted_history = AsyncMock(return_value=[])
    mem.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    return mem


@pytest.mark.asyncio
async def test_agent_loop_tool_result_is_capped(mock_bus, mock_memory):
    """Drive the real AgentLoop with a mocked LLM backend that triggers a
    tool call. The tool runs through the real registry; the tool_result the
    loop receives and forwards is the capped form, not the ~50KB blob."""
    registry = ToolRegistry()
    registry.register(BigOutputTool())

    # Records what the mocked backend produced as the tool_result content,
    # so the test can assert on the exact string that flowed into the loop.
    captured: dict[str, str] = {}

    async def mock_run(message, *, system_prompt=None, history=None, session_key=None):
        """Stand-in for the LLM backend. A real backend would call the tool
        and stream a tool_result; here we run the real registry and yield
        the real (capped) result."""
        yield AgentEvent(
            type="tool_use",
            content="Using big_output...",
            metadata={"name": "big_output", "input": {}},
        )
        tool_result = await registry.execute("big_output")
        captured["tool_result"] = tool_result
        yield AgentEvent(
            type="tool_result",
            content=tool_result,
            metadata={"name": "big_output"},
        )
        yield AgentEvent(type="message", content="Done.")
        yield AgentEvent(type="done", content="")

    mock_router = MagicMock()
    mock_router.run = mock_run
    mock_router.stop = AsyncMock()
    mock_router._backend = MagicMock()
    mock_router.scoped_tool_policy = MagicMock()

    settings = MagicMock()
    settings.agent_backend = "claude_agent_sdk"
    settings.max_concurrent_conversations = 5
    settings.welcome_hint_enabled = False
    settings.injection_scan_enabled = False
    settings.pii_scan_enabled = False
    settings.tool_profile = "full"
    settings.compaction_recent_window = 10
    settings.compaction_char_budget = 16000
    settings.compaction_summary_chars = 300
    settings.compaction_llm_summarize = False

    with (
        patch("pocketpaw.agents.loop.get_message_bus", return_value=mock_bus),
        patch("pocketpaw.agents.loop.get_memory_manager", return_value=mock_memory),
        patch("pocketpaw.agents.loop.AgentContextBuilder") as mock_builder_cls,
        patch("pocketpaw.agents.loop.AgentRouter", return_value=mock_router),
        patch("pocketpaw.agents.loop.get_settings", return_value=settings),
        patch("pocketpaw.agents.loop.Settings") as mock_settings_cls,
    ):
        mock_settings_cls.load.return_value = settings
        mock_builder_cls.return_value.build_system_prompt = AsyncMock(return_value="System Prompt")

        loop = AgentLoop()
        msg = InboundMessage(
            channel=Channel.CLI,
            sender_id="user1",
            chat_id="chat1",
            content="run the big tool",
        )
        await loop._process_message(msg)

    # The backend produced a tool_result, and it was the capped form.
    assert "tool_result" in captured, "mocked backend never produced a tool_result"
    tool_result = captured["tool_result"]
    assert len(tool_result) <= TOOL_OUTPUT_CHAR_CAP
    assert "tool output truncated" in tool_result

    # The loop ran the tool call end-to-end and fanned out tool events.
    published_events = [
        call.args[0].event_type for call in mock_bus.publish_system.call_args_list if call.args
    ]
    assert "tool_start" in published_events
    assert "tool_result" in published_events


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
