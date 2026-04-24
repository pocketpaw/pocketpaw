---
{
  "title": "Event Model Structural Tests: Base Event Fields and Typed Subclass Wire Type Assignment",
  "summary": "Two minimal tests verify the structural contract of the Event base model: that every Event has type, data, and ts fields; and that typed subclasses like GroupCreated automatically set the correct wire type string from EVENT_TYPE without the caller needing to pass it.",
  "concepts": [
    "Event model",
    "GroupCreated",
    "EVENT_TYPE",
    "wire type",
    "ts field",
    "Pydantic model",
    "WebSocket protocol",
    "typed subclass",
    "real-time events",
    "structural test"
  ],
  "categories": [
    "testing",
    "real-time",
    "event model",
    "data validation",
    "test"
  ],
  "source_docs": [
    "1b10eb1c47bb58e8"
  ],
  "backlinks": null,
  "word_count": 320,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `Event` model is the base type for every real-time event in PocketPaw's WebSocket layer. It carries three fields: `type` (the wire type string, e.g., `group.created`), `data` (an arbitrary dict payload), and `ts` (an auto-generated timestamp). These two tests pin the structural invariants of the model hierarchy.

## Base Event Construction

```python
def test_event_has_type_data_ts():
    ev = Event(type="x.y", data={"a": 1})
    assert ev.type == "x.y"
    assert ev.data == {"a": 1}
    assert ev.ts is not None
```

The `ts` field is auto-populated on construction. The test only asserts `is not None` because the exact timestamp is ephemeral and varies per run.

## Typed Subclass Wire Type

```python
def test_typed_event_subclass_sets_type():
    ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1"]})
    assert ev.type == "group.created"
    assert ev.data["group_id"] == "g1"
```

`GroupCreated` sets `type` automatically from `EVENT_TYPE = "group.created"` defined on the class. Callers do not pass `type=` when constructing typed events — the subclass handles it. This prevents typos and ensures the wire type is always the canonical string defined in one place.

## Why These Tests Matter

Even though these tests are simple, they confirm the `Event` model hierarchy is importable and structurally sound. If someone accidentally removed a required field or changed a default, these tests fail immediately, catching the regression before more complex tests run.

The `test_audience_coverage.py` exhaustive test depends on this model structure being correct — it would produce misleading results if the base `Event` class were broken.

## Relationship to Wire Protocol

The `type` field is what the WebSocket client uses to dispatch received events to the correct handler. If `GroupCreated.type` were ever `None` or wrong, the client would silently ignore group creation events.

## Known Gaps

No test currently verifies the serialization format of `ts` (UTC ISO string vs. Unix timestamp vs. Python datetime). This could matter if the frontend JavaScript client expects a specific format.

No test verifies behavior when a typed subclass receives an empty or incomplete `data` dict.