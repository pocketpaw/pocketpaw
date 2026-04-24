---
{
  "title": "Session and Message context_type Discriminator Validator Tests",
  "summary": "These tests verify that Pydantic model validators on Message and Session enforce strict discriminator rules, ensuring group and pocket context types carry exactly the required fields and nothing more. They also confirm backward-compatible inference when context_type is omitted.",
  "concepts": [
    "context_type",
    "discriminator",
    "model_validator",
    "Message",
    "Session",
    "Pydantic",
    "Beanie",
    "group context",
    "pocket context",
    "role validation",
    "backward compatibility",
    "inference"
  ],
  "categories": [
    "testing",
    "data validation",
    "memory models",
    "MongoDB",
    "test"
  ],
  "source_docs": [
    "46a15f5eafcd2ef8"
  ],
  "backlinks": null,
  "word_count": 439,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `context_type` discriminator is the core type-safety boundary between two communication contexts in PocketPaw: `group` messages (multi-user channels with mentions, group IDs, and senders) and `pocket` messages (one-on-one AI sessions with session keys and role labels). Without strict enforcement, a pocket message could carry a group field, silently corrupting query filters, session indexing, and notification routing.

## Why Validators Run Before Beanie

Beanie (the MongoDB ODM) performs a collection-binding step when a document is constructed successfully. The `_raises` tests don't need Beanie initialized because Pydantic's `model_validator` fires **before** the collection check — invalid models are caught cheaply in Python without touching the database. The `_passes` tests require the `beanie_memory_db` fixture because successful construction finalizes the Beanie ODM bindings.

## Message Discriminator Rules

```python
def test_group_message_requires_group_field(self):
    with pytest.raises(ValueError, match="group message must have group set"):
        Message(context_type="group", sender="u1", content="hi")

def test_group_message_cannot_carry_session_key(self):
    with pytest.raises(ValueError, match="must not set session_key"):
        Message(context_type="group", group="g1", sender="u1",
                content="hi", session_key="s1")
```

Each test targets a specific invariant:

- **Group messages** must have `group`, must not have `session_key`, must not have `role`, must not have `mentions`.
- **Pocket messages** must have `session_key`, must have a valid `role` (user/assistant/system), must not have `group`, must not have `mentions`.

The `mentions` field is group-only because at-mentions are a channel feature. AI sessions use a turn-based structure without mentions. Allowing mentions on pocket messages would break notification derivation logic that relies on `context_type` to decide whether to fire.

## Session Discriminator Rules

The `Session` model mirrors `Message` with its own set:

- **Group sessions** require a `group` field and must not carry the `pocket` field.
- **Pocket sessions** must not carry `group`.

These constraints prevent a session from being ambiguously classified, which would break the session index (`GET /sessions/runtime`), sidebar rendering, and workspace isolation queries.

## Backward-Compatible Inference

The most operationally important tests are the inference tests:

```python
async def test_context_type_inferred_when_absent_group(self, beanie_memory_db):
    m = Message(group="g1", sender="u1", content="legacy")
    assert m.context_type == "group"

async def test_context_type_defaults_to_pocket_when_no_group(self, beanie_memory_db):
    s = Session(sessionId="s1", workspace="w1", owner="u1")
    assert s.context_type == "pocket"
```

Legacy code and external API consumers that omit `context_type` are handled gracefully. The model infers the correct type from the presence of `group` (group context) or its absence (pocket context). This avoids a hard data migration while still making the type explicit for all new writes.

## Defensive Role Validation

```python
def test_pocket_message_requires_valid_role(self):
    with pytest.raises(Exception):
        Message(context_type="pocket", session_key="s1",
                role="not-a-role", content="hi")
```

The test accepts either a Pydantic `ValidationError` or a standard `ValueError` because Pydantic's Literal validator may fire before the custom `model_validator`. What matters is that an invalid role never reaches persistence.

## Known Gaps

None identified. The test suite covers the full discriminator surface for both model types.