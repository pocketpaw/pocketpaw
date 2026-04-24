---
{
  "title": "OAuth2 Storage: In-Memory Auth Codes and File-Persisted Tokens",
  "summary": "`OAuthStorage` uses a two-tier storage strategy: authorization codes live in memory only (ephemeral, 10-minute TTL), while access and refresh tokens are persisted to `~/.pocketpaw/oauth_tokens.json` so authenticated desktop sessions survive server restarts. A pre-registered `pocketpaw-desktop` client with Tauri-compatible redirect URIs is always available.",
  "concepts": [
    "OAuthStorage",
    "in-memory auth codes",
    "file-persisted tokens",
    "DEFAULT_DESKTOP_CLIENT",
    "cleanup_expired",
    "mark_code_used",
    "Tauri redirect URI",
    "token persistence",
    "two-tier storage",
    "RFC 8252"
  ],
  "categories": [
    "api",
    "OAuth2",
    "storage",
    "persistence"
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

`storage.py` manages the lifecycle of all OAuth2 state in PocketPaw. The split between in-memory codes and disk-persisted tokens reflects their different security requirements and lifetimes.

## Two-Tier Storage Rationale

**Authorization codes in memory only**: Auth codes have a 10-minute TTL and are exchanged exactly once. Persisting them to disk would require synchronized writes and reads with no practical benefit — a server restart during the 10-minute window simply requires the user to restart the OAuth flow, which is a trivial inconvenience. Keeping codes in memory also means they vanish completely on restart, preventing replay of old codes that somehow survived past their TTL.

**Tokens on disk**: Access tokens (1-hour TTL) and refresh tokens (30-day TTL) must survive server restarts. If PocketPaw crashes and restarts while the desktop app is running, the app must be able to use its refresh token to get a new access token without forcing the user to log in again. File persistence enables this.

## Default Desktop Client

`DEFAULT_DESKTOP_CLIENT` is a pre-registered `OAuthClient` that is always present in storage, requiring no administrative action:

```python
DEFAULT_DESKTOP_CLIENT = OAuthClient(
    client_id="pocketpaw-desktop",
    redirect_uris=[
        "tauri://oauth-callback",
        "http://localhost:1420/oauth-callback",
        "http://localhost/",
    ],
    allowed_scopes=["chat", "sessions", "settings:read", "settings:write",
                    "channels", "memory", "admin"],
)
```

`tauri://oauth-callback` is the custom URL scheme that Tauri apps can register with the OS to receive OAuth redirects without running a local HTTP server. `http://localhost/` with the RFC 8252 port-flexible rule covers development setups. Having `admin` in the allowed scopes grants the desktop app full access, appropriate for a first-party client running on the user's own machine.

## Cleanup and Expiry

`cleanup_expired()` scans stored tokens and removes those whose `expires_at` is in the past. This method must be called periodically (e.g., on server startup or via a scheduler) to prevent token files from growing unboundedly. The in-memory code store uses a simpler TTL check on access rather than active cleanup — `store_code` checks `created_at` against `CODE_TTL` when codes are retrieved.

## Mark-Code-Used Pattern

`mark_code_used(code)` sets `AuthorizationCode.used = True` rather than deleting the code immediately. This allows `exchange_code` in the server to distinguish between "code never existed" (storage miss) and "code already exchanged" (returns used code), enabling a clearer error message (`"code_used"` vs `"invalid_grant"`).

## Known Gaps

- Token persistence uses a single JSON file without file locking. Concurrent server instances (unlikely but possible) could corrupt the file.
- `cleanup_expired()` is not called automatically — it must be triggered by the application lifecycle. If never called, expired tokens accumulate on disk indefinitely.
- There is no mechanism to enumerate all tokens for a given `client_id`, making "revoke all sessions for this client" an O(n) full-file scan.
