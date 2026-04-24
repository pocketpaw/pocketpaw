---
{
  "title": "Settings REST Router with Security Field Filtering",
  "summary": "Exposes GET and PUT REST endpoints for reading and updating PocketPaw's runtime configuration, with two hard-coded protection layers: secrets are never returned over the wire, and a set of security-critical fields cannot be modified via the API at all. An asyncio lock prevents concurrent write races.",
  "concepts": [
    "settings REST API",
    "SECRET_FIELDS",
    "immutable fields",
    "asyncio Lock",
    "security guardrails",
    "field filtering",
    "bypass_permissions",
    "trust_level",
    "settings:write scope",
    "read-modify-write"
  ],
  "categories": [
    "api",
    "configuration",
    "security",
    "rest-endpoints"
  ],
  "source_docs": [
    "355dfe56816f1e0b"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Settings REST Router with Security Field Filtering

The settings router provides a REST alternative to the WebSocket-only settings interface, enabling programmatic configuration management from scripts, dashboards, and CI pipelines without requiring a live WebSocket connection.

### Two Layers of Field Protection

The router enforces two distinct protection categories, for different threat models.

**Layer 1 — Secret fields (read protection).** The `SECRET_FIELDS` constant imported from `pocketpaw.credentials` lists every field that contains sensitive credentials: API keys, bot tokens, OAuth secrets, and similar values. The GET endpoint iterates over all model fields and skips any that appear in this set. The consequence is that a client that reads `/settings` will never see a raw API key — it sees only the structural configuration. Clients that need to know whether a key is configured should use the `*_configured` boolean flags on the health endpoint instead. This behavior was added in the April 2026 update and matches the masking already applied by `Settings.to_safe_dict()` and the WebSocket `settings_get` handler, ensuring all three surfaces behave consistently.

**Layer 2 — Immutable fields (write protection).** The `_IMMUTABLE_FIELDS` frozenset names fields that control security guardrails: `file_jail_path`, `bypass_permissions`, `trust_level`, `injection_scan_enabled`, `guardian_enabled`, `localhost_auth_bypass`, and `pii_scan_enabled`. These cannot be changed via the REST API regardless of what the caller sends. The design decision is deliberate: if an attacker gained access to an API token with `settings:write` scope, they should not be able to escalate privileges by flipping `bypass_permissions=True` or disabling injection scanning. The only way to change these fields is to edit the config file or environment variables directly on the server, which requires OS-level access.

### Concurrency Guard

The module-level `_settings_lock = asyncio.Lock()` prevents concurrent read-modify-write races. Without this guard, two simultaneous PUT requests could both read the current config, both apply their changes to their local copy, and then both write back — the second write would silently discard the first write's changes. The lock serializes all PUT operations. GET requests also acquire the lock to ensure they see a fully-written config state rather than a half-written one.

### Scope Requirements

GET requires either `settings:read` or `settings:write` scope. PUT requires `settings:write`. This asymmetry means a read-only integration (monitoring, dashboard display) can be granted a scoped token that cannot accidentally modify configuration.

### Path Serialization

The GET handler converts `pathlib.Path` values to strings before returning them. Path objects are not JSON-serializable natively, and silently converting them prevents 500 errors when the settings model includes file-system paths.

### Known Gaps

The PUT handler source is truncated in the extracted file. The full implementation validates incoming fields against both `_IMMUTABLE_FIELDS` and the `Settings` model schema, but the field-level validation strategy (reject unknown fields vs. ignore them) is not visible. A strict reject-unknown policy would be safer, preventing typos in field names from silently having no effect.