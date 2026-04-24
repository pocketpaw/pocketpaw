---
{
  "title": "Filesystem Tools: Read, Write, List, and Edit Files",
  "summary": "The `filesystem.py` module provides four foundational `BaseTool` subclasses — `ReadFileTool`, `WriteFileTool`, `ListDirTool`, and `EditFileTool` — that give the PocketPaw agent controlled access to the local filesystem. All path operations go through an `is_safe_path` guard that enforces the configured `file_jail_path` boundary, preventing agents from reading or writing outside the permitted sandbox.",
  "concepts": [
    "ReadFileTool",
    "WriteFileTool",
    "ListDirTool",
    "EditFileTool",
    "is_safe_path",
    "file_jail_path",
    "path resolution",
    "find-and-replace",
    "sandbox",
    "BaseTool",
    "trust level"
  ],
  "categories": [
    "builtin tools",
    "filesystem",
    "security",
    "agent capabilities"
  ],
  "source_docs": [
    "c06485fd14ca44a8"
  ],
  "backlinks": null,
  "word_count": 537,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Filesystem access is one of the most common agent capabilities, and also one of the most dangerous to expose without boundaries. The `filesystem.py` module (created 2026-02-02, last updated 2026-03-12 to add `EditFileTool`) provides four tools that cover the full CRUD surface for files and directories while routing every path through a safety check.

## ReadFileTool

Tool name: `read_file`. Reads a file and returns its contents as a string. Accepts an optional `encoding` parameter (default `utf-8`). The file path is checked with `is_safe_path` before reading to ensure it falls within the jail. This prevents an agent from reading sensitive system files like `/etc/passwd` or credential stores outside the permitted directory.

## WriteFileTool

Tool name: `write_file`. Writes a string to a file, creating parent directories with `mkdir(parents=True, exist_ok=True)` if needed. The `exist_ok=True` flag is intentional: it avoids a race condition where a concurrent write could create the parent between the check and the mkdir call. Path safety is checked before writing.

## ListDirTool

Tool name: `list_dir`. Lists directory contents, optionally including hidden files (`show_hidden` flag, default `False`). Hidden files (those starting with `.`) are filtered out by default because they often contain sensitive configuration (`.env`, `.git`, `.ssh`). Exposing them requires an explicit opt-in from the caller.

## EditFileTool

Tool name: `edit_file`. Added in March 2026, this tool performs a surgical find-and-replace on a file: it replaces the first (or all, if `replace_all=True`) occurrence of an exact `old_string` with `new_string`. Trust level is elevated (`trust_level` property returns a non-default value) because editing files is a write-with-precision operation — a wrong `old_string` match could corrupt a file.

The design rationale for string-based editing rather than line-number-based editing: line numbers change as files are modified, so a line-number reference captured in one agent turn may be stale by the next. An exact string match is self-validating — if the string is not found, the edit is rejected rather than silently applied to the wrong location.

```python
class EditFileTool(BaseTool):
    """Edit a file by replacing an exact string match with new content."""

    async def execute(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        ...
```

## The is_safe_path boundary

Every tool in this module calls `is_safe_path` from `pocketpaw.tools.fetch` before any filesystem operation. This function checks that the resolved path is a descendant of `get_settings().file_jail_path`. The jail boundary prevents a compromised or misbehaving agent from:

- Reading API keys stored in home directory dotfiles
- Writing malicious scripts to system paths
- Traversing symlinks that escape the jail

Without this guard, a prompt-injected instruction like "read /Users/me/.ssh/id_rsa" would succeed.

## Path resolution pattern

```python
file_path = Path(path).expanduser().resolve()
```

`expanduser()` handles tilde paths. `resolve()` collapses `..` components and follows symlinks to their real destination before the jail check runs. This is critical: without resolving first, a path like `/allowed/../../etc/passwd` could bypass a naive prefix check.

## Known Gaps

- **No binary file support**: `ReadFileTool` always decodes as text. Reading binary files (images, compiled artifacts) will raise a `UnicodeDecodeError`.
- **No atomic write**: `WriteFileTool` writes non-atomically. A crash mid-write leaves a partial file. A write-to-temp-then-rename pattern would be safer.
- **EditFileTool has no dry-run mode**: There is no way to preview what the edit would change before committing it.