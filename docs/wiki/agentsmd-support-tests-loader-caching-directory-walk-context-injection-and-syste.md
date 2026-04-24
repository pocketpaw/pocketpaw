---
{
  "title": "AGENTS.md Support Tests: Loader, Caching, Directory Walk, Context Injection, and SystemEvent Emission",
  "summary": "Tests for the AGENTS.md feature (issue #456) that allows project-specific constraints to be injected into the agent's system prompt. Covers the `AgentsMdLoader` find-and-cache logic (including `.git` boundary stopping, nearest-file preference, and mtime-based cache invalidation), `AgentsMd` formatting, `AgentContextBuilder` injection, and `AgentLoop` `SystemEvent` emission when the file is found.",
  "concepts": [
    "AGENTS.md",
    "AgentsMdLoader",
    "mtime caching",
    "git boundary",
    "constraints_block",
    "AgentContextBuilder",
    "SystemEvent",
    "system prompt injection",
    "directory walk",
    "context window"
  ],
  "categories": [
    "testing",
    "agent loop",
    "project configuration",
    "context management",
    "test"
  ],
  "source_docs": [
    "383ef284f7a3ca04"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_agents_md.py` tests the AGENTS.md support added in issue #456. AGENTS.md is a project-level constraints file (inspired by similar tools in the Claude ecosystem) that project maintainers place in their repository to guide agent behaviour. The loader finds the file, parses it into sections, and the context builder injects a formatted `constraints_block` into the agent's system prompt.

## Why AGENTS.md Matters

Without per-project constraints, agents operating in a codebase have no way to know project-specific rules: which test runner to use, which directories are off-limits, which coding style to follow. AGENTS.md provides this without requiring changes to PocketPaw's configuration.

## AgentsMd Dataclass

`AgentsMd` wraps the raw file content and provides two computed properties:
- `constraints_block`: a formatted section including the file path and content, inserted into the system prompt.
- `preview`: the first 197 characters followed by `…` if the content is longer. Used in the `SystemEvent` notification to the frontend.

The 197-character limit is tested explicitly:
```python
def test_preview_long_content_truncated(self, tmp_path):
    content = "x" * 300
    assert len(agents_md.preview) == 198  # 197 chars + ellipsis
    assert preview.endswith("…")
```

## Loader: Find and Cache Logic

`AgentsMdLoader.find_and_load(start_dir)` walks up the directory tree from `start_dir` looking for `AGENTS.md`:
- **Nearest wins**: if both `subdir/AGENTS.md` and `parent/AGENTS.md` exist, the one in `subdir` is returned.
- **Git boundary stop**: the walk stops at a directory containing `.git`, even if the file is absent at that level. This prevents the loader from crossing repository boundaries in a monorepo.
- **Git root included**: an `AGENTS.md` at the `.git` root is still loaded — the stop happens after checking the directory, not before.

```python
def test_stops_at_git_boundary(self, tmp_path):
    # .git at parent level — walk must not cross into grandparent.
    (parent / ".git").mkdir()
    (grandparent / "AGENTS.md").write_text("Grandparent rules")
    result = loader.find_and_load(child)
    assert result is None  # Grandparent file not visible
```

## Mtime-Based Caching

The loader caches loaded files keyed by `(path, mtime)`. If the file is unchanged, subsequent calls return the cached `AgentsMd` without re-reading disk. If the file's mtime changes, the cache is invalidated:

```python
def test_cache_invalidates_on_mtime_change(self, tmp_path):
    p.write_text("Version 1")
    r1 = loader.find_and_load(tmp_path)
    time.sleep(0.01)  # ensure mtime advances
    p.write_text("Version 2")
    r2 = loader.find_and_load(tmp_path)
    assert r1 is not r2
    assert "Version 2" in r2.raw_content
```

Large files are truncated to `_MAX_BYTES` before caching to prevent the agent's context window from being overwhelmed by a very large AGENTS.md.

## Context Builder Injection

`AgentContextBuilder.build_system_prompt` injects the `constraints_block` when a directory is configured and the file is found. When `agents_md_dir` is `None` or the file is missing, the prompt is unchanged. Critically, if `AgentsMdLoader` raises an exception, the prompt build must still succeed:

```python
async def test_agents_md_failure_does_not_break_prompt(self, tmp_path):
    with patch.object(AgentsMdLoader, "find_and_load", side_effect=OSError("disk error")):
        prompt = await builder.build_system_prompt(...)
    assert prompt is not None  # Must not propagate the OSError
```

## SystemEvent Emission

When the loader finds an AGENTS.md, the `AgentLoop` emits an `agents_md_loaded` `SystemEvent` so the frontend can display a notification. When the file is absent, no event is emitted.

## Known Gaps

No test covers symlinked AGENTS.md files or circular symlinks in the directory tree. The `_parse_sections` function is tested only for `#` level-1 headings — deeper heading levels are not covered.