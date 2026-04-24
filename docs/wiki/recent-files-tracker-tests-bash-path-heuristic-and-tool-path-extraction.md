---
{
  "title": "Recent Files Tracker Tests: Bash Path Heuristic and Tool Path Extraction",
  "summary": "`RecentFilesTracker` maintains a most-recently-used list of files accessed by the agent, enabling context-aware suggestions and memory seeding. These tests cover the Bash command path heuristic (which was missing and is now implemented), tool-specific path extraction for Read/Write/Edit/Bash tools, and tracker deduplication and ordering behavior.",
  "concepts": [
    "RecentFilesTracker",
    "Bash path heuristic",
    "file path extraction",
    "tool path keys",
    "LRU ordering",
    "deduplication",
    "recent files",
    "Bash tool",
    "missing implementation fix",
    "str_replace_editor"
  ],
  "categories": [
    "testing",
    "file tracking",
    "agent context",
    "test"
  ],
  "source_docs": [
    "f74a23541bbab43e"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw tracks which files the agent has recently accessed to provide relevant context — for example, suggesting recently opened files in the soul's working memory. The `RecentFilesTracker` intercepts tool calls and extracts file paths. The Bash tool required a special heuristic because it doesn't have a structured `file_path` argument.

## The Missing Heuristic: Why These Tests Exist

The module docstring explicitly documents a bug fix:

> `[FI] Fix: implement missing Bash command path heuristic in recent_files.py.`
> `The _TOOL_PATH_KEYS dict listed "Bash": [] with a comment saying the path would be "handled separately via heuristic", but no heuristic existed.`

This is a case where a placeholder comment (`# handled separately via heuristic`) described intent but was never implemented. The result: file paths accessed via Bash (`cat /tmp/report.txt`) were silently never recorded. `TestExtractPathFromBash` was written to document the bug and drive the fix.

## Bash Path Heuristic

`TestExtractPathFromBash` validates the heuristic that scans Bash command strings for file paths:

- **Absolute paths**: `cat /tmp/report.txt` → `/tmp/report.txt`.
- **Python script paths**: `python /app/main.py` → `/app/main.py`.
- **Tilde paths**: `~/.config/paw.yaml` is recognized.
- **Relative paths**: `./script.sh` and `../config.json` are recognized.
- **First path only**: when multiple paths appear, the first is returned.
- **No path**: a command like `git status` returns `None`.
- **Pure directory without extension ignored**: `/home/user/` (directory, no extension) is not recorded as a file.
- **Trailing punctuation stripped**: if the path ends with `,` or `.` (from surrounding sentence context), it is stripped.

## Tool-Specific Path Extraction

`TestExtractPathFromTool` covers the structured `_TOOL_PATH_KEYS` path for all non-Bash tools:

- `Read`, `Write`, `Edit` tools use a `file_path` parameter key.
- `ReadFile` tool supports both `path` and `file_path` keys (compatibility with two naming conventions).
- `str_replace_editor` is handled (used by some Claude Code integrations).
- Unknown tools return `None` rather than raising.
- Bash tool with an absolute path in the `command` key is handled by the heuristic.
- Bash tool with a `cmd` key variant is also handled.
- Empty command and missing command key both return `None`.

## Tracker Integration

`TestRecentFilesTrackerBash` are integration tests that use a real (monkeypatched) tracker:

- `cat /tmp/file.txt` → path is recorded in the tracker.
- `git status` → no path recorded.
- Accessing the same file twice deduplicates the entry.
- Re-accessing a file moves it to the top of the list (LRU ordering).

LRU ordering ensures that suggestions reflect the most recently active files, not merely the first files ever accessed.

## Known Gaps

- No test for very long Bash commands where the path might appear after the first 10 tokens.
- No test for Bash heredoc patterns (`cat << EOF > /tmp/file`).
- The `pure directory without extension` rule is pragmatic but could miss files legitimately named without extensions (e.g., `Makefile`, `Dockerfile`).