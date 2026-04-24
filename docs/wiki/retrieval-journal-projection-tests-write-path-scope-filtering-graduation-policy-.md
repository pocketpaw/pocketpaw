---
{
  "title": "Retrieval Journal Projection Tests: Write Path, Scope Filtering, Graduation Policy, and REST Router",
  "summary": "Comprehensive tests for the `ee.retrieval` subsystem that journals every memory-retrieval query and drives a graduation policy promoting frequently-retrieved memories to higher tiers. Covers event write-path fidelity, scope containment, correlation grouping, threshold-based graduation decisions, projection rebuild equivalence, and the REST router's GET endpoints.",
  "concepts": [
    "retrieval journal",
    "graduation policy",
    "RetrievalJournalStore",
    "projection rebuild",
    "scope containment",
    "soul_protocol",
    "memory tier promotion",
    "correlation_id",
    "FastAPI router",
    "lru_cache isolation"
  ],
  "categories": [
    "testing",
    "memory management",
    "enterprise features",
    "projection",
    "test"
  ],
  "source_docs": [
    "8936b781c9521275"
  ],
  "backlinks": null,
  "word_count": 506,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/ee/test_retrieval_journal.py` was created in `feat/retrieval-journal-projection` as part of Wave 3 / Org Architecture RFC, Phase 3. It supersedes two previously held PRs — #936 (JSONL retrieval sink) and #937 (graduation policy over that JSONL) — because those designs would have shipped without the projection-based invariants this file pins.

## Subsystem Architecture

The retrieval subsystem has four layers:
- **Store** (`RetrievalJournalStore`): async write methods that append events to the shared `Journal`.
- **Projection** (`RetrievalProjection`): reads the journal to rebuild in-memory state (candidate hit counts, graduation decisions).
- **Policy** (`scan_for_graduations`, `apply_decisions`): stateless functions that inspect projection state and emit `graduation.applied` events when thresholds are crossed.
- **Router** (`ee.retrieval.router`): FastAPI endpoints (`GET /retrieval/recent`, `GET /retrieval/session/{id}`, `GET /graduation/state`) backed by a cached `RetrievalJournalStore`.

## Test Class Breakdown

### TestWritePath
Asserts event payload fidelity — every field passed to `log_retrieval` must appear verbatim in the resulting `retrieval.query` journal event. This matters because downstream consumers (graduation policy, analytics) read directly from journal payloads; a silent field drop would produce invisible data loss.

Also tests that `log_retrieval` and `log_graduation` both reject an empty `scope` list — the journal refuses scope-less entries and the store must validate before writing.

### TestScopeFilter
A caller scoped to `org:sales:*` should see retrievals tagged with `org:sales:leads` (containment match) but not events tagged with `org:support:*`. This scope containment is the same model used across Widget, Retrieval, and Fabric — a regression here would silently expose cross-org data.

### TestCorrelationView
`retrievals_by_correlation(correlation_id)` groups all retrieval events from a single agent run into chronological order. Correct grouping is required by the UI's session replay view and by the graduation policy when checking whether the same memory appeared in multiple runs.

### TestGraduationPolicy
The graduation policy elevates a memory when it appears in at least `DEFAULT_EPISODIC_THRESHOLD` distinct retrieval runs as a candidate. Tests verify:
- N retrievals of the same `memory_id` produce exactly one pin decision.
- N-1 retrievals produce no decision (no false promotions).
- Semantic-tier memories use a separate `DEFAULT_SEMANTIC_THRESHOLD` and promote to `core` rather than `episodic`.
- `apply_decisions` emits a `graduation.applied` event visible via the projection.
- `apply_decisions` is a no-op when the soul file is missing — prevents a crash from halting the journal write path.

### TestEmptyJournalRebuild and TestIncrementalEqualsRebuild
Projection correctness is verified two ways:
1. Rebuild on an empty journal returns zero state (no crash, no phantom decisions).
2. Applying N events incrementally produces the same projection state as a cold rebuild from scratch. This equivalence check was motivated by the held PRs' JSONL design, where incremental and rebuild paths diverged.

### TestRouter
End-to-end tests mounting the router on a fresh FastAPI app:

```python
@pytest.fixture
def app(tmp_path):
    a = FastAPI()
    a.include_router(router)
    a.dependency_overrides[get_journal] = lambda: open_journal(tmp_path / "journal.db")
    return a
```

Verifies cold-start empty envelopes, that direct journal writes surface through the endpoint, scope query parameter filtering, 404 on missing session, and the graduation state endpoint listing current decisions.

## Known Gaps

The `apply_decisions_skipped_when_soul_missing` test asserts a no-op but does not verify that a warning is logged. A silent skip could mask misconfiguration in production deployments.