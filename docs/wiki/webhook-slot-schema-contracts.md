---
{
  "title": "Webhook Slot Schema Contracts",
  "summary": "Defines the Pydantic models that represent a webhook slot's configuration and the request payloads for creating or identifying webhooks. The name pattern constraint prevents injection-prone characters from reaching URL-based routing.",
  "concepts": [
    "webhook slot",
    "Pydantic BaseModel",
    "name pattern validation",
    "HMAC secret",
    "sync timeout",
    "inbound webhook",
    "path traversal prevention",
    "WebhookSlot",
    "WebhookAddRequest",
    "REST schemas"
  ],
  "categories": [
    "api-schemas",
    "webhooks",
    "security",
    "event-integration"
  ],
  "source_docs": [
    "fa2471f8ccbb18b7"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Webhook Slot Schema Contracts

The `webhooks.py` schemas module defines the data shapes exchanged when managing PocketPaw's inbound webhook slots. Each slot is a named, secret-protected endpoint that external systems can POST events to, triggering agent workflows.

### The Three Models

**`WebhookSlot`** is the full internal representation of a configured slot. It carries:

- `name` — the slug embedded in the inbound URL (`/webhook/inbound/{name}`).
- `description` — a human label shown in the dashboard.
- `secret` — an HMAC secret used to verify that the inbound request genuinely comes from the registered sender. Defaults to empty string for compatibility with legacy slots created before secret enforcement was added.
- `sync_timeout` — how many seconds the runtime waits for the triggered workflow to complete before responding. Defaults to 30 seconds, matching the typical external service timeout threshold.
- `url` — the computed public URL for the slot, populated by the router at list time by combining the request host header with the slot name.

**`WebhookAddRequest`** is the payload for creating a new slot. The `name` field has two constraints: `min_length=1` (no blank names) and `pattern=r"^[a-zA-Z0-9_-]+$"` (alphanumeric plus hyphens and underscores only). The pattern guard is essential because `name` becomes a URL path segment. Allowing spaces, slashes, or special characters would enable path traversal, break routing, or create ambiguous URLs. The `sync_timeout` is optional here — the router falls back to the global `webhook_sync_timeout` setting when it is absent, keeping the creation request minimal.

**`WebhookNameRequest`** is a minimal payload used for operations that only need to identify a slot — specifically for secret regeneration and deletion. A dedicated model rather than a plain query parameter enforces a consistent JSON body contract and allows future fields (e.g., a `reason` for auditing) to be added without breaking callers.

### Why Pattern Validation Lives in the Schema

Webhook names are embedded directly into route paths. If a name like `foo/../../admin` were accepted, it could bypass authorization checks that depend on exact path matching in middleware. Enforcing the constraint at the Pydantic layer means it runs before any router logic executes — the HTTP framework rejects the request with a 422 before it reaches the settings mutation code.

### Integration Pattern

```python
# Create a new webhook slot
req = WebhookAddRequest(name="shopify-orders", description="Order notifications", sync_timeout=15)

# Rotate secret
name_req = WebhookNameRequest(name="shopify-orders")
```

### Known Gaps

The `secret` field on `WebhookSlot` defaults to empty string rather than `None`. A slot with an empty secret string accepts any inbound POST without HMAC verification — the router does not enforce that a secret must be present before a slot can receive traffic. Adding a `secret_required: bool` enforcement mode, or making `secret` a non-optional field in `WebhookAddRequest`, would close this gap.