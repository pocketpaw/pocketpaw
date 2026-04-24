---
{
  "title": "AGENTS.md Loader: Git-Aware File Discovery with Mtime Caching",
  "summary": "`AgentsMdLoader` walks the directory tree upward from a starting path — stopping at git repository roots — to find the nearest AGENTS.md file, parses it into a structured `AgentsMd` object, and caches results by `(path, mtime)` so repeated calls within a session cost nothing. A 32 KiB file size cap prevents oversized AGENTS.md files from bloating the agent's system prompt.",
  "concepts": [
    "AgentsMdLoader",
    "AgentsMd",
    "git root detection",
    "directory traversal",
    "mtime caching",
    "32KiB size cap",
    "constraints_block",
    "system prompt injection",
    "filesystem walk",
    "section parsing"
  ],
  "categories": [
    "agents",
    "configuration",
    "file system",
    "caching"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 497,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`loader.py` provides the runtime mechanism for locating and reading AGENTS.md files. The search strategy deliberately mirrors how tools like `git` and `.editorconfig` resolve configuration: start at the working directory and walk toward the filesystem root, so the most specific (most local) configuration takes precedence.

## Search Algorithm

The search in `AgentsMdLoader.find_and_load` works as follows:

1. Start at the given directory
2. Check if `AGENTS.md` exists in the current directory
3. If a `.git` directory is found, stop — this is the repository root and there is no point searching parent repositories
4. Walk up one level and repeat, up to `_MAX_WALK_DEPTH = 20` levels

The `.git` stop condition prevents the agent from accidentally picking up an AGENTS.md from a parent monorepo or home directory when the user's actual project root is a subdirectory. Without this guard, a `~/AGENTS.md` in the home directory could inject constraints intended for a completely different project.

The `_MAX_WALK_DEPTH = 20` cap prevents runaway traversal on pathological setups (e.g., deeply nested directories on a filesystem with no `.git` root). 20 is generous — real projects rarely exceed 10 levels of nesting.

## Size Cap and Injection Prevention

```python
_MAX_BYTES = 32_768  # 32 KiB
```

If the discovered AGENTS.md exceeds 32 KiB, the loader refuses to parse it and logs a warning. This is a security and performance boundary: the file's contents end up in the agent's system prompt, which has token limits and cost implications. A malicious or poorly maintained AGENTS.md that is hundreds of kilobytes could exhaust the context window or significantly increase API costs.

## Mtime-Based Caching

`_CacheEntry` pairs a parsed `AgentsMd` with the file's modification time at the moment of parsing. `AgentsMdLoader` stores cache entries keyed by absolute path. On each `find_and_load` call it checks whether the on-disk mtime matches the cached mtime — if so, returns the cached result immediately; if not, re-parses.

This design means that rapid repeated calls (e.g., on every agent turn in a long session) are essentially free after the first parse, while edits to the AGENTS.md mid-session are picked up automatically within one turn.

## AgentsMd Structure

The `AgentsMd` dataclass holds:

- `path` — absolute path to the file (for debugging)
- `raw_content` — the unmodified markdown text
- `sections` — a dict mapping section headings to their body text, parsed from markdown

Two computed properties provide convenient access:

- `constraints_block` — returns only the "Constraints" section content, the part most commonly injected into system prompts
- `preview` — a truncated summary for logging and UI display

## Known Gaps

- The section parser uses simple heading detection (lines starting with `#`). Nested sections and ATX vs setext headings may not parse identically to a full markdown parser.
- There is no support for AGENTS.md files with frontmatter (YAML metadata block), which some projects use to specify machine-readable metadata.
- Cache eviction is never-on-size-growth: the cache grows unbounded if the loader is used across many directories in a long-running server process.
