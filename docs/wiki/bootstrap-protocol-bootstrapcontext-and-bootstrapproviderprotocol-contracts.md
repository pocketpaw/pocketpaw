---
{
  "title": "Bootstrap Protocol — BootstrapContext and BootstrapProviderProtocol Contracts",
  "summary": "This module defines the two core contracts of the bootstrap subsystem: BootstrapContext, a dataclass holding all fields that compose an agent's system prompt, and BootstrapProviderProtocol, a structural Protocol that any identity source must implement. The system prompt layout — instructions first, identity block last — is a deliberate positioning strategy to keep the model anchored on persona during long conversations.",
  "concepts": [
    "BootstrapContext",
    "BootstrapProviderProtocol",
    "system prompt layout",
    "identity block",
    "to_system_prompt",
    "Protocol",
    "persona drift",
    "XML identity tags",
    "instructions ordering",
    "soul",
    "user_profile"
  ],
  "categories": [
    "bootstrap",
    "agent-identity",
    "protocol-contracts"
  ],
  "source_docs": [
    "0000000000000004"
  ],
  "backlinks": null,
  "word_count": 434,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## BootstrapContext

`BootstrapContext` is a plain Python dataclass that acts as the lingua franca of the bootstrap subsystem. Any provider (file-based, cloud, test) produces one; `AgentContextBuilder` consumes one. The fields are:

| Field | Purpose |
|---|---|
| `name` | The agent's display name |
| `identity` | The primary personality / "who are you" block |
| `soul` | Deeper philosophical core and values |
| `style` | Communication style guidelines |
| `instructions` | Behavioural rules and tool usage guides |
| `knowledge` | List of background facts to inject |
| `user_profile` | Content of `USER.md` — what the agent knows about the human |

The split between `identity`, `soul`, and `style` is intentional: they represent different layers of the agent's personality that may be updated independently. For example, `style` might change per-channel (terse for SMS, rich markdown for Discord) while `identity` stays constant.

## System Prompt Layout in `to_system_prompt()`

The `to_system_prompt()` method assembles all fields into a single string with a specific ordering that is central to how well the model maintains persona:

1. **Instructions first** — Tool docs and behavioural rules go at the top. They are long and serve as reference material; the model attends to them less intensely in very long exchanges.
2. **Knowledge next** — Background facts that provide factual grounding.
3. **Identity block last** — Wrapped in `<identity>` XML tags and placed as close to the live conversation as possible.

The rationale for putting identity last is empirical: transformer attention decays over distance, so placing the identity block immediately before the conversation turns means the model "re-reads" it at the start of each response. This reduces persona drift in long threads — a known failure mode where agents gradually sound more generic as the conversation grows.

The `<identity>` XML tags signal structural intent to the model. Some frontier models are trained to treat XML-tagged sections as high-priority directives, reinforcing the positioning effect.

## BootstrapProviderProtocol

`BootstrapProviderProtocol` is a `typing.Protocol` (structural subtyping) with a single async method:

```python
async def get_context(self) -> BootstrapContext: ...
```

Using `Protocol` rather than an abstract base class means any object with a compatible `get_context` method is a valid provider, without needing to inherit from anything. This makes testing trivial: a lambda-style provider or a simple stub class works without modifying the class hierarchy.

## Known Gaps

The `knowledge` field is a flat list of strings with no priority or expiry metadata. There is no mechanism to mark a knowledge item as time-sensitive or to drop lower-priority items when the context budget is tight. `AgentContextBuilder` currently includes all knowledge items or none.