---
{
  "title": "OAuth2 Token Exchange and Revocation Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's OAuth2 token lifecycle — code exchange, token refresh, standard token responses, and revocation. Field-level validation enforces RFC 6749 grant type restrictions at the schema boundary, catching invalid requests before they reach the OAuth2 implementation.",
  "concepts": [
    "TokenRequest",
    "TokenResponse",
    "RevokeRequest",
    "OAuth2",
    "authorization_code",
    "refresh_token",
    "PKCE",
    "code_verifier",
    "RFC 6749",
    "Pydantic validation"
  ],
  "categories": [
    "api-schemas",
    "authentication",
    "oauth2",
    "security"
  ],
  "source_docs": [
    "4b5b08b5691d2388"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw implements OAuth2 to allow third-party clients and channel adapters to authenticate users and obtain access tokens. This file defines the wire-format schemas for the token endpoint — covering the two standard grant types (authorization code exchange and refresh token) — and the revocation endpoint.

## Models

### `TokenRequest`

```python
class TokenRequest(BaseModel):
    grant_type: str = Field(..., pattern="^(authorization_code|refresh_token)$")
    code: str | None = None
    code_verifier: str | None = None
    client_id: str | None = None
    redirect_uri: str | None = None
    refresh_token: str | None = None
```

The `pattern` constraint on `grant_type` is the key defensive measure here. Without it, an attacker could submit an unsupported grant type (e.g. `"client_credentials"` or `"password"`) and potentially trigger unexpected code paths in the token handler. Enforcing the allowlist at the Pydantic layer means the router receives a pre-validated request — the handler never sees an invalid grant type.

**PKCE support** is implicit: `code_verifier` is present alongside `code` for the authorization code flow. PKCE (Proof Key for Code Exchange, RFC 7636) prevents authorization code interception attacks in public clients (mobile apps, SPAs) that cannot safely store a client secret.

All other fields are `Optional`. This is correct for OAuth2: `code` and `code_verifier` are only meaningful for `authorization_code`; `refresh_token` is only meaningful for `refresh_token` grant. Cross-field validation (ensuring `code` is present when `grant_type == "authorization_code"`) must happen in the route handler, not the schema.

### `TokenResponse`

```python
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str
```

Follows RFC 6749 Section 5.1 exactly. `token_type` defaults to `"Bearer"` — the only type PocketPaw issues. `expires_in` is an integer (seconds), matching the RFC and most OAuth2 client libraries. `scope` is a space-delimited string of granted scopes, also per RFC 6749.

### `RevokeRequest`

```python
class RevokeRequest(BaseModel):
    token: str
```

Minimal by design, matching RFC 7009 (OAuth2 Token Revocation). The spec intentionally does not distinguish token types in the revocation request — the server identifies whether it's an access or refresh token internally. Keeping the schema minimal avoids over-specifying the revocation surface.

## Security Considerations

- The `grant_type` regex allowlist is the primary input-validation guard. It rejects everything outside the two expected values before any business logic runs.
- `code_verifier` presence enables PKCE, which is the recommended mitigation for auth code interception in browser-based and mobile clients.
- `TokenResponse` never includes a `client_secret` or any credential material beyond the issued tokens themselves.

## Known Gaps

- No cross-field validation: `TokenRequest` will pass schema validation with `grant_type="authorization_code"` and no `code` supplied. The handler must guard this.
- `scope` on `TokenResponse` is a plain string with no parsing or validation — consumers must split on spaces themselves.
- No `id_token` field, meaning OpenID Connect flows (if ever needed) would require a schema extension.