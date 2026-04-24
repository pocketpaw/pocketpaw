---
{
  "title": "In-Memory Projection for Widget Interactions, Usage, and Co-occurrence",
  "summary": "Provides three unified read views over the widget event stream — per-interaction history, per-widget usage roll-up for graduation decisions, and per-pair co-occurrence counts for suggestion candidates — all rebuilt from a single org journal replay pass. Supersedes the JSONL-based scans from PRs #941 and #942.",
  "concepts": [
    "in-memory projection",
    "WidgetUsageProjection",
    "CooccurrenceProjection",
    "GraduationStateProjection",
    "bisect insertion",
    "session boundary",
    "signature re-derivation",
    "replay correctness",
    "accept/dismiss sets",
    "dataclasses",
    "visibility filtering",
    "single rebuild pass"
  ],
  "categories": [
    "widget-system",
    "event-sourcing",
    "projection-pattern",
    "co-occurrence"
  ],
  "source_docs": [
    "ee/widget/projection.py"
  ],
  "backlinks": null,
  "word_count": 448,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/widget/projection.py` is the read model for the widget domain. A single `WidgetProjection` instance maintains three internally consistent views built by folding widget events as they arrive from the org journal.

## Three Views in One Projection

**WidgetUsageProjection** — tracks per-`(widget_name, surface)` interaction counts within a rolling window. Each new `widget.interaction.recorded` event increments the counter for the appropriate pair. The graduation policy reads this to decide pin/fade/archive.

**CooccurrenceProjection** — tracks per-signature pair counts and example queries. A "session" is defined by the `DEFAULT_SESSION_GAP_SECONDS` gap between events; when two widgets appear in the same session, their signature's count increments. The `widget.cooccurrence.accepted` and `widget.cooccurrence.dismissed` events update accept/dismiss sets so the suggestion feed can filter already-actioned pairs.

**GraduationStateProjection** — holds the most-recent graduation verdict per `(widget_name, surface)`. Each `widget.graduated` event overwrites the previous row for that pair.

All three live on one `WidgetProjection` instance. A single `rebuild(journal, since_seq)` call populates all three, and a single `apply(entry)` call routes each event to the correct sub-projection's fold.

## Replay Correctness: The Signature Fix

On replay, the `CooccurrenceProjection.apply` method re-derives the co-occurrence signature from the raw widget names in the event payload — it does **not** use the stored signature field. This is the key to correcting the `sorted(tokens[:6])` bug from PR #942:

```python
# On apply — always re-derive to fix any historical bug in the emitter
sig = cooccurrence_signature(entry_widget_a, entry_widget_b)
```

Even if an old emitter wrote a broken signature into the event payload, the projection ignores it and computes the correct one. The stored payload signature field is only informational.

## Row Shapes

```python
@dataclass
class WidgetInteractionView:
    widget_name: str
    surface: str
    action_type: str
    actor_id: str
    scope: list[str]
    query_text: str | None
    ts: datetime

@dataclass
class WidgetUsageRow:
    widget_name: str
    surface: str
    total_interactions: int
    promoting_interactions: int
    last_seen: datetime
    first_seen: datetime

@dataclass
class CooccurrenceRow:
    signature: str
    widget_a: str
    widget_b: str
    session_count: int
    example_queries: list[str]
    accepted: bool
    dismissed: bool
```

All are plain dataclasses — no Pydantic — so the projection carries no model-machinery import cost during hot replay paths.

## Bisect-Based Insertion

The `WidgetInteractionView` list is kept sorted by timestamp using `bisect.insort`, the same pattern as `ee/retrieval/projection.py`. This allows O(log n) insertion and O(1) bounded-size maintenance without a full list sort per event.

## Visibility Filtering

All query methods pass results through `ee.fabric.policy.filter_visible` before returning, enforcing scope-based access control. A widget interaction logged under `["org:acme"]` is invisible to actors whose scope permits only `["org:beta"]`.

## Known Gaps

The session boundary for co-occurrence detection is derived at replay time from event timestamps, using the same `DEFAULT_SESSION_GAP_SECONDS` constant as the policy. If the constant changes, a full projection rebuild is needed to recompute session boundaries — there is no stored session ID in the events.