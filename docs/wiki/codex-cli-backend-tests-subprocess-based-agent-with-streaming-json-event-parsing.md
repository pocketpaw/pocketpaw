---
{
  "title": "Codex CLI Backend Tests: Subprocess-Based Agent with Streaming JSON Event Parsing",
  "summary": "This test suite validates `CodexCLIBackend`, PocketPaw's adapter for the OpenAI Codex CLI tool. The backend runs the CLI as a subprocess and parses its streaming JSON event output to extract agent messages, tool calls, file changes, web searches, MCP calls, reasoning steps, and usage statistics, all without requiring the real CLI binary to be installed.",
  "concepts": [
    "CodexCLIBackend",
    "subprocess",
    "streaming JSON",
    "Capability",
    "inject_history",
    "async line iteration",
    "command execution",
    "MCP tool call",
    "web search",
    "file change",
    "Windows compatibility"
  ],
  "categories": [
    "agent backends",
    "testing",
    "Codex CLI",
    "subprocess",
    "streaming",
    "test"
  ],
  "source_docs": [
    "40c989fa56fd4ded"
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`CodexCLIBackend` is a subprocess-based agent backend: instead of calling an LLM API directly, it launches the `codex` CLI as a child process and communicates by streaming JSON events over stdout. This allows PocketPaw to leverage Codex CLI's built-in tool ecosystem (file editing, shell commands, web search, MCP) while the backend handles the translation between PocketPaw's event model and Codex CLI's output format. All tests use mocked subprocesses.

## Static Info and Capability Flags

```python
class TestCodexCLIInfo:
    def test_info_static(self):
        info = CodexCLIBackend.info()
        assert info.name == "codex_cli"
        assert info.display_name == "Codex CLI"
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
```

The STREAMING and TOOLS capability flags tell PocketPaw's agent runner how to process responses from this backend.

## Subprocess Availability Check

```python
def test_init_without_cli(self, mock_which):
    mock_which.return_value = None  # codex not in PATH
    backend = CodexCLIBackend(settings=Settings())
    # backend.available is False

async def test_run_without_cli(self, mock_which):
    # run() raises or yields an error event when CLI not available
```

The `shutil.which` check prevents a confusing `FileNotFoundError` from deep inside subprocess creation. If the binary isn't found, the backend marks itself as unavailable and produces a clear error event.

## History Injection

```python
class TestCodexCLIHelpers:
    def test_inject_history_truncates(self):
        # very long history is truncated to prevent token overflow
```

Codex CLI doesn't have native multi-turn conversation support — each invocation is stateless. `inject_history` prepends formatted prior messages to the current prompt so the CLI sees conversation context. The truncation test guards against exceeding the CLI's input size limits on long conversations.

## Streaming JSON Event Parsing

```python
class _AsyncLineIterator:
    """Helper that simulates async line iteration over bytes."""

class TestCodexCLIRun:
    async def test_parses_agent_message(self, mock_which): ...
    async def test_parses_command_execution_started(self, mock_which): ...
    async def test_parses_file_change_started(self, mock_which): ...
    async def test_parses_web_search(self, mock_which): ...
    async def test_parses_mcp_tool_call(self, mock_which): ...
    async def test_parses_reasoning(self, mock_which): ...
    async def test_parses_turn_completed_usage(self, mock_which): ...
```

The `_AsyncLineIterator` helper simulates Codex CLI's stdout as an async byte stream. Each `test_parses_*` test feeds a specific JSON event line through the backend and asserts the correct PocketPaw event type is yielded. Each event type has different fields and the mapping must be exact — a miscategorized event would cause the dashboard to display the wrong icon or the memory system to store the wrong event type.

## Platform-Aware Subprocess

```python
_SUBPROCESS_PATCH = (
    "asyncio.create_subprocess_shell" if sys.platform == "win32"
    else "asyncio.create_subprocess_exec"
)
```

On Windows, the backend must use `create_subprocess_shell` because executables not in a system directory may not be directly launchable with `exec`. The tests use this platform-conditional patch target to ensure the mocking works correctly on both platforms.

## Known Gaps

No test covers the case where the CLI process exits with a non-zero return code mid-stream — the behavior when `mock_wait()` returns a non-zero exit code is not explicitly tested.