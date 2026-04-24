---
{
  "title": "OAuth2 Authorization Server: PKCE Flow Implementation",
  "summary": "`AuthorizationServer` implements the full OAuth2 authorization code + PKCE flow: issuing authorization codes, exchanging them for tokens via S256 challenge verification, refreshing tokens, and revoking tokens. Token lifetimes are 1-hour access / 30-day refresh / 10-minute authorization code.",
  "concepts": [
    "AuthorizationServer",
    "PKCE",
    "S256 challenge verification",
    "token exchange",
    "refresh token",
    "access token TTL",
    "hmac.compare_digest",
    "authorization code",
    "singleton pattern",
    "RFC 6749"
  ],
  "categories": [
    "api",
    "OAuth2",
    "authentication",
    "security"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 386,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`server.py` contains the business logic for PocketPaw's OAuth2 server. It coordinates between the data models and the storage layer to implement the RFC 6749 authorization code flow with RFC 7636 PKCE extension.

## Authorization Code Issuance

`authorize(client_id, redirect_uri, scope, code_challenge, code_challenge_method)` validates the request before issuing a code:

1. Client must exist in storage
2. Redirect URI must match the client's registered URIs (via `OAuthClient.matches_redirect_uri`)
3. Challenge method must be `"S256"` — plain challenge is rejected to ensure the PKCE exchange is cryptographically sound
4. Scope must be a subset of the client's `allowed_scopes`

On success, a random 32-byte hex code is generated and stored as an `AuthorizationCode` with the challenge embedded. The code is returned to the caller for inclusion in the redirect URL.

## Token Exchange (PKCE Verification)

`exchange_code(code, code_verifier, redirect_uri)` is where PKCE security is enforced:

```python
expected = base64.urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()
).rstrip(b"=").decode()
if not hmac.compare_digest(expected, auth_code.code_challenge):
    return None, "invalid_grant"
```

The verifier (known only to the client at flow start) is hashed with SHA-256 and compared against the stored challenge using `hmac.compare_digest` to prevent timing attacks. If the hashes match, the client proves it initiated the flow.

Token lifetimes are defined as module constants:

```python
ACCESS_TOKEN_TTL  = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
CODE_TTL          = timedelta(minutes=10)
```

## Token Refresh

`refresh_token(refresh_token_str)` validates the refresh token, checks its TTL, and issues a new access token with the same scope. The refresh token itself is not rotated on use — this is a deliberate choice to avoid clients being locked out if a network error prevents them from receiving the new refresh token.

## Singleton Pattern

`get_oauth_server()` returns a module-level `AuthorizationServer` singleton sharing a single `OAuthStorage` instance. `reset_oauth_server()` clears it for tests, consistent with the pattern used in `api_keys.py`.

## Known Gaps

- Refresh token rotation is not implemented. Security-conscious deployments prefer rotating refresh tokens on each use (RFC 6819 recommendation) to detect token theft. A stolen refresh token used by an attacker would immediately invalidate the legitimate client's token, triggering a re-auth that alerts the user.
- There is no introspection endpoint (`/oauth/introspect`, RFC 7662) for external services to validate tokens.
- `CODE_TTL` is defined in the server but the expiry check is performed in storage. If the constant is changed in one place and not the other, the TTL behavior would be inconsistent.
