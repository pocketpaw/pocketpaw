---
{
  "title": "Context Window Budget Tests: Priority-Based Block Assembly and KB Integration",
  "summary": "This test module, added in April 2026, validates `AgentContextBuilder._assemble_with_budget`, which assembles the agent's system prompt from priority-ranked blocks while respecting both a global character budget and per-block caps. It also tests the knowledge-base context injection path that calls the `kb-go` CLI subprocess to fetch relevant articles.",
  "concepts": [
    "AgentContextBuilder",
    "_assemble_with_budget",
    "_Priority",
    "_INJECTION_CAPS",
    "_DEFAULT_BUDGET_CHARS",
    "context window",
    "system prompt",
    "kb-go",
    "knowledge base injection",
    "priority-based assembly",
    "character budget",
    "subprocess mocking"
  ],
  "categories": [
    "agent runtime",
    "testing",
    "context management",
    "knowledge base",
    "test"
  ],
  "source_docs": [
    "a7cfcc9fd7645ff2"
  ],
  "backlinks": null,
  "word_count": 546,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

LLMs have finite context windows. PocketPaw's `AgentContextBuilder` must fit identity, memory, channel hints, sender info, and knowledge-base snippets into that window without overflowing it. Simply concatenating all blocks and truncating at the end would silently drop the most important content last. `_assemble_with_budget` solves this with a priority-aware assembler, and this test file is the specification for its behavior.

## Priority Tiers

The assembler works with `_Priority` enum values: `CRITICAL`, `HIGH`, `MEDIUM`, and `LOW`. The core guarantee is that higher-priority blocks are never sacrificed before lower-priority ones when budget is tight.

`test_low_priority_dropped_first` constructs a scenario where a `CRITICAL` block (800 chars) plus a `HIGH` block (100 chars) fit in the budget, but adding a `LOW` block (200 chars) would overflow. The test asserts the `LOW` block is absent in the output while both others are present.

`test_critical_never_dropped` pushes the budget to an extreme: only 100 characters total, but a 500-character `CRITICAL` block. Rather than dropping it, the assembler truncates the `CRITICAL` block to fit the budget and drops the `MEDIUM` block entirely. The output must start with the critical content — the agent's identity prompt survives even in the most constrained conditions.

## Per-Block Character Caps (`_INJECTION_CAPS`)

Beyond the global budget, each block type has an independent maximum. This prevents a single enormous memory context from consuming the entire budget and starving identity or channel hints.

`test_per_block_caps_applied` constructs a `memory_context` block far larger than `_INJECTION_CAPS["memory_context"]` and confirms the result is truncated to the cap value with a truncation marker appended. The test reads the cap directly from `_INJECTION_CAPS` rather than hardcoding it, so it stays correct if the cap value changes.

## Edge Cases

`test_empty_blocks_skipped` ensures blocks with empty or whitespace-only content don't contribute separators or whitespace to the output — preventing the prompt from being padded with blank lines.

`test_all_blocks_fit_within_budget` is the happy-path baseline: with a generous budget, all three test blocks appear verbatim in the result.

`test_budget_chars_kwarg` confirms the budget can be overridden via keyword argument, enabling callers to pass a model-specific window size at runtime.

`test_priority_ordering_preserved` verifies that blocks appear in their original declaration order within the same priority tier, not reordered by insertion sequence.

`test_default_budget_is_generous` asserts `_DEFAULT_BUDGET_CHARS` is at least 8,000 characters, preventing accidental regressions where the default is set to a tiny value.

## KB Context Injection (`TestKbContext`)

PocketPaw optionally injects knowledge-base articles into the system prompt via the `kb-go` CLI binary. The `TestKbContext` class tests this integration path with subprocess mocking.

`test_empty_query_returns_empty` and `test_empty_scope_returns_empty` confirm that the KB fetch is skipped entirely when the query or scope is blank — avoiding unnecessary subprocess launches.

`test_missing_binary_returns_empty` confirms graceful degradation when the `kb-go` binary is not installed. The agent continues without KB context rather than crashing.

`test_successful_kb_fetch` patches `asyncio.create_subprocess_exec` to simulate a `kb-go` process that returns a JSON payload, then asserts the extracted text appears in the context string.

`test_kb_context_has_injection_cap` confirms `_INJECTION_CAPS` includes an entry for `"kb_context"`, ensuring KB results are subject to the same budget discipline as other blocks.

## Known Gaps

No TODO or FIXME markers were found. The `test_critical_never_dropped` test's assertion `assert len(result) <= 100` is somewhat loose — it confirms the result fits the budget but does not assert the exact truncation point or the presence of a truncation marker, which `test_per_block_caps_applied` does check for capped blocks.