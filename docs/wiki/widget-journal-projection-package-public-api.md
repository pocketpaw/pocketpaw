---
{
  "title": "Widget Journal Projection Package — Public API",
  "summary": "Re-exports the complete public surface of the widget journal projection domain, including the store, projection classes, policy functions, and all canonical event names and payload builders. This module also documents why the package supersedes held PRs #941 and #942 and ships the fix for a token-sort bug that broke co-occurrence deduplication.",
  "concepts": [
    "package facade",
    "re-exports",
    "widget graduation",
    "co-occurrence detection",
    "token-sort bug fix",
    "JSONL migration",
    "signature deduplication",
    "WidgetProjection",
    "WidgetJournalStore",
    "action names",
    "Wave 3 refactor"
  ],
  "categories": [
    "widget-system",
    "event-sourcing",
    "package-structure",
    "co-occurrence"
  ],
  "source_docs": [
    "ee/widget/__init__.py"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/widget/__init__.py` is the package-level façade for the widget journal projection subsystem. Its primary job is to aggregate and re-export the symbols that external callers need, so they can write `from ee.widget import WidgetJournalStore` rather than navigating the internal module hierarchy.

## Context: What This Package Supersedes

The widget domain had two held PRs:

- **PR #941** — a widget graduation engine that read from `~/.pocketpaw/widget-interactions.jsonl`. It tracked how often each widget was interacted with and decided whether to pin (promote), fade, or archive it.
- **PR #942** — a co-occurrence detector stacked on top of #941's JSONL file. It identified widget pairs that were frequently used together in the same session and surfaced them as suggestions.

Both PRs were superseded by the org journal approach in Wave 3. This package replaces the JSONL file with three journal event types, and the `__init__.py` documents the transition explicitly so future contributors understand why the file-based approach was abandoned.

## The Bug Fix Shipped Here

PR #942 shipped with a subtle token-sort bug in its co-occurrence signature function:

```python
# #942's broken version — truncates before sorting
sorted(tokens[:6])

# Fixed version — sorts first, then truncates
sorted(tokens)[:6]
```

The distinction matters: for a query with 8 tokens, the broken version sorts only the first 6 (whatever order they happened to appear), producing different signatures depending on word order. The fixed version always sorts the full token list first, then takes the top 6 — so the signature is stable regardless of how the tokens were ordered in the original query.

Because the signature is re-derived from the raw widget pair **on replay**, out-of-band emitters that still carry the old bug cannot poison the projection's co-occurrence state. The projection is authoritative.

## What Is Re-Exported

The `__init__.py` re-exports three categories of symbols:

**From `ee.widget.events`:** Action names (`ACTION_WIDGET_INTERACTION_RECORDED`, `ACTION_WIDGET_GRADUATED`, `ACTION_WIDGET_COOCCURRENCE_DETECTED`, `ACTION_WIDGET_COOCCURRENCE_ACCEPTED`, `ACTION_WIDGET_COOCCURRENCE_DISMISSED`), payload builders, and signature utilities (`cooccurrence_signature`, `normalise_signature_tokens`, `SIGNATURE_MAX_TOKENS`).

**From `ee.widget.policy`:** Graduation and co-occurrence scanning functions (`scan_for_widget_graduations`, `apply_widget_graduations`, `scan_for_cooccurrences`, `apply_cooccurrences`), decision classes (`WidgetGraduationDecision`, `WidgetGraduationReport`, `CooccurrenceCandidate`, `CooccurrenceReport`), tier enum (`WidgetTier`), and all tuning defaults.

**From `ee.widget.projection`:** The main `WidgetProjection` class and its sub-projections (`CooccurrenceProjection`, `GraduationStateProjection`), plus all public row shapes (`WidgetInteractionView`, `WidgetUsageRow`, `CooccurrenceRow`, `GraduationStateRow`).

## Design Note

The store (`WidgetJournalStore`) is also re-exported but not shown in the abbreviated `__init__.py` listing — callers that need the write path import it from here. Keeping the store in the package namespace means consumers never need to know whether the write path lives in `store.py` or is later refactored into a sub-package.

## Known Gaps

No known gaps are flagged in the source. The module's own comment acknowledges that the JSONL migration closes the major correctness issues from #941 and #942.