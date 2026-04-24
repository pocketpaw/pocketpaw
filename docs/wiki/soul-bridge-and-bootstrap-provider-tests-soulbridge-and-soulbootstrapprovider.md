---
{
  "title": "Soul Bridge and Bootstrap Provider Tests: SoulBridge and SoulBootstrapProvider",
  "summary": "These tests cover the `SoulBootstrapProvider` — which translates a Soul Protocol soul object into a `BootstrapContext` for PocketPaw's agent runtime — and `SoulBridge`, the async shim that wraps soul `observe()` and `recall()` calls with error-swallowing guarantees. Together they ensure the paw module can safely integrate with soul-protocol without crashing when the soul is unavailable or misbehaves.",
  "concepts": [
    "SoulBootstrapProvider",
    "SoulBridge",
    "BootstrapContext",
    "soul.observe",
    "soul.recall",
    "error swallowing",
    "soul-protocol integration",
    "AsyncMock",
    "agent bootstrap",
    "soul state"
  ],
  "categories": [
    "testing",
    "soul integration",
    "agent runtime",
    "test"
  ],
  "source_docs": [
    "8b0c33173a4ed260"
  ],
  "backlinks": null,
  "word_count": 486,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's `paw` module bridges the agent runtime with Soul Protocol. Two classes handle this bridge:

- **`SoulBootstrapProvider`**: reads a soul object and produces a `BootstrapContext` (name, identity, style) that is injected into the agent's system prompt at startup.
- **`SoulBridge`**: wraps `soul.observe()` and `soul.recall()` with async-safe, exception-swallowing wrappers so a soul failure never crashes the agent conversation.

## SoulBootstrapProvider: Context Translation

The `TestSoulBootstrapProvider` class validates that `get_context()` correctly maps soul fields into the `BootstrapContext` schema:

```python
async def test_get_context_uses_soul_name(self, mock_soul):
    provider = SoulBootstrapProvider(mock_soul)
    ctx = await provider.get_context()
    assert ctx.name == "TestSoul"
```

Each field is tested independently rather than in a single assertion block. This isolation means a regression in mood mapping doesn't hide a separate regression in energy mapping.

The style field is tested separately for both `mood` and `energy`, because they come from `soul.state` attributes that may not always be present. `test_get_context_default_style_when_no_state_attrs` covers the case where a soul has no state object — the provider must return a safe default rather than raising `AttributeError`.

`test_get_context_survives_self_model_exception` addresses a real failure mode: `soul.self_model` may raise if the self-model subsystem is unavailable. The provider catches this and continues, ensuring the agent still boots even when personality introspection fails.

## SoulBridge.observe(): Fire-and-Forget with Safety

`TestSoulBridgeObserve` has three tests:

1. **Happy path**: `observe()` passes an `Interaction` object to `soul.observe()` correctly.
2. **ImportError swallowing**: if `soul-protocol` is not installed, the import fails silently — the agent continues without soul observation rather than crashing.
3. **Exception swallowing**: if `soul.observe()` raises any exception, it is caught and ignored.

This design reflects a deliberate product decision: observation is a best-effort enrichment. Missing an observation is acceptable; crashing the user's conversation is not.

## SoulBridge.recall(): Defensive Memory Retrieval

`TestSoulBridgeRecall` verifies:

- **Content extraction**: `recall()` returns plain strings extracted from memory objects' `.content` attribute, not raw memory objects.
- **Limit parameter**: the `limit` arg is forwarded to `soul.recall()`.
- **Default limit**: when called without a limit, defaults to 5 memories.
- **Empty list on exception**: any failure in `soul.recall()` returns `[]` rather than propagating.
- **Empty list on empty soul results**: when the soul has no matching memories, `[]` is returned.

```python
async def test_recall_returns_empty_list_on_exception(self, mock_soul):
    mock_soul.recall.side_effect = RuntimeError("soul db locked")
    bridge = SoulBridge(mock_soul)
    result = await bridge.recall("anything")
    assert result == []
```

This prevents a locked or unavailable soul database from breaking agent context injection.

## Shared Fixture Design

The `mock_soul` fixture is shared across all three test classes and fully specifies the soul interface used by both `SoulBootstrapProvider` and `SoulBridge`. Using `AsyncMock` for async methods (`remember`, `recall`, `observe`, `edit_core_memory`, `save`) and `MagicMock` for synchronous properties keeps tests hermetic from the actual soul-protocol library.

## Known Gaps

- No test covers the case where `soul.to_system_prompt()` raises — only `self_model` exceptions are tested for resilience.
- The `test_get_context_to_system_prompt_runs_without_error` test verifies the method doesn't crash but doesn't assert on the output format.
- No test for concurrent `recall()` calls or soul locking under parallelism.