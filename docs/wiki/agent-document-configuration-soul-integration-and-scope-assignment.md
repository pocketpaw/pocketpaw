---
{
  "title": "Agent Document — Configuration, Soul Integration, and Scope Assignment",
  "summary": "The Beanie ODM document representing an agent's persistent configuration in MongoDB — covering backend selection, system prompt, tool allowlist, trust level, soul personality settings, and hierarchical scope assignments. This document captures agent identity and configuration, not execution state.",
  "concepts": [
    "Agent document",
    "AgentConfig",
    "soul integration",
    "OCEAN personality",
    "trust level",
    "scope assignment",
    "Beanie ODM",
    "backend selection",
    "visibility",
    "slug",
    "tools allowlist",
    "soul_values"
  ],
  "categories": [
    "data modeling",
    "agents",
    "soul-protocol",
    "MongoDB"
  ],
  "source_docs": [
    "aa8ba43d69695e60"
  ],
  "backlinks": null,
  "word_count": 543,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`agent.py` defines the `Agent` Beanie document and its nested `AgentConfig` Pydantic model. The separation of concerns is explicit in the docstring: this is configuration only — not execution state, not runtime context, not message history. The `Agent` document answers "what is this agent's identity and how should it behave?" while the `Session` and `Message` documents answer "what has this agent done?"

## AgentConfig Fields

```python
class AgentConfig(BaseModel):
    backend: str = "claude_agent_sdk"
    model: str = ""
    system_prompt: str = ""
    tools: list[str] = []
    trust_level: int = Field(default=3, ge=1, le=5)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1)
    scopes: list[str] = []
    soul_enabled: bool = True
    soul_persona: str = ""
    soul_archetype: str = ""
    soul_values: list[str] = ["helpfulness", "accuracy"]
    soul_ocean: dict[str, float] = {...}
```

**backend** — Currently `"claude_agent_sdk"` is the default. The field is a string rather than an enum to allow new backends without a schema migration.

**model** — An empty string means "use the backend's default model". This design allows the backend to be updated to a newer default without requiring agents to be explicitly reconfigured.

**trust_level** — Integer 1-5 controls what tools and capabilities the agent is permitted to use. Level 1 is the most restricted (read-only, no external calls); level 5 is the most permissive (full tool access, code execution). Enforced at the tool-execution layer.

**scopes** — Added in the cluster-D agent-scope-picker feature. A list of hierarchical scope tags (e.g., `"org:sales:*"`) that bound the agent's retrieval surface in the knowledge base. An empty list means no narrowing — the agent sees the full workspace KB. Validated by `scope_rules.normalise_and_validate` at the API boundary.

## Soul Integration

The `soul_*` fields embed Soul Protocol's personality model directly into the agent configuration:

- `soul_enabled` — toggle for soul-aware behavior
- `soul_persona` / `soul_archetype` — free-text identity description
- `soul_values` — list of behavioral values injected into the agent's context
- `soul_ocean` — OCEAN (Big Five) personality trait scores, defaulting to a helpful, conscientious, agreeable personality

These defaults represent a deliberate design choice: a new agent out of the box should behave in a helpful, professional, low-anxiety way. Operators can override any dimension per-agent.

## Agent Document

```python
class Agent(TimestampedDocument):
    workspace: Indexed(str)
    name: str
    slug: str
    avatar: str = ""
    config: AgentConfig = Field(default_factory=AgentConfig)
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    owner: str
```

The `workspace` field is indexed for fast workspace-scoped queries. The `visibility` field controls who can interact with the agent: `private` is owner-only, `workspace` is visible to all workspace members, `public` allows cross-workspace access (used for shared template agents).

`slug` is a URL-safe identifier for the agent used in API paths and deep links. It must be unique within a workspace (enforced at the router layer, not at the MongoDB index layer).

## Known Gaps

- `slug` uniqueness is not enforced by a MongoDB unique index — a race condition in the router could create two agents with the same slug in the same workspace.
- `scopes` are stored as plain strings without a compound index; scope-based queries on the `agents` collection would need a dedicated index for large workspaces.
- `soul_ocean` values have no range validation — a value outside [0, 1] would be stored without error and could cause unexpected behavior in the soul-aware rendering layer.