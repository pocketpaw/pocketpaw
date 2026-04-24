---
{
  "title": "Soul Bridge: Connecting soul-protocol to PocketPaw's Agent Loop",
  "summary": "soul_bridge.py provides two classes: SoulBootstrapProvider maps soul state and memories into the BootstrapContext consumed by AgentContextBuilder while preserving tool documentation from the default provider, and SoulBridge exposes a simple observe/recall interface with transparent fallback to older soul-protocol API versions.",
  "concepts": [
    "SoulBootstrapProvider",
    "SoulBridge",
    "BootstrapContext",
    "BootstrapProviderProtocol",
    "observe",
    "recall",
    "context_for",
    "soul-protocol",
    "version compatibility",
    "mood",
    "energy",
    "hasattr guard"
  ],
  "categories": [
    "paw",
    "soul-protocol",
    "bootstrap"
  ],
  "source_docs": [
    "715f8533aaf24eed"
  ],
  "backlinks": null,
  "word_count": 416,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`soul_bridge.py` is the adapter layer between soul-protocol's `Soul` object and PocketPaw's internal bootstrap and agent loop protocols. Neither soul-protocol nor PocketPaw's core should need to know about the other's internals, but they need to exchange identity, state, and memory at runtime. This module is that exchange point.

## SoulBootstrapProvider

`SoulBootstrapProvider` implements `BootstrapProviderProtocol` by wrapping a `Soul` instance. The bootstrap system is how PocketPaw agents receive their system prompt before each invocation.

The provider instantiates `DefaultBootstrapProvider` internally and calls `get_context()` to get `instructions` (tool documentation from INSTRUCTIONS.md) and `user_profile` (USER.md contents). These are preserved in the returned `BootstrapContext`:

```python
default_ctx = await self._default.get_context()
return BootstrapContext(
    name=soul.name,
    identity=system_prompt,        # from soul.to_system_prompt()
    soul="I am a persistent AI companion powered by soul-protocol.",
    style="; ".join(style_parts),
    instructions=default_ctx.instructions,  # INSTRUCTIONS.md preserved
    user_profile=default_ctx.user_profile,  # USER.md preserved
    knowledge=knowledge,
)
```

Without preserving `instructions`, the agent would lose access to its tool documentation. The soul provides identity and personality; the default provider supplies operational knowledge.

## Mood and Energy in Style Hints

State fields (`mood`, `energy`, `tired_threshold`) are accessed via `hasattr` guards to prevent `AttributeError` when running against an older soul-protocol version that does not expose these fields.

## SoulBridge: Observe and Recall

`SoulBridge` provides a two-method interface for the agent loop:

- `observe(user_input, agent_output)`: records an `Interaction` in the soul's episodic memory
- `recall(query, limit)`: retrieves relevant memories for a query

Observation failures are silently swallowed:

```python
async def observe(self, user_input, agent_output) -> None:
    try:
        await self._soul.observe(Interaction(...))
    except Exception:
        pass  # Observation failure should never break the agent loop
```

A failed `observe()` means the agent misses learning from an interaction — bad but recoverable. A crash here would break the agent loop — unrecoverable. The trade-off is correct.

## Version-Adaptive Recall

`recall()` tries `soul.context_for()` first (available in soul-protocol >= 0.2.8), which returns a richer formatted context block. If unavailable, it falls back to `soul.recall()` and extracts raw content strings:

```python
if hasattr(self._soul, "context_for"):
    context = await self._soul.context_for(query, max_memories=limit)
    if context:
        return [context]
memories = await self._soul.recall(query, limit=limit)
return [m.content for m in memories]
```

This lets the bridge work across soul-protocol versions without hard version pinning.

## Known Gaps

- **Bond level is displayed but not acted on**: `SoulBootstrapProvider` reads `soul.bond.bond_strength` and adds it to the `knowledge` list. There is no logic that changes behavior based on bond level.
- **`SoulBootstrapProvider` is not registered in PocketPaw's bootstrap registry**: It is constructed in `get_paw_agent()` and passed directly to CLI commands rather than being registered as the active bootstrap provider via PocketPaw's DI system.