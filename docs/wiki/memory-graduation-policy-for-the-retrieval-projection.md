---
{
  "title": "Memory Graduation Policy for the Retrieval Projection",
  "summary": "Implements the access-count-based graduation logic that promotes memories across tiers (episodic to semantic, semantic to core, procedural promotion) by scanning the journal-backed retrieval projection rather than a flat JSONL file. All tuning thresholds are carried verbatim from PR #937 to preserve runtime behaviour after the storage layer refactor.",
  "concepts": [
    "graduation policy",
    "memory tiers",
    "episodic",
    "semantic",
    "core memory",
    "access-count thresholds",
    "GraduationDecision",
    "GraduationReport",
    "soul mutation",
    "two-phase apply",
    "JSONL migration",
    "journal-backed projection"
  ],
  "categories": [
    "memory-graduation",
    "retrieval",
    "policy-engine",
    "soul-protocol-integration"
  ],
  "source_docs": [
    "ee/retrieval/policy.py"
  ],
  "backlinks": null,
  "word_count": 527,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/retrieval/policy.py` contains the decision engine that looks at how often memories have been retrieved and proposes tier changes — a process called "graduation." When a memory is accessed frequently enough, it graduates from a transient tier (episodic) to a more durable one (semantic or core), reflecting growing importance.

## Why Graduation Exists

Soul-protocol's memory model uses tiers to represent the confidence and permanence of a memory. A freshly observed fact starts as episodic. If the agent retrieves it repeatedly — because users keep asking about the same topic — the system infers it is foundational knowledge and moves it to semantic or core storage. This prevents high-value memories from being evicted by TTL rules designed for transient observations.

## The Three Decision Types

```python
GraduationKind = Literal[
    "episodic_to_semantic",
    "semantic_to_core",
    "promote_procedural",
]
```

Each `GraduationDecision` dataclass carries the `memory_id`, the proposed `kind`, a `reason` string, and the access counts that triggered the decision. The companion `GraduationReport` wraps a list of decisions with scan metadata (window start/end, total memories scanned) so callers can audit why decisions were made.

## Threshold Values

Three tuning defaults govern all graduation decisions:

- `DEFAULT_WINDOW_DAYS = 30` — only count retrievals in the past 30 days
- `DEFAULT_EPISODIC_THRESHOLD = 10` — 10+ accesses in the window promotes episodic to semantic
- `DEFAULT_SEMANTIC_THRESHOLD = 50` — 50+ accesses promotes semantic to core

These values were ported verbatim from PR #937. The code comments explicitly note that the refactor is not the place to re-tune them: the thresholds were chosen for subjective feel ("10 accesses in a month feels important") and any re-tuning belongs in a dedicated follow-up that can be configured per-pocket.

## Projection-Backed Scan vs. JSONL Scan

PR #937 scanned a `RetrievalLogEntry` JSONL file on disk. This policy module replaces that by scanning `RetrievalProjection` rows — an in-memory view rebuilt from the append-only org journal. The change is architecturally significant:

1. **No file I/O during scans** — the projection is already in memory; `scan_for_graduations` is a pure Python fold
2. **No asyncio.Lock needed** — write serialization is handled by SQLite WAL at the journal layer
3. **Multi-process safe** — two processes scanning simultaneously both read the same projection state rebuilt from the same journal, rather than racing on a file

## Soul Mutation Mirror

The internal `_mutate_soul` helper mirrors PR #937's `soul.remember()` call so the in-memory soul reflects the graduation decision immediately — without waiting for a full soul reload. This matters because soul-protocol's in-memory state is the live read path; if the soul isn't updated synchronously with the journal event, queries made within the same request cycle would see stale tier data.

## Emit Path

Decisions are not applied inline — `scan_for_graduations` returns a `GraduationReport`. The caller (typically the router's `POST /graduation/scan` or an apply endpoint) iterates the decisions and calls `RetrievalJournalStore.log_graduation`, which emits `graduation.applied` events onto the journal and folds them back into the projection. This two-phase design keeps policy logic separate from I/O and makes it trivially testable.

## Known Gaps

Thresholds are hardcoded at the module level. There is no per-pocket configuration path yet. A follow-up is explicitly called out in the source comments to make them configurable.