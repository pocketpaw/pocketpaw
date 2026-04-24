---
{
  "title": "API Key Schemas — Create, Info, and One-Time Creation Response",
  "summary": "The API key schemas define the request and response shapes for managing PocketPaw API keys. The design enforces a security invariant: the full plaintext key is returned only once at creation time, after which only a short prefix is exposed for identification.",
  "concepts": [
    "API keys",
    "scope-based access",
    "one-time secret",
    "key prefix",
    "CreateKeyRequest",
    "APIKeyInfo",
    "APIKeyCreatedResponse",
    "secrets management",
    "key revocation",
    "expiry"
  ],
  "categories": [
    "authentication",
    "schemas",
    "security"
  ],
  "source_docs": [
    "5b89228efb3cdbc3"
  ],
  "backlinks": null,
  "word_count": 421,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports API keys as an alternative authentication mechanism to session cookies. These schemas define the data contracts for creating keys and for listing their metadata without exposing secrets.

## `CreateKeyRequest`

```python
class CreateKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: list[str] = Field(default=["chat", "sessions"])
    expires_at: str | None = None
```

The `name` field has explicit length bounds (1–100 characters) to prevent empty key names (which would make keys unidentifiable in a list) and excessively long names (which could be used for storage abuse). The default scopes — `chat` and `sessions` — represent the minimal useful grant for a typical integration, nudging callers away from over-granting permissions at creation time.

`expires_at` is an optional ISO-8601 string. Using a string rather than a `datetime` field avoids timezone serialisation ambiguity across different JSON clients and keeps the API surface simple.

## `APIKeyInfo`

```python
class APIKeyInfo(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: str
    last_used_at: str | None = None
    expires_at: str | None = None
    revoked: bool = False
```

This model is the safe representation of a key — it contains the `prefix` (first few characters of the key, used for display) but not the full secret. The `last_used_at` field enables detecting stale keys that can be safely revoked, and `revoked: bool` allows soft-deletion where a key is marked invalid without being removed from the list.

## `APIKeyCreatedResponse`

```python
class APIKeyCreatedResponse(BaseModel):
    key: str  # Full plaintext — only shown at creation
    ...
```

This is the only model in the codebase that contains a full API key. The inline comment `# Full plaintext — only shown at creation` documents the security intent explicitly: after this response is delivered, the server stores only a hash of the key. Subsequent list calls return `APIKeyInfo` (with only the prefix), never `APIKeyCreatedResponse`.

This pattern is standard practice for secrets management: show once, store never. It means users who lose their key must revoke and regenerate — there is no recovery path. The tradeoff is deliberate: a recoverable key implies the server can produce it again, which means it must be stored reversibly (encrypted), introducing key management complexity and a decryption attack surface.

## Known Gaps

- `expires_at` is stored and returned as a raw string; there is no validation that it parses as a valid ISO-8601 date. An invalid expiry string would be persisted and could cause comparison failures at authentication time.
- There is no `PATCH /api-keys/{id}` schema for updating scopes or extending expiry without revoking and re-creating the key.
