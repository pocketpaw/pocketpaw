---
{
  "title": "EditFileTool: Find-and-Replace File Editing with Jail Security Tests",
  "summary": "This suite tests EditFileTool, a built-in agent tool that performs find-and-replace edits on files within a sandboxed jail directory. It covers successful edits, missing file errors, old_string-not-found errors, multiple-occurrence ambiguity errors, and the file jail security boundary that prevents agents from editing files outside their allowed working area.",
  "concepts": [
    "EditFileTool",
    "find_and_replace",
    "old_string",
    "new_string",
    "file_jail",
    "ambiguous_edit",
    "file_not_found",
    "old_string_missing",
    "multiple_occurrences",
    "path_traversal",
    "Settings",
    "get_settings",
    "filesystem_tools",
    "agent_capabilities"
  ],
  "categories": [
    "testing",
    "tools",
    "filesystem",
    "security",
    "agent-capabilities",
    "test"
  ],
  "source_docs": [
    "52fa6a6f7f70a9bf"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_edit_file.py` tests `pocketpaw.tools.builtin.filesystem.EditFileTool`, which allows PocketPaw agents to make targeted text edits to files by specifying an exact `old_string` to replace with a `new_string`. This is one of the core filesystem tools in the agent toolkit, modeled after Claude Code's own edit-file semantics.

## Why This Module Exists

Agents frequently need to modify files — updating configuration, patching source code, editing documents. A raw write-file approach would require the agent to read the entire file, modify it in memory, and rewrite it — error-prone for large files and lossy for concurrent edits. `EditFileTool` provides surgical edits by targeting only the changed region.

## Basic Edit

`test_edit_file_basic` creates a file with content "Hello World", runs `execute(path=..., old_string="World", new_string="PocketPaw")`, and verifies both the success message and the updated file content. The result string contains "replacement" to signal a successful edit, which the agent can parse to confirm the operation.

## File Not Found

`test_edit_file_not_found` confirms that attempting to edit a non-existent file returns an error string containing "Error:" and "not found" rather than raising a Python exception. Tools in PocketPaw follow the convention of returning error strings so that agents receive the error as tool output and can handle it — for example, by creating the file first.

## old_string Not Found

`test_edit_file_old_string_missing` tests the case where the `old_string` doesn't appear in the file. Rather than silently making no change, the tool returns an error. This is critical for correctness: a silent no-op would leave the agent believing the edit succeeded when it did not, potentially causing downstream logic errors.

## Multiple Occurrences (Ambiguity)

`test_edit_file_ambiguous` tests what happens when `old_string` appears multiple times in the file. The tool rejects ambiguous edits and returns an error explaining that multiple matches were found. This matches the semantic of a precise surgical edit — if the target is ambiguous, the tool refuses rather than guessing which occurrence to replace.

This design mirrors the philosophy behind Claude Code's edit tool: ambiguous edits are bugs waiting to happen, and the tool should force the caller to provide enough context to uniquely identify the replacement target.

## File Jail Security

`test_edit_file_jail` (inferred from the suite's security pattern) tests that paths outside the `file_jail_path` are rejected. Just as with `DeliverArtifactTool`, `EditFileTool` reads the jail boundary from `Settings` and rejects any path that resolves outside it after normalization (handling `../` traversal). A compromised agent cannot edit `/etc/passwd` or SSH keys.

The `mock_settings` fixture patches `pocketpaw.tools.builtin.filesystem.get_settings` to use a temporary directory as the jail, isolating tests from the real filesystem.

## Tool Definition

The `@pytest.mark.asyncio` decorator on all async tests follows PocketPaw's test conventions. The fixture approach (injecting `mock_settings` via pytest fixture rather than manual patching inside each test) keeps tests concise and ensures the settings patch is always active for the test's duration.

## Known Gaps

Encoding edge cases (UTF-8 files with multi-byte characters, BOM-prefixed files) are not tested. Symlink traversal through the jail boundary is not explicitly tested — a symlink inside the jail pointing outside it might bypass the path check depending on whether normalization follows symlinks.
