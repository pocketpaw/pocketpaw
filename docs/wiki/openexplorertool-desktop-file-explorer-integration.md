---
{
  "title": "OpenExplorerTool: Desktop File Explorer Integration",
  "summary": "The `OpenExplorerTool` lets a PocketPaw agent open any file or folder in the user's native desktop file manager after completing a task. It delegates the actual OS interaction to the dashboard REST API rather than calling OS APIs directly, which makes it work identically whether the tool is invoked in-process via the tool bridge or from a CLI subprocess spawned by the Claude SDK.",
  "concepts": [
    "OpenExplorerTool",
    "BaseTool",
    "tool bridge",
    "dashboard REST API",
    "file explorer",
    "path resolution",
    "action auto-detection",
    "in-process vs subprocess",
    "tool protocol"
  ],
  "categories": [
    "builtin tools",
    "desktop integration",
    "file system"
  ],
  "source_docs": [
    "d84b7ab081c0e8c5"
  ],
  "backlinks": null,
  "word_count": 632,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`OpenExplorerTool` is a built-in PocketPaw tool (tool name: `open_in_explorer`) that gives the agent the ability to reveal a file or folder in the operating system's native file explorer — Finder on macOS, Explorer on Windows, or Nautilus/Dolphin on Linux. Its purpose is ergonomic: after the agent creates, downloads, or locates a file the user asked about, it can automatically surface that file so the user does not have to navigate to it manually.

## Why a REST API bridge instead of direct OS calls?

The agent runtime runs in two distinct execution contexts:

1. **In-process** — the tool executes inside the same Python process as the dashboard server (tool bridge mode).
2. **Subprocess** — the tool executes inside a short-lived Claude SDK Bash subprocess that has no direct access to the host desktop session.

Calling OS-level commands like `subprocess.run(['open', path])` directly would work in the in-process case but silently fail in the subprocess case because the subprocess does not own a GUI session. By routing through `POST /api/v1/files/open` on the dashboard REST API, both execution contexts go through the same path: the dashboard server — which always runs in the desktop session — handles the OS call on behalf of the tool.

This indirection is a deliberate architectural choice: the dashboard becomes the single owner of OS-level desktop integration.

## Action auto-detection

The tool accepts an optional `action` parameter with two values:

- `navigate` — open a folder and show its contents in the explorer
- `view` — open a file using the OS default viewer

If the caller omits `action`, the tool inspects the resolved path: if it is a file, it defaults to `view`; if it is a directory, it defaults to `navigate`. This makes the tool ergonomic for agents — they can pass any path and get sensible behavior without needing to know the file type in advance.

## Path resolution and validation

Before making the API call, the tool:

1. Calls `Path(path).expanduser().resolve()` to normalize tilde expansion (`~/Downloads/report.pdf`) and resolve relative path components. This prevents the API from receiving ambiguous paths.
2. Checks `resolved.exists()` and returns an error if the path does not exist. This prevents the dashboard API from receiving a path that would silently fail or raise an unhandled exception on the server side.

These two steps are defensive: without path resolution, the dashboard might receive `~/foo` literally, which would fail on some OS APIs. Without the existence check, the error surfaces in the dashboard server logs rather than as a clean error message to the user.

## Integration with BaseTool

`OpenExplorerTool` extends `BaseTool` from `pocketpaw.tools.protocol`, which provides the standard tool contract — `name`, `description`, `parameters` (JSON Schema), and `execute`. The `parameters` schema marks `path` as required and `action` as optional with an enum of `["navigate", "view"]`, which guides the LLM to pass only valid values.

```python
class OpenExplorerTool(BaseTool):
    """Open a file or folder in the user's desktop file explorer."""

    @property
    def name(self) -> str:
        return "open_in_explorer"

    async def execute(self, path: str, action: str | None = None) -> str:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            return self._error(f"Path not found: {path}")
        if action is None:
            action = "view" if resolved.is_file() else "navigate"
        # POST /api/v1/files/open via dashboard REST API
```

## Known Gaps

No explicit TODO or FIXME annotations are present in the source. However, the tool has implicit limitations worth noting:

- **No URL support**: The tool only handles local filesystem paths. There is no path for `http://` or `file://` URIs.
- **No permission check**: The tool does not verify that the path falls within the configured `file_jail_path` before sending it to the dashboard API. The safety boundary is enforced only at the API layer, not at the tool layer.
- **Single-path only**: There is no batch variant for opening multiple files at once.