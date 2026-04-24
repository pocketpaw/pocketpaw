---
{
  "title": "Cloud Agent Bootstrap Provider",
  "summary": "CloudAgentBootstrapProvider builds a BootstrapContext from a MongoDB agent config dict, enabling cloud-hosted agents to carry their own custom identity, persona, archetype, and values rather than inheriting the generic OSS defaults. It satisfies the same BootstrapProviderProtocol as the default file-based provider, so the rest of the runtime is unaware of which source is in use.",
  "concepts": [
    "CloudAgentBootstrapProvider",
    "BootstrapProviderProtocol",
    "BootstrapContext",
    "soul_persona",
    "system_prompt",
    "soul_archetype",
    "soul_values",
    "identity assembly",
    "MongoDB agent config",
    "cloud agents"
  ],
  "categories": [
    "bootstrap",
    "cloud-agents",
    "agent-identity"
  ],
  "source_docs": [
    "183081ed393519cb"
  ],
  "backlinks": null,
  "word_count": 544,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why This Provider Exists

PocketPaw's OSS deployment reads agent identity from local markdown files (`IDENTITY.md`, `USER.md`). Cloud deployments differ: each agent is a MongoDB document with its own `soul_persona`, `system_prompt`, `soul_archetype`, and `soul_values` fields. Without a dedicated provider, every cloud agent would present the same generic identity — defeating the purpose of per-agent customisation.

`CloudAgentBootstrapProvider` bridges that gap by implementing `BootstrapProviderProtocol.get_context()` against the config dict rather than the filesystem.

## Identity Assembly Logic

The identity block is the "who are you" section that sits at the bottom of the system prompt (closest to the live conversation, so the model anchors on it). The assembly logic has a deliberate priority:

1. If `soul_persona` is set, it becomes the base identity.
2. If `system_prompt` is set *and* differs from `soul_persona`, it is appended as an additional directive. This supports the common pattern where an operator wants a persona *plus* a short operational instruction without repeating the persona verbatim.
3. If neither is set, the fallback is `"You are {agent_name}."` — a minimal but non-empty identity that prevents the model from improvising a random personality.

The `or ""` + `.strip()` pattern on every config read guards against `None` values from MongoDB documents where optional fields were never set.

## Soul Block

Separate from identity, the soul block encodes the agent's archetype (e.g., "The Mentor") and core values list. These feed into PocketPaw's OCEAN-inspired personality model. Values are joined into a single line rather than a bullet list to keep the token count low for agents that have many declared values.

The `isinstance(values, list)` check prevents a crash if the MongoDB document stores values as a string or `None` — a real risk when documents are created by different API versions.

## Blank Fields

`style` and `instructions` are returned as empty strings. For cloud agents, style guidance and tool instructions are either injected later by `AgentContextBuilder` or omitted entirely. Returning empty strings rather than `None` keeps downstream consumers from needing null checks.

## Runtime Behaviour and Testability

Because `CloudAgentBootstrapProvider` implements the same `BootstrapProviderProtocol` as `DefaultBootstrapProvider`, it can be dropped into any test or integration scenario that expects a provider. Testing a cloud-agent configuration requires nothing more than constructing the class with a config dict — no MongoDB connection, no mock patches for file I/O. This makes the provider easy to test exhaustively: the full set of field combinations (persona only, system_prompt only, both, neither) can be covered with simple unit tests.

The `agent_config or {}` default in `__init__` is a defensive guard for callers that pass `None` when the MongoDB document has no config field. Without it, every `cfg.get(...)` call would raise an `AttributeError`. This pattern propagates through the entire method: every field access uses `or ""` or `or []` to coerce absent fields to safe empty values, so no single missing field can cause a crash.

## Known Gaps

No validation that `soul_archetype` belongs to a known set of archetypes. Operators can set arbitrary strings, and the system will accept them silently. There is also no schema version check on the config dict — if a new required field is added to the provider in a future release, old MongoDB documents without that field will silently produce an incomplete `BootstrapContext` rather than raising a validation error.