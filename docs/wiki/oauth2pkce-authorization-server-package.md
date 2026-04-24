---
{
  "title": "OAuth2/PKCE Authorization Server Package",
  "summary": "The `oauth2` package implements a self-contained OAuth2 authorization server with PKCE support, designed primarily for the PocketPaw Tauri desktop application to authenticate against a locally running PocketPaw instance without exposing a client secret. The package is organized into models, server logic, and storage layers.",
  "concepts": [
    "OAuth2",
    "PKCE",
    "RFC 7636",
    "authorization code flow",
    "Tauri desktop",
    "access token",
    "refresh token",
    "client secret problem",
    "scope-limited tokens",
    "local authentication"
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
  "word_count": 347,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `oauth2` package was introduced in February 2026 to give the PocketPaw Tauri desktop application a secure, standards-based authentication flow. Before OAuth2/PKCE support, the desktop app used a shared master token that had to be manually copied from the server — a poor user experience and a security risk (the token had no expiry and no scope restrictions).

## Why OAuth2 for a Local App?

At first glance, OAuth2 seems heavyweight for a desktop app authenticating against localhost. The reasons for using it:

**PKCE eliminates the client secret problem.** Traditional OAuth2 requires a `client_secret` to exchange an authorization code for tokens. Desktop apps cannot keep secrets — the binary is on the user's machine and can be inspected. PKCE (RFC 7636) replaces the secret with a one-time cryptographic challenge/verifier pair, maintaining security without requiring an embedded secret.

**Scoped, revocable tokens.** OAuth2 tokens carry scopes (e.g., `chat`, `settings:read`) and can be revoked without invalidating other credentials. The master token can stop long sessions by revoking the desktop app's token rather than rotating the global master token.

**Refresh token lifecycle.** Access tokens expire in 1 hour; refresh tokens last 30 days. This means the desktop app stays authenticated across days without user interaction, while the short access token window limits the damage from token theft.

**Standards compliance.** Using RFC 6749 + RFC 7636 means the desktop app's auth flow can be understood, audited, and potentially extended using standard OAuth2 tooling.

## Package Structure

- **`models.py`** — Data classes for `OAuthClient`, `AuthorizationCode`, and `OAuthToken`
- **`server.py`** — `AuthorizationServer` implementing the authorize/token-exchange/refresh/revoke flow
- **`storage.py`** — `OAuthStorage` handling in-memory auth codes and file-persisted tokens

## Known Gaps

- The authorization server only supports the authorization code + PKCE flow. The implicit flow and client credentials flow are not implemented (by design — they are less secure for this use case).
- There is no user consent UI within PocketPaw itself — the authorization always succeeds for any registered client. This is acceptable for the single-user local deployment model but would need a consent screen for multi-user or cloud deployments.
