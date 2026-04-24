---
{
  "title": "Agent Domain Schema Validation Tests",
  "summary": "This module comprehensively tests the Pydantic schemas for the agents domain — `CreateAgentRequest`, `UpdateAgentRequest`, `DiscoverRequest`, `AgentResponse`, `ScopeAssignmentRequest`, and `ScopeAssignmentResponse` — covering required fields, defaults, length constraints, visibility validation, and scope normalization. Scope-related tests were added in the `feat/cluster-d-agent-scope-picker` feature.",
  "concepts": [
    "CreateAgentRequest",
    "UpdateAgentRequest",
    "DiscoverRequest",
    "AgentResponse",
    "ScopeAssignmentRequest",
    "ScopeAssignmentResponse",
    "Pydantic",
    "scope normalization",
    "visibility",
    "pagination",
    "agent schemas"
  ],
  "categories": [
    "agents",
    "schemas",
    "testing",
    "validation",
    "test"
  ],
  "source_docs": [
    "ead66ed98d5410d5"
  ],
  "backlinks": null,
  "word_count": 412,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_agent_schemas.py` module acts as a contract test for all Pydantic schemas in the `ee.cloud.agents.schemas` package. These schemas are the gatekeepers for data entering the agent management API — they validate, normalize, and document the shape of requests and responses.

## CreateAgentRequest Coverage

The create request tests verify:
- Required fields: `name` and `slug` must be present.
- Defaults: `avatar` defaults to `""`, `visibility` to `"private"`, `backend` to `"claude_agent_sdk"`, `model` to `""`.
- Length constraints: `name` maximum 100 characters, `slug` maximum 50 characters.
- Empty string rejection: both `name` and `slug` must be non-empty.
- Visibility enum: only `"private"`, `"workspace"`, and `"public"` are valid.

These constraints prevent garbage data from reaching the database layer and provide clear error messages to API consumers.

## UpdateAgentRequest Coverage

All fields on `UpdateAgentRequest` are optional — a PATCH-style schema. Tests confirm that instantiating with no arguments yields all-`None` fields, and that partial updates (e.g., only `name`) work without affecting other fields. The `config` field accepts arbitrary dicts, enabling per-agent backend configuration.

## DiscoverRequest Pagination

The discover endpoint accepts pagination parameters. Tests verify:
- `page` minimum is 1 (page=0 is rejected).
- `page_size` maximum is 100, minimum is 1.
- Defaults: `page=1`, `page_size=20`, `query=""`, `visibility=None`.

## Scope Field Coverage (feat/cluster-d-agent-scope-picker)

Scopes are colon-delimited hierarchical strings (e.g., `org:sales:*`) that define which organizational data an agent can access. The normalizer lowercases and strips whitespace, then deduplicates while preserving order:

```python
req = CreateAgentRequest(
    name="Sales Bot",
    slug="sales-bot",
    scopes=["  Org:Sales:*  ", "org:sales:*"],  # whitespace + dedupe
)
assert req.scopes == ["org:sales:*"]
```

Three validation rules are tested on scope strings:
1. The universal wildcard `"*"` is forbidden — it would grant all access.
2. Mid-segment wildcards like `"org:*:leads"` are rejected — only trailing wildcards are allowed.
3. An empty list is accepted on `UpdateAgentRequest` (to clear all scopes).

## ScopeAssignmentRequest / Response

These schemas wrap bulk scope assignment operations. `ScopeAssignmentRequest` requires the `scopes` field (unlike update which omits it for partial patches), accepts an empty list, and applies the same normalization rules. `ScopeAssignmentResponse` echoes back the `agent_id` and the normalized `scopes`.

## AgentResponse Model

The response shape test creates a full `AgentResponse` with all fields and asserts key values round-trip correctly. This prevents serialization bugs where internal field names differ from JSON keys.

## Known Gaps

No TODO or FIXME markers. The tests do not cover the interaction between `scopes` and `visibility` (e.g., whether a public agent can hold private scopes). Backend enum validation beyond `claude_agent_sdk` is not tested.