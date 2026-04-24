---
{
  "title": "Widget Graduation and Co-occurrence Policy Engine",
  "summary": "Implements the pin/fade/archive graduation logic for individual widgets and the threshold-based co-occurrence candidate scanner, both ported verbatim from PRs #941 and #942 onto the journal-backed projection. Decision objects and tuning knobs are preserved under their original names so existing tests and configuration carry over without renaming.",
  "concepts": [
    "widget graduation",
    "pin/fade/archive",
    "co-occurrence scan",
    "WidgetGraduationDecision",
    "CooccurrenceCandidate",
    "WidgetTier",
    "session gap",
    "threshold tuning",
    "AgentProposal pattern",
    "two-phase apply",
    "usage roll-up"
  ],
  "categories": [
    "widget-system",
    "policy-engine",
    "co-occurrence",
    "memory-graduation"
  ],
  "source_docs": [
    "ee/widget/policy.py"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/widget/policy.py` is the decision engine for the widget domain. It answers two questions:

1. **Should this widget be promoted, demoted, or archived?** (`scan_for_widget_graduations`)
2. **Are these two widgets co-occurring often enough to suggest as a pair?** (`scan_for_cooccurrences`)

Both decisions are derived from the `WidgetProjection` — an in-memory replay of widget journal events — rather than from disk files.

## Graduation: Pin / Fade / Archive

Widget graduation uses three tiers defined in `WidgetTier`:

- **Pinned** — widget is actively promoted in the UI
- **Active** (default)
- **Faded** — widget is de-emphasized
- **Archived** — widget is hidden

The scan computes usage counts per `(widget_name, surface)` pair over a rolling window:

- Widgets with 10+ "promoting" interactions (open, edit, click) in 30 days → pin
- Widgets untouched for 60+ days → archive
- Everything else → fade (if previously pinned but now below threshold)

These thresholds — `DEFAULT_PIN_THRESHOLD = 10`, `DEFAULT_ARCHIVE_DAYS = 60`, `DEFAULT_WINDOW_DAYS = 30` — are ported verbatim from PR #941. The comments are explicit that re-tuning belongs in a future slice, not the migration.

`scan_for_widget_graduations` returns a `WidgetGraduationReport` containing `WidgetGraduationDecision` objects. The caller passes decisions to `apply_widget_graduations`, which calls `WidgetJournalStore.log_widget_graduation` per decision — emitting `widget.graduated` events and folding them into the projection.

## Co-occurrence: Threshold Scan

The co-occurrence scan walks the projection's usage roll-up (`CooccurrenceProjection.pairs`) and emits `CooccurrenceCandidate` objects for pairs whose session-co-occurrence count exceeds `DEFAULT_COOCCURRENCE_THRESHOLD = 3` in a `DEFAULT_COOCCURRENCE_WINDOW_DAYS = 14` day window.

```python
CooccurrenceCandidate(
    signature=row.signature,
    widget_a=row.widget_a,
    widget_b=row.widget_b,
    session_count=row.session_count,
    example_queries=row.example_queries,
)
```

The candidate carries the stable signature (already fixed by `normalise_signature_tokens` in `events.py`), so the policy does not recompute signatures — it trusts the projection.

## Why Not soul-protocol's AgentProposal?

The source comments explicitly address why `graduation.applied` events don't use soul-protocol's `AgentProposal` / `HumanCorrection` primitive: widget graduation is a system-derived decision from usage counts, not a human-reviewed proposal. There is no summary, no reviewer disposition, and no back-and-forth. If a future slice adds human-in-the-loop approval (captain reviews proposed widget pins before they apply), it can wrap these decisions in `agent.proposed` / `human.corrected` pairs without changing the projection.

## Tuning Defaults Summary

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_WINDOW_DAYS` | 30 | Graduation look-back window |
| `DEFAULT_PIN_THRESHOLD` | 10 | Interactions to pin |
| `DEFAULT_ARCHIVE_DAYS` | 60 | Inactivity before archive |
| `DEFAULT_SESSION_GAP_SECONDS` | 1800 | Session boundary gap |
| `DEFAULT_COOCCURRENCE_THRESHOLD` | 3 | Pair count to suggest |
| `DEFAULT_COOCCURRENCE_WINDOW_DAYS` | 14 | Co-occurrence look-back |

## Known Gaps

All thresholds are module-level defaults. There is no per-pocket or per-surface override mechanism yet. The session gap constant (`DEFAULT_SESSION_GAP_SECONDS = 1800`) is also hardcoded, which means a user who pauses mid-session for more than 30 minutes will have their interactions split into two separate sessions for co-occurrence counting.