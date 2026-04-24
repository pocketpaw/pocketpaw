---
{
  "title": "Soul-Protocol Integration Tests: End-to-End PocketPaw Wiring",
  "summary": "Integration tests that validate the full lifecycle of the soul-protocol package within PocketPaw, including bootstrap prompt generation, memory observe-and-recall, tool bridge discovery of soul tools, and corrupt file recovery. All tests are conditionally skipped when `soul-protocol` is not installed.",
  "concepts": [
    "soul-protocol",
    "SoulManager",
    "tool_bridge",
    "bootstrap prompt",
    "soul_recall",
    "soul_observe",
    "corrupt file recovery",
    "_reset_manager",
    "optional dependency"
  ],
  "categories": [
    "testing",
    "soul integration",
    "memory",
    "test"
  ],
  "source_docs": [
    "a68e0de73b9662bf"
  ],
  "backlinks": null,
  "word_count": 419,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's soul layer is an optional integration with the `soul-protocol` SDK. When installed, it gives the AI companion persistent identity, memory, and personality across sessions. These integration tests exercise the wiring between the two packages end-to-end — no mocks, real objects.

## Conditional Execution

The module uses `pytestmark = pytest.mark.skipif(not _has_soul_protocol(), ...)` to skip the entire suite when `soul-protocol` is not installed. This is essential for the PocketPaw test suite to remain runnable in environments (CI, lightweight installs) that don't include the optional dependency. Without this guard, an `ImportError` would fail the entire test run.

## State Isolation

The `_reset_soul()` autouse fixture calls `_reset_manager()` before and after every test. `SoulManager` is a module-level singleton — without explicit reset, state from one test (a loaded soul file, an active autosave loop, a cached tool list) would bleed into the next, producing order-dependent failures that are notoriously hard to debug.

## Bootstrap Prompt Generation

`test_bootstrap_provider_generates_prompt` verifies that the `soul_bridge` produces a non-empty system prompt from the soul's identity and memory. This prompt is prepended to every agent conversation, giving the LLM the soul's name, personality, and recent context. If the bootstrap provider were broken, the agent would behave as a generic assistant with no persistent identity.

## Observe and Recall

`test_bridge_observe_and_recall` exercises the memory write-read cycle: an event is observed (written to memory), and then a recall query retrieves it. This is the core of the soul's persistence model. The test ensures the bridge layer correctly delegates to the underlying soul-protocol memory store.

## Full Lifecycle

`test_manager_full_lifecycle` initialises the manager, runs several operations, and shuts down cleanly. It validates that the manager survives a complete session without leaking resources (open file handles, running tasks).

## Tool Discovery

`test_soul_tools_injected_into_tool_bridge` verifies that when the soul is active, `tool_bridge` discovers all soul-exposed tools and makes them callable by the agent. Soul tools (like `soul_recall`, `soul_observe`) must appear in the tool bridge's registry, or the agent cannot use them.

## Corrupt File Recovery

`test_corrupt_file_recovery_end_to_end` writes a malformed soul file to disk and confirms the manager recovers gracefully by birthing a new soul rather than crashing. This failure scenario arises from interrupted saves, disk errors, or manual edits.

## Known Gaps

The source text is truncated before the full method bodies are visible. Based on the AST, the class appears complete, but any `TODO` annotations inside method bodies are not visible from the extracted structure.

```python
# Skip guard pattern used at module level
pytestmark = pytest.mark.skipif(
    not _has_soul_protocol(), reason="soul-protocol not installed"
)
```
