---
{
  "title": "DirectoryTreeTool: File System Tree Visualization with Jail, Depth, and Security Controls",
  "summary": "The `DirectoryTreeTool` renders an ASCII directory tree within a file jail, with configurable depth, hidden file visibility, file size display, and entry count truncation. Tests cover basic tree rendering, depth limiting, hidden file exclusion/inclusion, size annotation, edge cases (empty dir, nonexistent path, file-not-directory), security (jail break, prefix bypass, symlink skipping), truncation, and the tool's schema definition.",
  "concepts": [
    "DirectoryTreeTool",
    "file_jail",
    "max_depth",
    "show_hidden",
    "show_size",
    "symlink_skipping",
    "truncation",
    "tree_rendering",
    "path_traversal",
    "directory_tree"
  ],
  "categories": [
    "tool-system",
    "security",
    "testing",
    "test"
  ],
  "source_docs": [
    "23c389079400e223"
  ],
  "backlinks": null,
  "word_count": 441,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`DirectoryTreeTool` provides agents with a visual map of a directory's structure — similar to the Unix `tree` command but sandboxed within PocketPaw's file jail. Agents use this to understand project layouts before deciding which files to read or modify.

## Basic Tree Rendering

The output uses Unicode box-drawing characters (`├──`, `└──`) to show hierarchy, and includes a summary line with directory and file counts. Tests use a `_make_structure` helper that creates a sample tree with `src/`, `docs/`, nested Python files, a hidden `.hidden` file, and a `.config/` directory.

```python
assert "src/" in result
assert "├──" in result or "└──" in result
assert "directories" in result and "files" in result
```

## Depth Control

`max_depth=1` shows top-level entries but not nested ones — `helpers.py` inside `src/utils/` disappears. `max_depth=0` renders only the root line and reports `"0 directories, 0 files"` since no entries are traversed. This allows agents to get a quick high-level view before drilling in.

## Hidden File Handling

Hidden files and directories (names starting with `.`) are excluded by default. `show_hidden=True` includes them. This default prevents cluttering the tree with `.git`, `.env`, and other dotfiles that agents typically should not touch.

## File Size Annotation

File sizes are not shown by default to keep output compact. `show_size=True` adds size annotations. Agents can request sizes when making decisions about which files to read (to avoid loading large binaries).

## Security Controls

Three attack vectors against the file jail are tested:

**Jail break via traversal**: Attempting `path="../outside"` relative to the jail is blocked. The tool resolves canonical paths before any directory traversal.

**Prefix-matching bypass**: A path like `/tmp/jail_sibling` shares a string prefix with `/tmp/jail` but is outside the sandbox — blocked by canonical path comparison.

**Symlink skipping**: Symbolic links that would escape the jail are silently skipped during traversal. This prevents an attacker from placing a symlink inside the jail that points to sensitive directories outside it.

```python
async def test_symlink_is_skipped(self, ...):
    # symlink inside jail pointing outside is not followed
```

## Edge Cases

- **Empty directory**: Returns a valid tree with `"0 directories, 0 files"` — no crash.
- **Nonexistent path**: Returns an error message rather than raising.
- **File (not directory)**: Attempting to tree a file path returns an appropriate error, since `tree` is a directory operation.

## Truncation

When a directory has more entries than `max_entries`, the output is truncated with a notice, preventing enormous outputs that would overflow context windows for large projects.

## Known Gaps

No TODOs. The symlink test behavior (skip vs. error) depends on the underlying traversal implementation; the test only asserts the symlink is not followed, not what message is returned.