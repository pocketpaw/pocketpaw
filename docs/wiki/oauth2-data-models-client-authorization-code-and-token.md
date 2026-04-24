---
{
  "title": "OAuth2 Data Models: Client, Authorization Code, and Token",
  "summary": "Defines the three core data structures for PocketPaw's OAuth2 implementation: `OAuthClient` (registered application with redirect URI validation), `AuthorizationCode` (short-lived PKCE exchange token), and `OAuthToken` (access + refresh token pair). The redirect URI matching logic implements RFC 8252 Section 7.3 for native app loopback flexibility.",
  "concepts": [
    "OAuthClient",
    "AuthorizationCode",
    "OAuthToken",
    "redirect URI validation",
    "RFC 8252",
    "loopback port flexibility",
    "PKCE code challenge",
    "code replay prevention",
    "token expiry",
    "dataclasses"
  ],
  "categories": [
    "api",
    "OAuth2",
    "data models",
    "security"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 433,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`models.py` contains pure data structures with no I/O or business logic — a clean separation that lets the server layer (`server.py`) and storage layer (`storage.py`) import models without circular dependencies.

## OAuthClient

Represents a registered application that can initiate OAuth2 flows. The `client_id` and `client_name` fields identify the application; `redirect_uris` and `allowed_scopes` constrain what it can request.

The `matches_redirect_uri` method implements RFC 8252 Section 7.3:

> For native apps using loopback redirects, any port is acceptable.

The rationale: native desktop apps bind to a random ephemeral port on `localhost` to receive the OAuth callback. The port number is not known at client registration time and changes on every auth flow. RFC 8252 explicitly acknowledges this and permits port-flexible matching for loopback URIs:

```python
if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
    path = parsed.path or "/"
    for registered in self.redirect_uris:
        rp = urlparse(registered)
        if rp.scheme == "http" and rp.hostname in ("localhost", "127.0.0.1"):
            if path == (rp.path or "/"):
                return True
```

The path is compared after normalization (empty path → `/`) to prevent trivial bypasses. The scheme is checked strictly — `https://localhost` does not match `http://localhost` because HTTPS on localhost requires a self-signed cert that most desktop apps avoid.

Custom URL schemes (`tauri://oauth-callback`) are matched by exact string comparison, with no port-flexibility logic needed (custom schemes do not have ports).

## AuthorizationCode

Short-lived (10 minute TTL, enforced in storage) and single-use. Fields:

- `code` — Random hex string
- `code_challenge` / `code_challenge_method` — PKCE verifier storage for the exchange step
- `used` — Set to `True` after a successful token exchange to prevent code replay attacks

The `used` flag is the defense against authorization code replay: if an attacker intercepts the code in transit (e.g., via a malicious redirect URI on the same machine), they cannot exchange it a second time.

## OAuthToken

Access + refresh token pair. The `expires_at` field on the access token enables expiry checking without clock skew issues (absolute UTC timestamp vs. relative `expires_in` seconds). The refresh token does not have an `expires_at` field in the dataclass — its TTL is enforced during validation in `AuthorizationServer`, not in the model, keeping the model as a pure data container.

## Known Gaps

- `OAuthToken` does not have a `revoked` flag — revocation is handled by deleting the token from storage rather than marking it. This means revocation checks require a storage lookup rather than a field check on the in-memory object.
- The `code_challenge_method` field accepts any string but the server enforces `"S256"` only. The field could be a `Literal["S256"]` type annotation to make this constraint explicit.
