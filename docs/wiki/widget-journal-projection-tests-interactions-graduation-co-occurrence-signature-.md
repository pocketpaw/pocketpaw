---
{
  "title": "Widget Journal Projection Tests: Interactions, Graduation, Co-occurrence Signature Fix, and Router",
  "summary": "Comprehensive tests for the widget journal projection subsystem covering the write path for interactions, graduations, and co-occurrence events; scope containment in usage roll-ups; the pin-threshold graduation policy; a regression guard for the `sorted(tokens[:6])` co-occurrence signature bug from PR #942; archive rules for inactive widgets; and the three GET router endpoints.",
  "concepts": [
    "widget journal",
    "co-occurrence signature",
    "graduation policy",
    "WidgetJournalStore",
    "WidgetProjection",
    "scope containment",
    "archive rule",
    "regression guard",
    "sorted tokens bug",
    "projection rebuild equivalence"
  ],
  "categories": [
    "testing",
    "enterprise features",
    "widget system",
    "projection",
    "test"
  ],
  "source_docs": [
    "9da4b69454f87209"
  ],
  "backlinks": null,
  "word_count": 490,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/ee/test_widget_journal.py` was created in `feat/widget-journal-projection` as part of Wave 3 / Org Architecture RFC, Phase 3. It supersedes two held PRs: #941 (widget graduation engine over a JSONL log) and #942 (co-occurrence detector over that same log, which shipped with a `sorted(tokens[:6])` bug). The projection-based replacement fixes both while making the invariants explicit in tests.

## Subsystem Architecture

The widget subsystem mirrors the retrieval subsystem structure:
- **Store** (`WidgetJournalStore`): async write methods for interactions, graduations, and co-occurrences.
- **Projection** (`WidgetProjection`): folds journal events into in-memory state (usage counts, pin decisions, co-occurrence pairs, archive flags).
- **Policy** (`scan_for_widget_graduations`, `scan_for_cooccurrences`, `apply_widget_graduations`): stateless scanners that read projection state and emit decision events.
- **Router**: FastAPI endpoints for usage, graduation state, and co-occurrence queries.

## Key Test Classes

### TestWritePath
Verifies field fidelity for all three write methods. Notably, `log_cooccurrence` computes the signature internally — the test asserts that the stored signature matches `cooccurrence_signature(widget_a, widget_b)` computed separately, proving the store does not accept a caller-supplied signature (which would allow signature spoofing).

### TestScopeContainment
A caller scoped to `org:sales:*` sees its own scope's widget usage but not events from `org:support:*`. Cross-scope leakage would be a data isolation bug affecting multi-tenant deployments.

### TestCooccurrenceSignatureFix — The #942 Regression Guard

```python
def test_long_query_signature_stable_across_token_rotation(self):
    # Query with > 6 tokens — the old sorted(tokens[:6]) bug
    # produced different prefixes for rotated inputs.
    tokens_a = ["renewal", "discount", "alpha", "beta", "gamma", "delta", "epsilon"]
    tokens_b = tokens_a[3:] + tokens_a[:3]  # rotated
    sig_a = cooccurrence_signature(" ".join(tokens_a), " ".join(tokens_b))
    sig_b = cooccurrence_signature(" ".join(tokens_b), " ".join(tokens_a))
    assert sig_a == sig_b
```

The original bug: `sorted(tokens[:6])` truncated before sorting, so two token sets that were permutations of each other could have different 6-token prefixes and thus different signatures. The fix: sort all tokens, then hash. This test would have failed under the original implementation, making regression impossible to miss.

### TestUsageProjection
N `open` interactions for the same widget cross `DEFAULT_PIN_THRESHOLD` and produce exactly one pin decision. Below threshold: no decision. Only "promoting" action types (open, click) count — view-only actions do not.

### TestArchiveRule
An old, inactive widget (no interactions for > archive period) should be flagged as archived after a projection rebuild. The fixture writes a timestamped old event and then re-opens the journal (forcing a cold rebuild) to ensure the archive rule applies during rebuild, not just during incremental folding.

### TestIncrementalEqualsRebuild
Incremental folding via the store must produce the same projection state as a cold rebuild from scratch. This equivalence check prevents the subtle class of bug where the incremental path and the rebuild path diverge on edge-case events.

### TestRouter
Three GET endpoints are tested: `GET /widgets/usage`, `GET /widgets/usage/{name}`, and `GET /widgets/graduation/state`. Tests verify cold-start empty envelopes and that writes made via the store surface through the endpoints.

## Known Gaps

No test covers the `TestArchiveRule` path where the interaction is exactly at the archive boundary (age == archive threshold). Off-by-one errors in the archive rule are not caught.