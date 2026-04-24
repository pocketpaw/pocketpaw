---
{
  "title": "MCP OAuth Token Storage — Secure File-Based Persistence",
  "summary": "`MCPTokenStorage` implements the MCP SDK's `TokenStorage` protocol, persisting OAuth access tokens and dynamic client registration data to per-server JSON files under `~/.pocketpaw/mcp_oauth/`. Files are written with `chmod 0600` to prevent token leakage to other OS users.",
  "concepts": [
    "OAuth",
    "TokenStorage",
    "file permissions",
    "chmod 0600",
    "dynamic client registration",
    "MCP SDK protocol",
    "token persistence",
    "secure storage",
    "mcp_oauth"
  ],
  "categories": [
    "MCP Integration",
    "Security"
  ],
  "source_docs": [
    "e915cd06aec658f8"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

OAuth-authenticated MCP servers require PocketPaw to hold a valid access token between sessions. Without persistence, users would need to re-authorise on every restart. `MCPTokenStorage` solves this by storing tokens in the user's config directory, scoped per server name.

## Storage Layout

```
~/.pocketpaw/
  mcp_oauth/
    github.json        # tokens + client registration for "github" server
    notion.json
```

Each file contains two top-level keys: `tokens` (OAuth access/refresh tokens) and `client_info` (dynamic client registration metadata, used by servers implementing RFC 7591).

## Security: chmod 0600

```python
def _save(self, data: dict) -> None:
    self._path.write_text(json.dumps(data))
    os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
```

Setting the file mode to `0600` immediately after writing prevents other users on a shared system from reading OAuth tokens. This is the same pattern used by SSH private key files. Without this, the tokens would be readable by any process running as any user in the same group.

## Async Interface, Sync I/O

The `TokenStorage` protocol requires async methods, but file I/O is synchronous. The methods are declared `async` for protocol compliance but perform blocking reads/writes directly. This is acceptable because token operations are rare (once per session start) and brief.

## Graceful Degradation

`_load()` catches both `json.JSONDecodeError` and `OSError`, logs a warning, and returns an empty dict. This prevents a corrupt token file from blocking MCP server startup — the server simply re-authenticates as if no token exists.

## Directory Creation

`_get_oauth_dir()` calls `mkdir(exist_ok=True)` to ensure the directory exists before any file operation. Without this guard, the first call on a fresh install would fail with `FileNotFoundError`.

## Why Per-Server Files?

Storing tokens in separate per-server JSON files (rather than a single combined file) prevents one corrupt or invalidated token from blocking access to all other servers. If the GitHub token expires, only `github.json` is affected; Notion and Linear tokens remain valid and readable. It also makes manual token inspection and deletion straightforward: a user can delete `github.json` to force re-authentication with GitHub without touching any other server's credentials.

## Protocol Compliance

The MCP SDK defines a `TokenStorage` protocol (interface) that token storage implementations must satisfy. By implementing this protocol, `MCPTokenStorage` integrates into the MCP SDK's OAuth flow automatically without the SDK needing to know anything about PocketPaw's file system layout. The SDK calls `get_tokens()` and `set_tokens()` at the right points in the OAuth lifecycle.

## Known Gaps

Tokens are stored as plain JSON (not encrypted at rest). An attacker with read access to the home directory can extract OAuth tokens directly. No token refresh logic: `MCPTokenStorage` persists tokens but the OAuth refresh flow is owned by the MCP SDK. No multi-process locking: concurrent PocketPaw instances could corrupt a token file with simultaneous writes.