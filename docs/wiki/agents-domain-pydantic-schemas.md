---
{
  "title": "Agents Domain Pydantic Schemas",
  "summary": "The agents schemas module defines the Pydantic request and response models for creating, updating, and querying agents, including server-side scope normalisation validators that enforce the same grammar rules as the frontend ScopePicker regardless of how the API is called.",
  "concepts": [
    "Pydantic",
    "request schema",
    "response schema",
    "scope validation",
    "field_validator",
    "ScopeAssignmentRequest",
    "soul configuration",
    "OCEAN personality",
    "full replacement semantics"
  ],
  "categories": [
    "API",
    "agents",
    "validation"
  ],
  "source_docs": [
    "76e117146ca6c82f"
  ],
  "backlinks": null,
  "word_count": 403,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Schema Inventory

| Schema | Role |
|---|---|
| `CreateAgentRequest` | Body for `POST /agents` |
| `UpdateAgentRequest` | Body for `PATCH /agents/{id}` |
| `ScopeAssignmentRequest` | Body for `PATCH /agents/{id}/scope` |
| `ScopeAssignmentResponse` | Response for `GET` and `PATCH /agents/{id}/scope` |
| `DiscoverRequest` | Body for `POST /agents/discover` |
| `AgentResponse` | Read model returned by all agent endpoints |

## Agent Configuration Fields

`CreateAgentRequest` bundles both identity fields (`name`, `slug`, `avatar`, `visibility`) and runtime configuration (`backend`, `model`, `system_prompt`, `temperature`, `max_tokens`, `tools`, `trust_level`). This flat structure means a single POST call fully specifies an agent — callers don't need a two-step create-then-configure flow.

The `soul_*` fields (`soul_enabled`, `soul_archetype`, `soul_values`, `soul_ocean`) let callers customise the agent's persistent identity at creation time. `soul_ocean` is a `dict[str, float]` mapping OCEAN personality trait names to scores, enabling fine-grained personality control.

## Scope Validation on Every Schema

Both `CreateAgentRequest` and `UpdateAgentRequest` carry an optional `scopes: list[str]` field. Both apply the same `_clean_scopes` validator:

```python
@field_validator("scopes")
@classmethod
def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    return normalise_and_validate_scopes(v)
```

This runs before the data reaches the service layer, so malformed scopes are rejected with a Pydantic 422 response rather than stored silently. The validator is duplicated across all three scope-bearing schemas rather than shared via a mixin because Pydantic v2's `field_validator` doesn't compose cleanly across unrelated models.

## ScopeAssignmentRequest: Full Replacement Semantics

The dedicated scope endpoint uses full-list replacement rather than delta operations. The design rationale is in the docstring: "the UI and API share a single 'these are the scopes now' semantic." This avoids the complexity of delta merging (add/remove operations with ordering concerns) at the cost of requiring callers to always send the complete desired scope list.

## ScopeAssignmentResponse: Lightweight Polling

The response schema is deliberately narrow — just `agent_id` and `scopes`. The comment explains why: "the UI can cheaply poll scope without pulling the full agent document each time." For a ScopePicker that refreshes on open, fetching 2 fields is meaningfully cheaper than fetching the full agent document with its config blob.

## Known Gaps

- `AgentResponse` duplicates some fields from the MongoDB document. If `Agent` gains new fields, `AgentResponse` must be updated manually — there is no auto-derivation from the document model.
- `UpdateAgentRequest` has no validation that at least one field is non-None; a PATCH with an empty body is a no-op that succeeds silently.