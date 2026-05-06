"""Tests for Codex CLI backend — SDK-driven, mocked.

We mock ``Codex.start_thread`` to return a fake Thread whose
``run_streamed`` yields typed SDK events
(``ItemStartedEvent``/``ItemCompletedEvent``/``TurnCompletedEvent`` with
real ``CommandExecutionItem``/``McpToolCallItem``/``FileChangeItem``/
``WebSearchItem``/``AgentMessageItem``/``ReasoningItem``/``ErrorItem``
payloads). This catches the same kind of regressions our old subprocess-
fixture tests caught — wrong field name, missing case in the dispatch —
without depending on stdout NDJSON parsing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings

# A path that ``_resolve_codex_binary`` would never actually return; we
# patch the resolver so the backend treats Codex as installed without
# actually running it.
_FAKE_BINARY = "/usr/local/bin/codex"


def _events_from(items_or_events):
    """Build the typed SDK events for a sequence of ``(phase, item)``
    tuples (``phase`` ∈ ``"started" | "completed"``) plus optional
    raw events appended verbatim."""
    from openai_codex_sdk import ItemCompletedEvent, ItemStartedEvent

    out = []
    for entry in items_or_events:
        if isinstance(entry, tuple):
            phase, item = entry
            if phase == "started":
                out.append(ItemStartedEvent(type="item.started", item=item))
            elif phase == "completed":
                out.append(ItemCompletedEvent(type="item.completed", item=item))
            else:
                raise AssertionError(f"unknown phase: {phase}")
        else:
            out.append(entry)
    return out


def _patch_codex(events):
    """Build a context manager that swaps ``Codex.start_thread`` with a
    fake thread whose ``run_streamed`` yields ``events``."""
    from openai_codex_sdk import StreamedTurn

    class _AsyncIter:
        def __init__(self, items):
            self._items = list(items)
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(self._items):
                raise StopAsyncIteration
            ev = self._items[self._i]
            self._i += 1
            return ev

    class _FakeThread:
        def __init__(self):
            self.last_input = None
            self.last_turn_options = None

        async def run_streamed(self, input_, turn_options=None):
            self.last_input = input_
            self.last_turn_options = turn_options
            return StreamedTurn(events=_AsyncIter(events))

    fake_thread = _FakeThread()

    class _FakeCodex:
        def __init__(self, options=None):
            self.options = options
            self.started_with = None

        def start_thread(self, options=None):
            self.started_with = options
            return fake_thread

    return patch(
        "pocketpaw.agents.codex_cli.Codex"
        if False
        else "openai_codex_sdk.Codex",
        _FakeCodex,
    ), fake_thread


@pytest.fixture(autouse=True)
def _stub_resolver():
    """Make every test see Codex as "installed" without touching disk."""
    with patch(
        "pocketpaw.agents.codex_cli._resolve_codex_binary",
        return_value=_FAKE_BINARY,
    ):
        yield


# ---------------------------------------------------------------------------
# Static metadata
# ---------------------------------------------------------------------------


class TestCodexCLIInfo:
    def test_info_static(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        info = CodexCLIBackend.info()
        assert info.name == "codex_cli"
        assert info.display_name == "Codex CLI"
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MCP in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities
        assert Capability.CUSTOM_SYSTEM_PROMPT in info.capabilities
        assert "shell" in info.builtin_tools
        assert "web_search" in info.builtin_tools

    def test_tool_policy_map(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        info = CodexCLIBackend.info()
        assert info.tool_policy_map["shell"] == "shell"
        assert info.tool_policy_map["file_edit"] == "write_file"
        assert info.tool_policy_map["web_search"] == "browser"

    def test_required_keys(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        info = CodexCLIBackend.info()
        assert "openai_api_key" in info.required_keys
        assert "openai" in info.supported_providers


# ---------------------------------------------------------------------------
# Init / availability
# ---------------------------------------------------------------------------


class TestCodexCLIInit:
    def test_init(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        assert backend._cli_available is True
        assert backend._codex_path == _FAKE_BINARY

    @pytest.mark.asyncio
    async def test_run_without_cli(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        with patch(
            "pocketpaw.agents.codex_cli._resolve_codex_binary", return_value=None
        ):
            backend = CodexCLIBackend(Settings())
            events = []
            async for event in backend.run("test"):
                events.append(event)

        assert any(e.type == "error" for e in events)
        assert any("not found" in e.content for e in events if e.type == "error")

    @pytest.mark.asyncio
    async def test_get_status(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        status = await backend.get_status()
        assert status["backend"] == "codex_cli"
        assert status["cli_available"] is True
        assert "model" in status


# ---------------------------------------------------------------------------
# History injection helper
# ---------------------------------------------------------------------------


class TestCodexCLIHelpers:
    def test_inject_history(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = CodexCLIBackend._inject_history("Base prompt.", history)
        assert "Base prompt." in result
        assert "# Recent Conversation" in result
        assert "**User**: Hello" in result
        assert "**Assistant**: Hi!" in result

    def test_inject_history_truncates(self):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        long_msg = "x" * 600
        history = [{"role": "user", "content": long_msg}]
        result = CodexCLIBackend._inject_history("Base.", history)
        assert "x" * 500 + "..." in result
        assert "x" * 501 not in result


# ---------------------------------------------------------------------------
# Stream → AgentEvent translation
# ---------------------------------------------------------------------------


class TestCodexCLIRun:
    @pytest.mark.asyncio
    async def test_parses_agent_message(self):
        from openai_codex_sdk import AgentMessageItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        events_in = _events_from(
            [
                (
                    "completed",
                    AgentMessageItem(
                        id="i1", type="agent_message", text="Hello from Codex!"
                    ),
                ),
            ]
        )
        ctx, _ = _patch_codex(events_in)
        with ctx:
            out = []
            async for event in backend.run("Hi"):
                out.append(event)

        messages = [e for e in out if e.type == "message"]
        assert len(messages) == 1
        assert messages[0].content == "Hello from Codex!"

    @pytest.mark.asyncio
    async def test_parses_command_execution_started_and_completed(self):
        from openai_codex_sdk import CommandExecutionItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        started = CommandExecutionItem(
            id="i1",
            type="command_execution",
            command="bash -lc ls",
            aggregated_output="",
            exit_code=None,
            status="in_progress",
        )
        completed = CommandExecutionItem(
            id="i1",
            type="command_execution",
            command="bash -lc ls",
            aggregated_output="file1.txt\nfile2.txt",
            exit_code=0,
            status="completed",
        )
        events_in = _events_from([("started", started), ("completed", completed)])

        ctx, _ = _patch_codex(events_in)
        with ctx:
            out = []
            async for event in backend.run("list files"):
                out.append(event)

        tool_use = [e for e in out if e.type == "tool_use"]
        results = [e for e in out if e.type == "tool_result"]
        assert tool_use and tool_use[0].metadata["name"] == "shell"
        assert "ls" in tool_use[0].metadata["input"]["command"]
        assert results and results[0].metadata["name"] == "shell"
        assert "file1.txt" in results[0].content

    @pytest.mark.asyncio
    async def test_command_execution_passes_full_output_through(self):
        """The adapter used to truncate ``aggregated_output`` to 200
        chars, which destroyed the JSON body of cloud_* CLI calls
        before ``_publish_pocket_event`` could parse it. Full content
        (capped at 64 KiB) must reach the agent loop now."""
        from openai_codex_sdk import CommandExecutionItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        big = "x" * 5000  # well over the old 200-char cap
        completed = CommandExecutionItem(
            id="i1",
            type="command_execution",
            command="echo big",
            aggregated_output=big,
            exit_code=0,
            status="completed",
        )
        ctx, _ = _patch_codex(_events_from([("completed", completed)]))
        with ctx:
            out = []
            async for event in backend.run("run"):
                out.append(event)

        results = [e for e in out if e.type == "tool_result"]
        assert results
        assert len(results[0].content) >= 5000

    @pytest.mark.asyncio
    async def test_command_execution_nonzero_exit_prefixed(self):
        from openai_codex_sdk import CommandExecutionItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        completed = CommandExecutionItem(
            id="i1",
            type="command_execution",
            command="false",
            aggregated_output="oops",
            exit_code=1,
            status="failed",
        )
        ctx, _ = _patch_codex(_events_from([("completed", completed)]))
        with ctx:
            out = []
            async for event in backend.run("run"):
                out.append(event)

        results = [e for e in out if e.type == "tool_result"]
        assert results and results[0].content.startswith("[exit 1]")

    @pytest.mark.asyncio
    async def test_parses_file_change(self):
        from openai_codex_sdk import FileChangeItem
        from openai_codex_sdk.types import FileUpdateChange

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = FileChangeItem(
            id="i2",
            type="file_change",
            changes=[FileUpdateChange(path="main.py", kind="update")],
            status="completed",
        )
        ctx, _ = _patch_codex(_events_from([("started", item), ("completed", item)]))
        with ctx:
            out = []
            async for event in backend.run("edit"):
                out.append(event)

        tool_use = [e for e in out if e.type == "tool_use"]
        results = [e for e in out if e.type == "tool_result"]
        assert tool_use and tool_use[0].metadata["name"] == "file_edit"
        assert "main.py" in tool_use[0].content
        assert results and "main.py" in results[0].content

    @pytest.mark.asyncio
    async def test_parses_web_search(self):
        from openai_codex_sdk import WebSearchItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = WebSearchItem(id="i3", type="web_search", query="python asyncio")
        ctx, _ = _patch_codex(_events_from([("started", item), ("completed", item)]))
        with ctx:
            out = []
            async for event in backend.run("search"):
                out.append(event)

        tool_use = [e for e in out if e.type == "tool_use"]
        results = [e for e in out if e.type == "tool_result"]
        assert tool_use and "asyncio" in tool_use[0].content
        assert results and results[0].metadata["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_parses_mcp_tool_call_with_text_blocks(self):
        from openai_codex_sdk import McpToolCallItem
        from openai_codex_sdk.types import McpToolCallResult

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        started = McpToolCallItem(
            id="i4",
            type="mcp_tool_call",
            server="pocketpaw_pocket",
            tool="get_pocket",
            arguments={"pocket_id": "abc"},
            result=None,
            error=None,
            status="in_progress",
        )
        completed = McpToolCallItem(
            id="i4",
            type="mcp_tool_call",
            server="pocketpaw_pocket",
            tool="get_pocket",
            arguments={"pocket_id": "abc"},
            result=McpToolCallResult(
                content=[{"type": "text", "text": "pocket payload"}],
                structured_content=None,
            ),
            error=None,
            status="completed",
        )
        ctx, _ = _patch_codex(_events_from([("started", started), ("completed", completed)]))
        with ctx:
            out = []
            async for event in backend.run("use mcp"):
                out.append(event)

        tool_use = [e for e in out if e.type == "tool_use"]
        results = [e for e in out if e.type == "tool_result"]
        assert tool_use and tool_use[0].metadata["name"] == "get_pocket"
        assert tool_use[0].metadata["input"] == {"pocket_id": "abc"}
        # The actual MCP text reaches the agent — this is the bug from the
        # earlier subprocess implementation that returned ``""``.
        assert results and "pocket payload" in results[0].content

    @pytest.mark.asyncio
    async def test_mcp_tool_call_error(self):
        from openai_codex_sdk import McpToolCallItem
        from openai_codex_sdk.types import McpToolCallError

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        completed = McpToolCallItem(
            id="i5",
            type="mcp_tool_call",
            server="srv",
            tool="x",
            arguments={},
            result=None,
            error=McpToolCallError(message="boom"),
            status="failed",
        )
        ctx, _ = _patch_codex(_events_from([("completed", completed)]))
        with ctx:
            out = []
            async for event in backend.run("err"):
                out.append(event)

        results = [e for e in out if e.type == "tool_result"]
        assert results and results[0].content.startswith("[error] boom")

    @pytest.mark.asyncio
    async def test_parses_reasoning(self):
        from openai_codex_sdk import ReasoningItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = ReasoningItem(id="i6", type="reasoning", text="Thinking about this...")
        ctx, _ = _patch_codex(_events_from([("completed", item)]))
        with ctx:
            out = []
            async for event in backend.run("think"):
                out.append(event)

        thinking = [e for e in out if e.type == "thinking"]
        assert thinking and "Thinking" in thinking[0].content

    @pytest.mark.asyncio
    async def test_parses_turn_completed_usage(self):
        from openai_codex_sdk import TurnCompletedEvent
        from openai_codex_sdk.types import Usage

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        usage = Usage(input_tokens=100, output_tokens=25, cached_input_tokens=50)
        ev = TurnCompletedEvent(type="turn.completed", usage=usage)
        ctx, _ = _patch_codex([ev])
        with ctx:
            out = []
            async for event in backend.run("test"):
                out.append(event)

        usage_evts = [e for e in out if e.type == "token_usage"]
        assert usage_evts
        assert usage_evts[0].metadata["input_tokens"] == 100
        assert usage_evts[0].metadata["output_tokens"] == 25
        assert usage_evts[0].metadata["cached_input_tokens"] == 50
        assert usage_evts[0].metadata["backend"] == "codex_cli"

    @pytest.mark.asyncio
    async def test_handles_error_item(self):
        from openai_codex_sdk import ErrorItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = ErrorItem(id="ie", type="error", message="Rate limit exceeded")
        ctx, _ = _patch_codex(_events_from([("completed", item)]))
        with ctx:
            out = []
            async for event in backend.run("test"):
                out.append(event)

        errors = [e for e in out if e.type == "error"]
        assert errors and "Rate limit" in errors[0].content

    @pytest.mark.asyncio
    async def test_yields_done_at_end(self):
        from openai_codex_sdk import AgentMessageItem

        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = AgentMessageItem(id="i", type="agent_message", text="ok")
        ctx, _ = _patch_codex(_events_from([("completed", item)]))
        with ctx:
            out = []
            async for event in backend.run("test"):
                out.append(event)

        assert out[-1].type == "done"


# ---------------------------------------------------------------------------
# Cross-backend / system-prompt behaviour
# ---------------------------------------------------------------------------


class TestCodexCLIPromptDelivery:
    @pytest.mark.asyncio
    async def test_history_injected_into_agents_md(self, tmp_path, monkeypatch):
        """Conversation history is written into AGENTS.md (which Codex
        auto-loads from cwd) so multi-turn context survives across runs."""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        captured_path: list[str] = []
        original_mkdtemp = __import__("tempfile").mkdtemp

        def capture_mkdtemp(*args, **kwargs):
            p = original_mkdtemp(*args, **kwargs)
            captured_path.append(p)
            return p

        monkeypatch.setattr("tempfile.mkdtemp", capture_mkdtemp)

        backend = CodexCLIBackend(Settings())
        ctx, _ = _patch_codex([])

        # Snapshot AGENTS.md while the run is in progress (before the
        # ``finally`` block deletes the temp dir). We do that by reading
        # the file inside our fake ``start_thread``.
        captured_agents_md: list[str] = []

        from openai_codex_sdk import StreamedTurn

        class _FakeThread:
            async def run_streamed(self, input_, turn_options=None):
                # cwd is set to the tempdir; AGENTS.md sits there.
                from pathlib import Path

                cwd = Path(captured_path[0])
                captured_agents_md.append((cwd / "AGENTS.md").read_text("utf-8"))
                return StreamedTurn(events=_async_empty())

        class _FakeCodex:
            def __init__(self, options=None):
                pass

            def start_thread(self, options=None):
                return _FakeThread()

        async def _async_empty():
            if False:
                yield None
            return

        with patch("openai_codex_sdk.Codex", _FakeCodex):
            history = [
                {"role": "user", "content": "From previous backend"},
                {"role": "assistant", "content": "I remember"},
            ]
            async for _ in backend.run(
                "Continue our chat",
                system_prompt="You are PocketPaw.",
                history=history,
                session_key="s1",
            ):
                pass

        assert captured_agents_md, "AGENTS.md should have been written"
        contents = captured_agents_md[0]
        assert "You are PocketPaw." in contents
        assert "Recent Conversation" in contents
        assert "From previous backend" in contents

    def test_build_subprocess_env_extracts_pocket_id_and_mirrors_mongo(
        self, monkeypatch
    ):
        """The Codex subprocess gets per-turn identity + a Mongo URI alias.
        ``cloud_*`` CLI commands invoked from the agent then have everything
        they need without an explicit MCP layer."""
        from pocketpaw.agents.codex_cli import _build_subprocess_env

        monkeypatch.setenv("CLOUD_MONGODB_URI", "mongodb://example/paw")
        monkeypatch.delenv("POCKETPAW_MONGO_URI", raising=False)
        monkeypatch.delenv("POCKETPAW_POCKET_ID", raising=False)

        prompt = (
            "<scope>pocket abc123</scope>\n"
            '<current-pocket id="abc123" />\n'
            "..."
        )
        env = _build_subprocess_env(prompt)

        assert env["POCKETPAW_POCKET_ID"] == "abc123"
        # Mirror the cloud Mongo URI under the CLI's preferred name so
        # ``pocketpaw.tools.cli cloud_*`` doesn't have to know about both.
        assert env["POCKETPAW_MONGO_URI"] == "mongodb://example/paw"
        # Parent env survives.
        assert env["CLOUD_MONGODB_URI"] == "mongodb://example/paw"

    def test_build_subprocess_env_no_pocket_in_prompt_leaves_id_unset(
        self, monkeypatch
    ):
        from pocketpaw.agents.codex_cli import _build_subprocess_env

        monkeypatch.delenv("POCKETPAW_POCKET_ID", raising=False)
        env = _build_subprocess_env("plain prompt with no current-pocket tag")
        assert "POCKETPAW_POCKET_ID" not in env

    @pytest.mark.asyncio
    async def test_user_message_goes_to_run_streamed_input(self):
        """The user prompt is the ``input_`` argument to ``run_streamed`` —
        not in AGENTS.md. (AGENTS.md gets the system prompt + history.)"""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        ctx, fake_thread = _patch_codex([])
        with ctx:
            async for _ in backend.run(
                "Hello world",
                system_prompt="You are PocketPaw.",
            ):
                pass

        assert fake_thread.last_input == "Hello world"
