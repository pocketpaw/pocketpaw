---
{
  "title": "OAuth Token Store: Secure File-Based Credential Persistence",
  "summary": "The `TokenStore` persists OAuth 2.0 tokens to `~/.pocketpaw/oauth/{service}.json` with strict `0600` file permissions to prevent other OS users from reading credentials. It provides load, save, delete, and list operations over the `OAuthTokens` dataclass that covers the full OAuth 2.0 token surface.",
  "concepts": [
    "TokenStore",
    "OAuthTokens",
    "OAuth 2.0",
    "file permissions",
    "chmod 0600",
    "credential persistence",
    "JSON storage",
    "Phase 2 Integration Ecosystem",
    "refresh token",
    "access token"
  ],
  "categories": [
    "integrations",
    "authentication",
    "security"
  ],
  "source_docs": [
    "361e8ed7c5ddd336"
  ],
  "backlinks": null,
  "word_count": 377,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/integrations/token_store.py` is the credential persistence layer for all of PocketPaw's OAuth integrations. Created in February 2026 as part of Phase 2 Integration Ecosystem, it ensures OAuth tokens survive process restarts without requiring users to re-authenticate on every session.

## Why File-Based Storage?

PocketPaw runs as a local desktop application without a centralized backend. A database would be overkill; the system keychain would add platform-specific dependencies. Plain JSON files in a known location (`~/.pocketpaw/oauth/`) strike the right balance: portable, inspectable, and backed up with the user's home directory.

## Security: chmod 0600

After every write, the store applies `os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)` — owner read/write only. This prevents other OS users on a shared machine from reading token files. Without this, newly created files default to `0644` (world-readable), exposing access tokens and refresh tokens to any local user.

```python
def save(self, tokens: OAuthTokens) -> None:
    path = _get_oauth_dir() / f"{tokens.service}.json"
    data = asdict(tokens)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
```

## OAuthTokens Dataclass

The `OAuthTokens` dataclass captures the full OAuth 2.0 token surface:

```python
@dataclass
class OAuthTokens:
    service: str
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float | None = None  # Unix timestamp
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
```

The `extra` dict allows services with non-standard fields (e.g., Spotify's `user_id`) to store additional data without schema changes.

## Directory Creation

`_get_oauth_dir()` calls `d.mkdir(exist_ok=True)` on every access. This idempotent pattern ensures the directory exists even on first run, without requiring explicit setup steps.

## Operations

- `save(tokens)` — writes and chmods the file
- `load(service)` — returns `OAuthTokens | None`; returns `None` rather than raising if the file is missing, so callers can detect "not authenticated" cleanly
- `delete(service)` — removes the token file; returns `bool` indicating whether anything was deleted
- `list_services()` — returns names of all services with stored tokens (useful for settings UI)

## Known Gaps

- **No encryption at rest**: Tokens are stored as plain JSON. If the user's home directory is exposed (unencrypted disk, backup), tokens could be extracted. OS keychain integration would mitigate this.
- **No token validation on load**: `load()` returns whatever is on disk without verifying expiry or token format. Callers like `OAuthManager` are responsible for checking `expires_at`.