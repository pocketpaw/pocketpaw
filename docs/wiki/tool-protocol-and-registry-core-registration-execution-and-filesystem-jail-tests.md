---
{
  "title": "Tool Protocol and Registry: Core Registration, Execution, and Filesystem Jail Tests",
  "summary": "These tests exercise the foundational `ToolRegistry` and `BaseTool` protocol, the `ShellTool`'s command-blocking and timeout behavior, and the filesystem tools' write/read/list operations within a file jail. The jail fixture pattern and dangerous-command blocking together form PocketPaw's first line of defense against unsafe tool invocations.",
  "concepts": [
    "ToolRegistry",
    "BaseTool",
    "ShellTool",
    "ReadFileTool",
    "WriteFileTool",
    "ListDirTool",
    "file_jail",
    "dangerous_command_blocking",
    "timeout",
    "path_traversal",
    "MockTool"
  ],
  "categories": [
    "tool-system",
    "security",
    "testing",
    "test"
  ],
  "source_docs": [
    "b51d35737dc3e824"
  ],
  "backlinks": null,
  "word_count": 430,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

This test file validates the core tool infrastructure that every PocketPaw tool builds on: the `ToolRegistry` for registration and dispatch, the `BaseTool` protocol, `ShellTool` for secure command execution, and the filesystem tools (`ReadFileTool`, `WriteFileTool`, `ListDirTool`) that operate within a sandboxed directory.

## ToolRegistry Fundamentals

The `ToolRegistry` is a simple name-keyed store with:
- `register(tool)` / `unregister(name)` for lifecycle management
- `get(name)` / `has(name)` for lookup
- `get_definitions(format)` for producing backend-specific schema lists
- `execute(name, **kwargs)` for async dispatch

The `test_execute_missing` test verifies that calling a non-existent tool returns a structured error string rather than raising `KeyError` — critical for agent loops that must continue running even when a tool call fails.

```python
async def test_execute_missing(self):
    result = await registry.execute("missing_tool")
    assert "Error: Tool 'missing_tool' not found" in result
```

## ShellTool: Security and Timeouts

**Dangerous command blocking**: `rm -rf /` is rejected with `"Dangerous command blocked"`. The tool maintains a list of forbidden command patterns and checks input before spawning any subprocess. This prevents the most catastrophic accidental or adversarial commands from executing.

**Timeout enforcement**: A `ShellTool` constructed with `timeout=1` will interrupt a `sleep 2` command and return `"Command timed out"`. Without this, a misbehaving or intentionally hung command would block the agent's event loop indefinitely.

```python
async def test_timeout(self):
    tool = ShellTool(timeout=1)
    result = await tool.execute(command="sleep 2")
    assert "Command timed out" in result
```

The test uses a cross-platform command (`ping -n 5 127.0.0.1` on Windows, `sleep 2` elsewhere) to ensure the timeout behavior is tested regardless of OS.

## Filesystem Jail

The `temp_jail` fixture creates a temporary directory that acts as the sandbox root, and `mock_settings` patches `get_settings()` to return a `Settings` object pointing at that jail. This pattern is reused across many test files.

Four filesystem scenarios are tested:
- **Write then read**: A file written via `WriteFileTool` can be read back via `ReadFileTool` with matching content.
- **Jail break via traversal**: Writing to `../../outside` is blocked.
- **Prefix bypass**: A path like `/tmp/jail_sibling` that shares a prefix with the jail root but resolves outside it is blocked.
- **List directory**: `ListDirTool.execute(path=jail)` returns the contents of the jail directory.

All security tests rely on `Path.resolve()` canonicalization to prevent both `..` traversal and prefix-matching attacks.

## MockTool Fixture

The `MockTool` class implements `BaseTool` minimally — `name`, `description`, and an async `execute` that returns a formatted string. This stub is sufficient for testing the registry's dispatch and schema generation without introducing real tool side effects.

## Known Gaps

No TODOs present. The file predates some later additions (e.g., `EditFileTool`, `DirectoryTreeTool`) whose jail behavior is tested in separate files.