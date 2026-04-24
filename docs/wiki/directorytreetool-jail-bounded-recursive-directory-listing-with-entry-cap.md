---
{
  "title": "DirectoryTreeTool: Jail-Bounded Recursive Directory Listing with Entry Cap",
  "summary": "`DirectoryTreeTool` generates a `tree`-style directory listing with configurable depth, hidden-file filtering, and optional file sizes — all bounded by the `file_jail_path` and a hard `MAX_ENTRIES = 500` cap. Path resolution uses `is_relative_to()` to prevent traversal outside the allowed root.",
  "concepts": [
    "DirectoryTreeTool",
    "file_jail",
    "MAX_ENTRIES",
    "path_traversal_prevention",
    "depth_limiting",
    "hidden_files",
    "recursive_walk",
    "is_relative_to",
    "directory_listing",
    "BaseTool"
  ],
  "categories": [
    "tools",
    "filesystem",
    "security",
    "directory-navigation"
  ],
  "source_docs": [
    "022f3e1aa2434cf0"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tree.py` implements the `directory_tree` tool, which renders a recursive directory listing in the same visual style as the Unix `tree` command. It exists because agents frequently need to understand project layouts before editing files, and a formatted tree is more readable (and more token-efficient) than running `ls` repeatedly at each level.

## Security: File Jail Check

```python
jail = get_settings().file_jail_path.resolve()
if not dir_path.is_relative_to(jail):
    return self._error(f"Access denied: {path} is outside allowed directory")
```

The jail check is the first validation after path resolution (after `expanduser().resolve()`). `resolve()` collapses symlinks and `..` segments before the `is_relative_to()` check, preventing traversal attacks like `/jail/../../etc`. Without this, an agent could list any directory on the host filesystem.

The check runs before the directory-existence test — this is intentional. If a non-existent path is outside the jail, the error should be "access denied," not "not found," so the tool doesn't inadvertently reveal filesystem structure beyond the jail boundary.

## Entry Cap

```python
MAX_ENTRIES = 500
```

The 500-entry hard cap exists because a naive recursive tree of a large codebase (e.g., a `node_modules` directory) would produce millions of lines, exhausting LLM context. When truncation occurs, the `_walk` method returns `True` (truncated), and the tool appends a warning to the output so the caller knows the listing is incomplete.

## Depth Limiting

`max_depth` defaults to 3. The `_walk` recursive method receives both `depth` (current recursion level) and `max_depth` (ceiling), stopping when `depth >= max_depth`. This prevents infinite recursion on deeply nested or circular directory structures (symlink loops, for example, could create infinite recursion without the depth check).

## Hidden File Filtering

```python
show_hidden: bool = False
```

Hidden entries (those whose names start with `.`) are excluded by default. This is the right default for project layout exploration — `.git`, `.venv`, `__pycache__` add noise without helping the agent understand the project's logical structure. The `show_hidden=True` option exists for use-cases like inspecting dotfiles.

## Optional Size Display

```python
show_size: bool = False
```

File sizes are disabled by default because they add significant token overhead (each file gets an additional size annotation). The `_format_size` helper converts raw bytes to human-readable units (B, KB, MB, GB). Size display is useful for storage audits but distracting for layout comprehension.

## Output Structure

The output follows the Unix `tree` format:

```
/path/to/dir
├── file.py
├── subdir/
│   ├── nested.py
│   └── other.py
└── README.md

2 directories, 3 files
```

A summary line counts directories and files, providing a quick sanity check that matches what `tree` produces natively.

## Known Gaps

- Symlink loops are partially handled by the depth limit but not explicitly detected — a loop at depth 1 would still recurse to `max_depth` before stopping.
- The 500-entry cap is a module constant, not a parameter — callers can't adjust it per-invocation.
- Sorting within each directory (alphabetical, dirs-first, etc.) is left to the `_walk` implementation which was not fully shown.
