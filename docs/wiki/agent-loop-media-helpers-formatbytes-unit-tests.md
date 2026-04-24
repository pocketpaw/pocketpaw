---
{
  "title": "Agent Loop Media Helpers: _format_bytes Unit Tests",
  "summary": "Focused unit tests for the `_format_bytes` helper in `pocketpaw.agents.loop`, verifying that file sizes are formatted correctly across the B, KB, MB, and GB ranges. This utility generates human-readable size strings for media attachment prompts injected into the agent's context.",
  "concepts": [
    "_format_bytes",
    "media attachments",
    "human-readable file sizes",
    "binary prefix",
    "KB",
    "MB",
    "GB",
    "agent loop helpers",
    "unit tests",
    "boundary conditions"
  ],
  "categories": [
    "testing",
    "agent loop",
    "media handling",
    "utilities",
    "test"
  ],
  "source_docs": [
    "1bf399507b3f1f8f"
  ],
  "backlinks": null,
  "word_count": 422,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_agent_loop_media.py` tests `_format_bytes`, a single private helper exported from `pocketpaw.agents.loop`. The function formats raw byte counts into human-readable strings for use in media attachment prompts that the agent loop injects when a user attaches a file to a conversation.

## Why This Helper Needs Tests

When a user attaches an image or document, the agent loop prepends a prompt like `"Attached file: report.pdf (1.4 MB)"` to the message context. If `_format_bytes` produces incorrect output — for example, showing `"1024 B"` instead of `"1.0 KB"` — the agent receives a misleading size hint. For very large files this could cause the agent to underestimate whether the content fits in its context window.

The boundary conditions (exactly 1024, 1024*1024, etc.) are the most likely sources of off-by-one errors, so the tests deliberately probe the edges:

```python
def test_under_1kb_shows_bytes(self):
    assert _format_bytes(0) == "0 B"
    assert _format_bytes(1) == "1 B"
    assert _format_bytes(1023) == "1023 B"  # Just below 1 KB

def test_kb_range(self):
    assert _format_bytes(1024) == "1.0 KB"       # Exactly 1 KB
    assert _format_bytes(414255) == "404.5 KB"   # Non-trivial conversion
    assert _format_bytes(1024 * 1024 - 1) == "1024.0 KB"  # Just below 1 MB

def test_mb_range(self):
    assert _format_bytes(1024 * 1024) == "1.0 MB"  # Exactly 1 MB
    assert _format_bytes(5_242_880) == "5.0 MB"

def test_gb_range(self):
    assert _format_bytes(1024 * 1024 * 1024) == "1.0 GB"
    assert _format_bytes(2 * 1024 * 1024 * 1024) == "2.0 GB"
```

The `1024 * 1024 - 1` → `"1024.0 KB"` case is interesting: a naive implementation that rounds up would show `"1.0 MB"` for this value, which is technically above the 1 MB threshold but has not crossed it. The test pins the expected rounding behaviour so any change to the rounding strategy is immediately visible.

## Implementation Pattern

The helper follows the standard binary prefix ladder: divide by 1024 until the value drops below 1024, format with one decimal place, and suffix with the appropriate unit. The `0 B` edge case (zero bytes) is explicitly tested because some implementations skip the zero check and produce `"0.0 KB"` or a division error.

## Scope

This file is intentionally narrow — it imports only `_format_bytes` and exercises it with pure arithmetic inputs. No mocking, no fixtures, no async. This makes it fast, deterministic, and safe to run in any environment.

## Known Gaps

No test covers negative byte values (which could appear if a file size is reported as -1 by a malformed attachment). No test covers very large values (> 1 TB) where the GB formatting would produce values above 1000.