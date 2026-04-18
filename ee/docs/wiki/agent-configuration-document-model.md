---
{
  "title": "Agent Configuration Document Model",
  "summary": "Defines the `Agent` document (stored in MongoDB's `agents` collection) and its embedded `AgentConfig` model. Agents are configuration-only records — they define how an AI agent behaves but don't handle execution. Includes Soul Protocol integration fields for personality and memory.",
  "concepts": [
    "Agent",
    "AgentConfig",
    "Soul Protocol",
    "OCEAN model",
    "trust level",
    "Beanie document",
    "workspace scoping",
    "agent configuration",
    "personality model"
  ],
  "categories": [
    "models",
    "agents",
    "configuration",
    "Soul Protocol"
  ],
  "source_docs": [
    "161f9c485b66d651"
  ],
  "backlinks": null,
  "word_count": 442,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agent Configuration Document Model

`cloud/models/agent.py`

## Purpose

This module defines the persistent configuration for AI agents within a workspace. An `Agent` document stores everything needed to configure an agent's behavior — model selection, system prompt, tools, trust level, and Soul Protocol personality settings. It does not handle agent execution; that happens elsewhere in the runtime.

## AgentConfig (Embedded Model)

Configuration fields for how the agent operates:

### LLM Settings
- `backend: str = "claude_agent_sdk"` — Which AI backend to use
- `model: str = ""` — Specific model ID (empty = backend default)
- `system_prompt: str = ""` — Custom system instructions
- `temperature: float = 0.7` — Creativity vs determinism (0-2 range)
- `max_tokens: int = 4096` — Response length cap
- `tools: list[str]` — Enabled tool names
- `trust_level: int = 3` — 1-5 scale controlling what actions the agent can take autonomously

### Soul Protocol Integration
- `soul_enabled: bool = True` — Whether the agent has persistent identity
- `soul_persona: str = ""` — Custom persona text
- `soul_archetype: str = ""` — Archetype template (e.g., "mentor", "explorer")
- `soul_values: list[str]` — Core values (default: helpfulness, accuracy)
- `soul_ocean: dict[str, float]` — OCEAN personality model scores (Openness, Conscientiousness, Extraversion, Agreeableness, Neuroticism) with sensible defaults for a helpful assistant

The OCEAN defaults (high conscientiousness 0.85, high agreeableness 0.8, low neuroticism 0.2) produce a reliable, friendly, emotionally stable personality.

## Agent (Document Model)

Extends `TimestampedDocument` (auto createdAt/updatedAt):

- `workspace: Indexed(str)` — Scoped to a workspace
- `name: str` — Display name
- `slug: str` — URL-safe identifier
- `avatar: str` — Avatar URL
- `config: AgentConfig` — Embedded config
- `visibility: str` — `private`, `workspace`, or `public` (regex-validated)
- `owner: str` — User ID of the creator

### Database Settings
- Collection name: `agents`
- Compound index on `(workspace, slug)` — enables fast lookup by workspace + slug and enforces uniqueness within a workspace

## Design Decisions

- **Config-only, not runtime**: Separating configuration from execution means agents can be configured in the UI without starting a runtime process.
- **Soul enabled by default**: New agents get Soul Protocol integration out of the box, aligning with PocketPaw's identity-first philosophy.
- **Trust level scale**: 1-5 maps to increasing autonomy — level 1 agents need approval for everything, level 5 can act independently.

## Known Gaps

- No uniqueness constraint enforced at the database level for `(workspace, slug)` — the index is for query performance, not a unique constraint.
- `tools` is `list[str]` with no validation against available tools — invalid tool names would be stored silently.
- `visibility` uses a regex pattern but the application may not check visibility during agent operations.
