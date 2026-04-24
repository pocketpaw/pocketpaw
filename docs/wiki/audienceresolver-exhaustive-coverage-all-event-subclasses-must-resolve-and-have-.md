---
{
  "title": "AudienceResolver Exhaustive Coverage: All Event Subclasses Must Resolve and Have Unique Wire Types",
  "summary": "These tests use Python's inspect module to enumerate every Event subclass at runtime and verify AudienceResolver handles each without raising. A companion test enforces that no two event classes share the same EVENT_TYPE wire string, preventing silent routing collisions when new event types are added.",
  "concepts": [
    "AudienceResolver",
    "coverage test",
    "inspect module",
    "Event subclass enumeration",
    "EVENT_TYPE",
    "wire type uniqueness",
    "room-scoped events",
    "ConnectionManager",
    "meta-test",
    "drift prevention",
    "self-maintaining test"
  ],
  "categories": [
    "testing",
    "real-time",
    "event routing",
    "code quality",
    "test"
  ],
  "source_docs": [
    "85006599faf688d2"
  ],
  "backlinks": null,
  "word_count": 317,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The audience coverage test is a meta-test: it verifies the resolver does not crash for any event that exists in the codebase. This catches a specific drift pattern — a developer adds a new `Event` subclass, forgets to add the corresponding branch to `AudienceResolver`, and the omission goes unnoticed until a user receives no event that should have been delivered.

## Runtime Enumeration Pattern

```python
def _all_event_subclasses() -> list[type[Event]]:
    return [
        cls for _name, cls in inspect.getmembers(ev_mod, inspect.isclass)
        if issubclass(cls, Event) and cls is not Event
    ]
```

By enumerating subclasses from the `ee.cloud.realtime.events` module at test time rather than hardcoding a list, the test automatically picks up new event types. There is no need to update this test when adding a new event.

## Common Payload Strategy

```python
common_payload = {
    "group_id": "g1", "user_id": "u1", "sender_id": "u1",
    "peer_id": "p1", "workspace_id": "w1", "invite_id": "i1",
    "member_ids": ["u1", "u2"], "message_id": "m1",
    "emoji": "x", "file_id": "f1", "id": "n1", "kind": "mention",
}
for cls in subclasses:
    ev = cls(data=dict(common_payload))
    result = await resolver.audience(ev)
    assert isinstance(result, list)
```

The common payload includes every field any resolver branch touches, preventing false failures from a `KeyError` rather than a missing branch.

## Wire Type Uniqueness Test

```python
def test_subclasses_have_unique_wire_types():
    seen: dict[str, str] = {}
    for cls in _all_event_subclasses():
        wire = getattr(cls, "EVENT_TYPE", None)
        assert wire, f"{cls.__name__} missing EVENT_TYPE"
        assert wire not in seen
        seen[wire] = cls.__name__
```

If two event classes share a wire type string, the bus cannot distinguish them. The resolver's branching is based on `ev.type` (set from `EVENT_TYPE`), so a collision would route one class's events through the other's handler.

## Room-Scoped Events

Typing events (`typing.*`) intentionally return `[]` from the resolver because they are handled by `ConnectionManager` directly, not the fan-out bus. The coverage test accepts `[]` as valid, so these pass without a special case.

## Known Gaps

None identified. The introspection approach makes this test self-maintaining.