---
{
  "title": "Agents Domain Pydantic Schemas (ee/cloud/agents/schemas.py)",
  "summary": "Pydantic request and response models for the agents domain API. Defines validation rules for agent creation, updates, and discovery, including soul customization fields (archetype, values, OCEAN personality scores).",
  "concepts": [
    "Pydantic schemas",
    "agent creation",
    "OCEAN personality",
    "soul customization",
    "validation",
    "request models"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "API",
    "agents",
    "data models"
  ],
  "source_docs": [
    "a2c2d0e0fecff453"
  ],
  "backlinks": null,
  "word_count": 308,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agents Domain Pydantic Schemas

## Purpose

This module defines the data contracts for the agents API. All request bodies and response shapes are Pydantic `BaseModel` subclasses, giving automatic validation, serialization, and OpenAPI documentation.

## Request Schemas

### CreateAgentRequest

Fields for creating a new agent:

- **Identity**: `name` (1-100 chars), `slug` (1-50 chars, URL-friendly), `avatar`
- **Visibility**: `private`, `workspace`, or `public` — enforced via regex pattern
- **Agent config**: `backend` (defaults to `claude_agent_sdk`), `model`, `persona`, `temperature`, `max_tokens`, `tools`, `trust_level`, `system_prompt`
- **Soul customization**: `soul_enabled` (default True), `soul_archetype`, `soul_values`, `soul_ocean` (OCEAN personality dict)

The soul fields are notable — every agent can have a persistent personality profile based on the OCEAN (Big Five) model. This ties into the Soul Protocol integration where agents aren't just tools but have identity continuity.

### UpdateAgentRequest

All fields are optional (`None` defaults) to support partial updates. The service layer applies only non-None fields, preserving existing values for unspecified fields.

### DiscoverRequest

Pagination and filtering for agent discovery:
- `query` — free-text search
- `visibility` — filter by visibility level
- `page` / `page_size` — bounded pagination (page >= 1, size 1-100)

## Response Schema

### AgentResponse

A typed response model with all agent fields plus timestamps. Note: the service layer currently builds response dicts manually (in `_agent_response()`) rather than using this schema. The schema exists for OpenAPI documentation but isn't enforced on responses.

## Known Gaps

- `AgentResponse` is defined but not used as a return type annotation on router endpoints — responses are plain `dict` returns. This means the OpenAPI docs may not reflect actual response shapes.
- The `slug` field has a max_length of 50 but no regex pattern to enforce URL-safe characters. Invalid slugs could cause routing issues.
- `soul_ocean` is typed as `dict[str, float]` but has no validation that keys are valid OCEAN dimensions (openness, conscientiousness, extraversion, agreeableness, neuroticism).