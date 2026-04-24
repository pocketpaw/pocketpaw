---
{
  "title": "Webhook Slot Management Router",
  "summary": "Manages the lifecycle of PocketPaw's inbound webhook slots — creating named endpoints, listing them with dynamically constructed URLs, removing them, and rotating their HMAC secrets. All operations require the `admin` scope, reflecting that webhook configuration controls which external systems can trigger agent workflows.",
  "concepts": [
    "webhook slots",
    "HMAC secret",
    "Cloudflare Tunnel",
    "dynamic URL",
    "secret redaction",
    "name validation",
    "409 Conflict",
    "admin scope",
    "secrets.token_urlsafe",
    "inbound webhook"
  ],
  "categories": [
    "api",
    "webhooks",
    "security",
    "event-integration"
  ],
  "source_docs": [
    "c704f7b4d3bae0ac"
  ],
  "backlinks": null,
  "word_count": 469,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Webhook Slot Management Router

The webhooks router provides CRUD operations for PocketPaw's inbound webhook slots. Each slot is a named, secret-protected endpoint that external systems (Shopify, GitHub Actions, Zapier, custom integrations) can POST events to, triggering agent workflows. This router manages the configuration of those slots; the actual event processing happens in the inbound webhook handler.

### Dynamic URL Construction

The list endpoint constructs each slot's public URL at request time using the incoming `Host` header:

```python
host = request.headers.get("host", f"localhost:{settings.web_port}")
protocol = "https" if "trycloudflare" in host else "http"
```

This approach handles PocketPaw's Cloudflare Tunnel integration transparently. When accessed via a `*.trycloudflare.com` tunnel, the URLs are returned with `https://` and the tunnel hostname — the correct public address to give to the external service. When accessed locally, URLs use `http://localhost:{port}`. Hardcoding a base URL in settings would require users to update it every time they start a new tunnel session.

### Secret Redaction on List

Secrets are never returned in full from the list endpoint. The response replaces all but the last four characters with `***`. This allows an admin to confirm which slot a known secret belongs to (by matching the last four characters) without exposing the full secret to anyone who can read network traffic or dashboard logs. The full secret is visible only at creation time and after a rotation.

### Name Validation

Webhook names are validated against `^[a-zA-Z0-9_-]+$` in both the schema layer and the router layer. The duplicate check defends against a race condition: the schema validates the format of a single request, but between validation and the config write, another request might create a slot with the same name. The router's explicit duplicate check raises 409 Conflict before the write, preventing two slots from sharing a name.

### Secret Generation

Secrets are generated with `secrets.token_urlsafe(32)`, producing 32 bytes of cryptographically random data encoded as a URL-safe Base64 string (43 characters). This strength is appropriate for HMAC-SHA256 signature verification — it provides ~256 bits of entropy, making brute-force attacks computationally infeasible.

### Scope Enforcement

The entire router is mounted with `dependencies=[Depends(require_scope("admin"))]`. Webhook management is an admin-only operation because misconfigured webhooks can expose the agent to arbitrary external triggers. Requiring the `admin` scope ensures that standard API tokens (which may have narrower scopes) cannot create or modify webhook slots.

### Known Gaps

Webhook slot configuration is stored in the settings file alongside all other settings. This means every settings write locks the entire settings state, and concurrent webhook operations compete with settings changes for the `_settings_lock`. A dedicated webhook config store would isolate webhook writes from the broader settings lock. There is also no per-slot enable/disable flag — removing a slot is the only way to stop it from receiving traffic, requiring a delete and re-add cycle to temporarily deactivate a slot.