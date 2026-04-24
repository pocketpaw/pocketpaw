---
{
  "title": "API Keys Router — Long-Lived Key CRUD with One-Shot Plaintext Return",
  "summary": "The API keys router provides create, list, revoke, and rotate operations for long-lived programmatic access keys. A critical security property of the create endpoint is that the plaintext key is returned exactly once — subsequent reads only expose a short prefix — mirroring industry-standard patterns like GitHub personal access tokens.",
  "concepts": [
    "API keys",
    "plaintext return",
    "one-shot secret",
    "rotate",
    "revoke",
    "admin scope",
    "APIKeyManager",
    "bcrypt hash",
    "scope guard",
    "access control",
    "machine-to-machine auth"
  ],
  "categories": [
    "API",
    "Security",
    "Authentication"
  ],
  "source_docs": [
    "8c4325d4f5dc5d4f"
  ],
  "backlinks": null,
  "word_count": 355,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`api_keys.py` manages PocketPaw's long-lived API keys — credentials intended for machine-to-machine access, automation scripts, or external integrations. All four CRUD operations are gated behind the `admin` scope, ensuring that only administrators can issue or revoke keys.

## Create: One-Shot Plaintext Return

The most security-critical design decision in this router is the `create_api_key` endpoint:

```python
record, plaintext = manager.create(
    name=body.name,
    scopes=body.scopes,
    expires_at=body.expires_at,
)
return APIKeyCreatedResponse(key=plaintext, ...)
```

The full plaintext key is returned **only in this single response**. The `APIKeyManager` stores a bcrypt hash (or similar one-way hash) of the key, so it can never be recovered later. The `list_api_keys` endpoint returns only the `prefix` field (e.g., `ppk_abc123...`) to help users identify which key is which without exposing the secret.

This pattern prevents a class of vulnerabilities where an attacker with read access to the database or a later API response could harvest valid credentials. Once the creation response is consumed, the secret is gone.

## Rotate: Atomic Revoke-and-Replace

`rotate_api_key` combines a revoke and a create in a single operation, preserving the original key's scopes:

This atomicity matters because a naive two-step approach (revoke then create separately) would leave a window during which no valid key exists, breaking any automation relying on that key. The rotate endpoint closes that gap by generating the replacement before revoking the original — or, at minimum, treating both as a single logical transaction.

## Admin Scope Guard

The router applies `require_scope("admin")` at the `APIRouter` level via the `dependencies` parameter:

```python
router = APIRouter(tags=["API Keys"], dependencies=[Depends(require_scope("admin"))])
```

This means every endpoint in the file inherits the scope check automatically — new endpoints added in the future won't accidentally be left unguarded.

## Error Handling

`create_api_key` catches `ValueError` from the manager and re-raises as HTTP 400. This surfaces user-facing validation errors (e.g., invalid scope names, duplicate key names) without leaking internal stack traces.

## Known Gaps

No TODOs or FIXMEs are flagged in the source. One implicit gap: there is no audit log entry recorded when a key is created, revoked, or rotated. For compliance-sensitive deployments, key lifecycle events should be recorded in the audit log alongside the actor's identity.