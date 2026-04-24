---
{
  "title": "Agent-Facing Pocket Context Fetcher",
  "summary": "`fetch_pocket_for_agent` retrieves a pocket document and strips sensitive fields before returning it as a compact JSON-safe dict for injection into AI agent tool responses. The function follows a defensive result-envelope pattern, always returning `{\"ok\": bool, ...}` rather than raising exceptions, so agent tool handlers can branch on success without try/except.",
  "concepts": [
    "agent context",
    "pocket fetcher",
    "field exclusion",
    "result envelope",
    "JSON serialization",
    "deferred import",
    "MCP binding",
    "security field stripping",
    "Beanie",
    "agent tool response"
  ],
  "categories": [
    "pockets",
    "agent-integration",
    "security",
    "enterprise-cloud"
  ],
  "source_docs": [
    "2b7de1c80fc9801c"
  ],
  "backlinks": null,
  "word_count": 588,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When a PocketPaw AI agent needs to reason about a pocket's contents — its widgets, layout, type, and description — it calls `fetch_pocket_for_agent`. This function bridges the MongoDB document model and the agent tool response format, performing three jobs: loading the document, stripping agent-invisible fields, and normalizing the result to a JSON-safe dict.

## Why a Dedicated Agent-Facing Fetch?

The standard Beanie `.get()` call returns a full `Pocket` document including fields that agents should never see. Passing the raw document to an agent would expose:

- **`share_link_token`** — a bearer credential that grants unauthenticated access; leaking it into agent context would allow the agent to construct or share access links it was not authorized to create.
- **`shared_with`** — a list of user IDs that the agent has no legitimate reason to enumerate.
- **`team` and `agents`** — bulk relationship arrays that are large, change frequently, and are irrelevant to the agent's task of understanding pocket contents.

The `_AGENT_INVISIBLE_FIELDS` tuple makes the exclusion list explicit and auditable — a security reviewer can see at a glance what the agent cannot access.

## Result Envelope Pattern

```python
{"ok": True, "pocket": {...}}   # success
{"ok": False, "error": "..."}   # failure
```

Rather than raising exceptions, the function returns a dict envelope. This is the correct pattern for agent tool responses: the MCP binding layer that calls this function expects a serializable result, not an exception to catch. Returning `{"ok": False, "error": "..."}` lets the agent's tool handler log the error or return it to the user without a try/except wrapper at the call site.

## Input Validation

```python
if not pocket_id or not isinstance(pocket_id, str):
    return {"ok": False, "error": "pocket_id is required (string)"}
```

The type and truthy check guards against `None`, empty string, and non-string types that would cause `PydanticObjectId(pocket_id)` to raise a `ValueError` or `TypeError`. Catching these early returns a clean error message rather than an unhandled exception propagating to the agent.

## Deferred Import Pattern

```python
try:
    from beanie import PydanticObjectId
    from ee.cloud.models.pocket import Pocket
    pocket = await Pocket.get(PydanticObjectId(pocket_id))
except Exception as exc:
    ...
```

The Beanie and model imports are deferred inside the try block. This allows the module to be imported in contexts where Beanie is not yet initialized (e.g., during application startup before `init_beanie()` is called) without raising import-time errors. It also means a missing database connection surfaces as a caught exception with a clean error message rather than an ImportError.

## `_json_safe` Normalizer

```python
def _json_safe(doc: Any) -> Any:
    return json.loads(json.dumps(doc, default=str))
```

After `model_dump(mode="json")`, the document may still contain Python objects that are not JSON-native (e.g., `datetime` instances that Pydantic serializes as Python objects in some modes, or residual `ObjectId` references). The `json.dumps(..., default=str)` pass converts any non-serializable object to its string representation, and the immediate `json.loads` converts the result back to a plain Python dict. This round-trip is a belt-and-suspenders serialization guard.

## Architectural Separation

The module docstring explicitly notes that the MCP binding (`sdk_mcp_pocket.py`) is a separate file. This separation keeps pocket-domain logic in the pockets package and the MCP protocol binding in the agents package, following the single-responsibility principle and making it easier to swap MCP bindings without touching pocket logic.

## Known Gaps

- `except Exception as exc` catches all exceptions including `CancelledError` and `KeyboardInterrupt` on Python 3.7; the `# noqa: BLE001` comment acknowledges this as a known broad catch.
- The `_AGENT_INVISIBLE_FIELDS` exclusion runs after `model_dump` — if Pydantic serializes aliased fields differently, some field names might not match and the exclusion would silently fail.