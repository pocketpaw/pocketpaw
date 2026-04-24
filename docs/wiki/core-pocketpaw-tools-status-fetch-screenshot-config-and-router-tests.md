---
{
  "title": "Core PocketPaw Tools: Status, Fetch, Screenshot, Config, and Router Tests",
  "summary": "This test module provides broad coverage of PocketPaw's foundational tool and configuration layer, including the system status tool, the file fetch tool's path jail enforcement, the screenshot tool's optional dependency handling, the settings save/load lifecycle, and both the LLM and agent routers' initialization and error paths.",
  "concepts": [
    "StatusTool",
    "FetchTool",
    "file_jail",
    "path_traversal",
    "ScreenshotTool",
    "Settings",
    "LLM_router",
    "agent_router",
    "is_safe_path",
    "pyautogui",
    "get_config_dir"
  ],
  "categories": [
    "tool-system",
    "security",
    "testing",
    "test"
  ],
  "source_docs": [
    "15fc5aa67e9364e5"
  ],
  "backlinks": null,
  "word_count": 484,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

This file tests several independent but foundational subsystems of PocketPaw: the status tool, the fetch tool's security sandbox, the screenshot tool, the settings system, the LLM router, and the agent router. Together these form the scaffolding on which all higher-level agent features are built.

## Status Tool

The `StatusTool` wraps `get_system_status()` which returns a formatted string containing CPU, RAM, and disk utilization with percentage values. Tests verify that the return type is a string and that it contains the expected section headers and `%` signs. This tool is typically exposed to agents for self-monitoring.

## Fetch Tool: File Jail Security

The fetch tool uses a "file jail" — a root directory outside which no file access is permitted. The `is_safe_path` function resolves paths and checks that the canonical resolved path starts with (or equals) the jail root. Four attack vectors are tested:

- **Within jail**: Normal access — allowed
- **Outside jail**: Sibling directory — blocked
- **Parent traversal**: `jail/../outside` resolves to outside the jail — blocked
- **Prefix bypass**: `jail_outside` shares a string prefix with `jail` but is a different directory — blocked (uses canonical path comparison, not string prefix matching)

The prefix bypass is the most subtle: naive implementations that check `str(path).startswith(str(jail))` would incorrectly allow `jail_outside/`. Using `Path.resolve()` with proper parent comparison prevents this.

```python
def test_is_safe_path_prefix_bypass(self, tmp_path):
    jail = tmp_path / "jail"
    outside_with_prefix = tmp_path / "jail_outside"
    assert is_safe_path(outside_with_prefix, jail) is False
```

The `handle_path` async function dispatches on whether the path points to a file or directory, returning structured results that include the type and relevant metadata.

## Screenshot Tool

The screenshot tool wraps `pyautogui`, an optional desktop automation dependency. Tests verify:
- Returns bytes or string when `pyautogui` is available
- Returns a graceful error string when `pyautogui` is not installed (rather than raising `ImportError`)
- Catches and surfaces exceptions from `pyautogui.screenshot()` without crashing

This is the same graceful-degradation pattern used throughout PocketPaw for optional desktop dependencies.

## Settings System

Tests verify the `Settings` Pydantic model:
- Default values are correct out of the box
- Settings can be serialized to disk and reloaded with identical values (round-trip persistence)
- `get_config_dir()` creates the config directory if it does not exist, preventing failures on first run

## LLM Router

The LLM router manages conversation history and backend dispatch. Tests confirm:
- Initialization succeeds with default settings
- `clear_history()` resets the conversation state
- When no backend is configured, a descriptive error string is returned rather than raising

## Agent Router

The agent router selects the active agent backend (Claude Agent SDK vs. legacy backends). Tests verify:
- `claude_agent_sdk` is initialized when configured
- Legacy backend configuration falls back to the appropriate handler

## Known Gaps

The test for `handle_path_directory` asserts `"keyboard" in result`, which appears to be a typo or copy-paste artifact — the expected key is likely `"entries"` or `"children"`. This may be a latent test bug.