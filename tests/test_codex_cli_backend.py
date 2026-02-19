"""Tests for Codex CLI backend — mocked (no real CLI needed)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.backend import Capability
from pocketpaw.config import Settings


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


class TestCodexCLIInit:
    @patch("shutil.which", return_value="/usr/bin/codex")
    def test_init(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        assert backend._cli_available is True

    @patch("shutil.which", return_value=None)
    def test_init_without_cli(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        assert backend._cli_available is False

    @pytest.mark.asyncio
    @patch("shutil.which", return_value=None)
    async def test_run_without_cli(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        events = []
        async for event in backend.run("test"):
            events.append(event)

        assert any(e.type == "error" for e in events)
        assert any("not found" in e.content for e in events if e.type == "error")

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_stop(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        await backend.stop()
        assert backend._stop_flag is True

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_get_status(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        status = await backend.get_status()
        assert status["backend"] == "codex_cli"
        assert status["cli_available"] is True
        assert "model" in status


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


class _AsyncLineIterator:
    """Helper that simulates async line iteration over bytes."""

    def __init__(self, lines: list[str]):
        self._lines = [(line + "\n").encode("utf-8") for line in lines]
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


def _ev(data: dict) -> str:
    """Serialize a dict to a compact JSON string for mock stdout."""
    import json

    return json.dumps(data, separators=(",", ":"))


def _make_mock_process(stdout_lines: list[str], returncode: int = 0) -> MagicMock:
    """Create a mock subprocess with given stdout lines."""
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdout = _AsyncLineIterator(stdout_lines)
    mock_proc.stderr = AsyncMock()
    mock_proc.stderr.read = AsyncMock(return_value=b"")

    async def mock_wait():
        mock_proc.returncode = returncode

    mock_proc.wait = mock_wait
    return mock_proc


class TestCodexCLIRun:
    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_agent_message(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {"id": "item_1", "type": "agent_message", "text": "Hello from Codex!"}
        mock_proc = _make_mock_process([
            _ev({"type": "item.completed", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("Hi"):
                events.append(event)

        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 1
        assert messages[0].content == "Hello from Codex!"

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_command_execution_started(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {
            "id": "item_1",
            "type": "command_execution",
            "command": "bash -lc ls",
            "status": "in_progress",
        }
        mock_proc = _make_mock_process([
            _ev({"type": "item.started", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("list files"):
                events.append(event)

        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].metadata["name"] == "shell"
        assert "ls" in tool_events[0].metadata["input"]["command"]

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_command_execution_completed(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {
            "id": "item_1",
            "type": "command_execution",
            "output": "file1.txt\nfile2.txt",
        }
        mock_proc = _make_mock_process([
            _ev({"type": "item.completed", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("list files"):
                events.append(event)

        results = [e for e in events if e.type == "tool_result"]
        assert len(results) == 1
        assert results[0].metadata["name"] == "shell"

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_file_change_started(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {
            "id": "item_2",
            "type": "file_change",
            "filename": "main.py",
            "status": "in_progress",
        }
        mock_proc = _make_mock_process([
            _ev({"type": "item.started", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("edit file"):
                events.append(event)

        tool_events = [e for e in events if e.type == "tool_use"]
        assert len(tool_events) == 1
        assert tool_events[0].metadata["name"] == "file_edit"
        assert "main.py" in tool_events[0].content

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_file_change_completed(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {"id": "item_2", "type": "file_change", "filename": "main.py"}
        mock_proc = _make_mock_process([
            _ev({"type": "item.completed", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("edit file"):
                events.append(event)

        results = [e for e in events if e.type == "tool_result"]
        assert len(results) == 1
        assert "main.py" in results[0].content

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_web_search(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        started = {"id": "item_3", "type": "web_search", "query": "python asyncio"}
        completed = {"id": "item_3", "type": "web_search", "output": "Results found"}
        mock_proc = _make_mock_process([
            _ev({"type": "item.started", "item": started}),
            _ev({"type": "item.completed", "item": completed}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("search"):
                events.append(event)

        tool_use = [e for e in events if e.type == "tool_use"]
        tool_result = [e for e in events if e.type == "tool_result"]
        assert len(tool_use) == 1
        assert "asyncio" in tool_use[0].content
        assert len(tool_result) == 1
        assert tool_result[0].metadata["name"] == "web_search"

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_mcp_tool_call(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        started = {
            "id": "item_4",
            "type": "mcp_tool_call",
            "name": "my_tool",
            "arguments": {"key": "val"},
        }
        completed = {
            "id": "item_4",
            "type": "mcp_tool_call",
            "name": "my_tool",
            "output": "done",
        }
        mock_proc = _make_mock_process([
            _ev({"type": "item.started", "item": started}),
            _ev({"type": "item.completed", "item": completed}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("use mcp"):
                events.append(event)

        tool_use = [e for e in events if e.type == "tool_use"]
        tool_result = [e for e in events if e.type == "tool_result"]
        assert len(tool_use) == 1
        assert tool_use[0].metadata["name"] == "my_tool"
        assert tool_use[0].metadata["input"] == {"key": "val"}
        assert len(tool_result) == 1

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_reasoning(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {"id": "item_5", "type": "reasoning", "text": "Thinking about this..."}
        mock_proc = _make_mock_process([
            _ev({"type": "item.completed", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("think"):
                events.append(event)

        thinking = [e for e in events if e.type == "thinking"]
        assert len(thinking) == 1
        assert "Thinking" in thinking[0].content

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_parses_turn_completed_usage(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        usage = {
            "input_tokens": 100,
            "cached_input_tokens": 50,
            "output_tokens": 25,
        }
        mock_proc = _make_mock_process([
            _ev({"type": "turn.completed", "usage": usage}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        usage_evts = [e for e in events if e.type == "token_usage"]
        assert len(usage_evts) == 1
        assert usage_evts[0].metadata["input_tokens"] == 100
        assert usage_evts[0].metadata["output_tokens"] == 25
        assert usage_evts[0].metadata["cached_input_tokens"] == 50

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_handles_error_event(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        mock_proc = _make_mock_process([
            _ev({"type": "error", "message": "Rate limit exceeded"}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 1
        assert "Rate limit" in errors[0].content

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_handles_turn_failed(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        mock_proc = _make_mock_process([
            _ev({"type": "turn.failed", "message": "Model overloaded"}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert len(errors) == 1
        assert "overloaded" in errors[0].content.lower()

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_handles_process_failure(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        mock_proc = _make_mock_process([], returncode=1)
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"fatal error")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        errors = [e for e in events if e.type == "error"]
        assert len(errors) >= 1
        assert any("error" in e.content.lower() for e in errors)

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_skips_invalid_json(self, mock_which):
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        item = {"id": "item_1", "type": "agent_message", "text": "OK"}
        mock_proc = _make_mock_process([
            "not valid json",
            _ev({"type": "item.completed", "item": item}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("test"):
                events.append(event)

        messages = [e for e in events if e.type == "message"]
        assert len(messages) == 1
        assert messages[0].content == "OK"

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_full_conversation_flow(self, mock_which):
        """End-to-end: thread start -> command -> message -> usage -> done."""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())
        cmd_item = {
            "id": "i1",
            "type": "command_execution",
            "command": "bash -lc ls",
            "status": "in_progress",
        }
        cmd_done = {"id": "i1", "type": "command_execution", "output": "README.md"}
        msg_item = {"id": "i2", "type": "agent_message", "text": "Has a README."}
        usage = {
            "input_tokens": 500,
            "cached_input_tokens": 400,
            "output_tokens": 50,
        }
        mock_proc = _make_mock_process([
            _ev({"type": "thread.started", "thread_id": "abc-123"}),
            _ev({"type": "turn.started"}),
            _ev({"type": "item.started", "item": cmd_item}),
            _ev({"type": "item.completed", "item": cmd_done}),
            _ev({"type": "item.completed", "item": msg_item}),
            _ev({"type": "turn.completed", "usage": usage}),
        ])

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            events = []
            async for event in backend.run("summarize"):
                events.append(event)

        types = [e.type for e in events]
        assert "tool_use" in types
        assert "tool_result" in types
        assert "message" in types
        assert "token_usage" in types
        assert types[-1] == "done"


class TestCodexCLICrossBackend:
    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_history_seeded_on_new_session(self, mock_which):
        """History is injected into prompt for context portability."""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return _make_mock_process([])

        history = [
            {"role": "user", "content": "From previous backend"},
            {"role": "assistant", "content": "I remember that context"},
        ]

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            async for _ in backend.run(
                "Continue our chat",
                system_prompt="You are PocketPaw.",
                history=history,
                session_key="s1",
            ):
                pass

        assert captured_cmd is not None
        # Prompt is the last positional argument
        prompt_value = captured_cmd[-1]
        assert "Recent Conversation" in prompt_value
        assert "From previous backend" in prompt_value

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_history_not_injected_when_empty(self, mock_which):
        """No history section when history is empty."""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return _make_mock_process([])

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            async for _ in backend.run(
                "Hello",
                system_prompt="You are PocketPaw.",
                session_key="s1",
            ):
                pass

        assert captured_cmd is not None
        prompt_value = captured_cmd[-1]
        assert "Recent Conversation" not in prompt_value

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_system_prompt_injected(self, mock_which):
        """System prompt is included at the beginning of the full prompt."""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return _make_mock_process([])

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            async for _ in backend.run(
                "Hello",
                system_prompt="You are a helpful assistant.",
                session_key="s1",
            ):
                pass

        assert captured_cmd is not None
        prompt_value = captured_cmd[-1]
        assert "[System Instructions]" in prompt_value
        assert "helpful assistant" in prompt_value

    @pytest.mark.asyncio
    @patch("shutil.which", return_value="/usr/bin/codex")
    async def test_uses_codex_exec_json_full_auto(self, mock_which):
        """Verify the subprocess command includes exec --json --full-auto."""
        from pocketpaw.agents.codex_cli import CodexCLIBackend

        backend = CodexCLIBackend(Settings())

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return _make_mock_process([])

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            async for _ in backend.run("test"):
                pass

        assert captured_cmd is not None
        cmd_list = list(captured_cmd)
        assert cmd_list[0] == "codex"
        assert cmd_list[1] == "exec"
        assert "--json" in cmd_list
        assert "--full-auto" in cmd_list
        assert "--model" in cmd_list


class TestCodexCLIRegistry:
    def test_registered_in_backend_registry(self):
        from pocketpaw.agents.registry import get_backend_class

        cls = get_backend_class("codex_cli")
        assert cls is not None
        assert cls.__name__ == "CodexCLIBackend"

    def test_backend_info_via_registry(self):
        from pocketpaw.agents.registry import get_backend_info

        info = get_backend_info("codex_cli")
        assert info is not None
        assert info.name == "codex_cli"
        assert info.display_name == "Codex CLI"

    def test_listed_in_backends(self):
        from pocketpaw.agents.registry import list_backends

        backends = list_backends()
        assert "codex_cli" in backends
