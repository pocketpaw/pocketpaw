---
{
  "title": "Generic Settings Read and Update Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's settings API, using a deliberately generic dict-based approach to handle the wide variety of configurable settings without requiring a separate schema for each. The design prioritises flexibility over strict typing, with secrets excluded from the read response.",
  "concepts": [
    "SettingsResponse",
    "SettingsUpdateRequest",
    "settings API",
    "partial update",
    "secret filtering",
    "dynamic configuration",
    "dict-based schema",
    "Pydantic",
    "runtime settings",
    "configuration management"
  ],
  "categories": [
    "api-schemas",
    "configuration",
    "settings"
  ],
  "source_docs": [
    "7ffdcca76c08fee4"
  ],
  "backlinks": null,
  "word_count": 520,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw has a large and evolving set of runtime settings — LLM provider, model names, API keys, memory backend, UI preferences, feature flags, and more. Rather than maintaining a rigid typed schema for every setting, the settings API uses a flexible dict-based approach that can accommodate new settings without schema changes.

## Models

### `SettingsResponse`

```python
class SettingsResponse(BaseModel):
    """Current settings (non-secret fields)."""

    # Dynamic dict — settings vary, so we use a generic dict
    pass  # Actual endpoint returns dict directly
```

This is a placeholder model with no fields. The actual endpoint returns a `dict` directly rather than a Pydantic-serialised response. The comment explains the deliberate choice: settings are too varied and dynamic to enumerate as typed fields. The `SettingsResponse` class exists as a documentation anchor — it signals that a settings response is expected here — rather than as an active validation layer.

The `non-secret fields` note in the docstring is critical. API keys, tokens, and passwords are filtered out server-side before constructing the response dict. A client that reads settings will never see credential values, only their presence (e.g. `{"openai_api_key": "***"}` or the key omitted entirely). This prevents secrets from being logged, cached by browsers, or exposed via dashboard dev tools.

### `SettingsUpdateRequest`

```python
class SettingsUpdateRequest(BaseModel):
    """Settings update — only provided fields are changed."""

    settings: dict
```

A single `settings: dict` field holds all the key-value pairs the caller wants to update. This is a partial-update model: only the keys present in the dict are modified; absent keys are left unchanged. This prevents a client that only knows about a subset of settings from accidentally wiping settings it didn't include in its request.

Using a plain `dict` means any key can be submitted — there's no allowlist at the schema layer. Validation of individual setting names and value types happens in the application layer (the settings manager), not here.

## Design Tradeoffs

**Flexibility vs. type safety.** The dict approach means:
- New settings can be added to the backend without touching the schema.
- Clients can update settings they discovered dynamically (by first reading the full settings dict).
- But: no Pydantic validation on individual setting values. A typo in a model name (`"gpt-4o"` vs. `"gpt4o"`) passes schema validation and only fails at runtime.

**Comparison with other schemas.** Most other schemas in this module use explicit typed fields. Settings is the exception because its key space is controlled by a separate configuration registry, not the schema layer.

## Defensive Patterns

- Secret filtering on the read path — the server-side handler is responsible for stripping secrets before returning the settings dict, preventing credential exposure via the API.
- Partial-update semantics on the write path — only supplied keys are modified.

## Known Gaps

- `SettingsResponse` provides no typed contract for the actual response shape. API consumers must inspect the live endpoint to understand which keys are returned, making client code fragile.
- `SettingsUpdateRequest.settings: dict` has no value-type constraint (`dict[str, Any]` would be more explicit).
- No changelog or audit trail — there's no `updated_keys: list[str]` in the response to confirm which settings were actually changed.