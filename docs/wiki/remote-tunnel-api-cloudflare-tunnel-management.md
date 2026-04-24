---
{
  "title": "Remote Tunnel API — Cloudflare Tunnel Management",
  "summary": "The remote router exposes three endpoints for controlling a Cloudflare tunnel that makes the locally running PocketPaw instance accessible over the public internet. This lets users interact with their companion from mobile devices or share access without configuring port forwarding.",
  "concepts": [
    "Cloudflare tunnel",
    "TunnelManager",
    "remote access",
    "cloudflared",
    "public URL",
    "NAT traversal",
    "TunnelStatusResponse",
    "TunnelStartResponse",
    "lazy import",
    "admin scope"
  ],
  "categories": [
    "networking",
    "API",
    "remote access"
  ],
  "source_docs": [
    "67f914b7271b49de"
  ],
  "backlinks": null,
  "word_count": 410,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw runs locally by default. The remote tunnel feature, powered by Cloudflare's `cloudflared`, punches a hole through NAT and firewalls to expose the local server at a public HTTPS URL. The `remote.py` router is the thin API wrapper around the `TunnelManager` singleton.

## `GET /remote/status`

Returns whether a tunnel is currently active and, if so, the public URL assigned by Cloudflare. The `TunnelStatusResponse` includes `active: bool` and `url: str | None`. Callers should poll this endpoint after issuing a start command to wait for the tunnel URL to become available.

## `POST /remote/start`

Calls `TunnelManager.start()` and returns the assigned public URL. The endpoint is a fire-and-start operation: `cloudflared` is launched as a subprocess and the URL is extracted from its stdout. Two failure modes are handled:

- **RuntimeError** — raised by the manager if `cloudflared` is not installed, already running, or fails to start. Returns 500.
- **Success** — returns `TunnelStartResponse(url=url)` with the live Cloudflare URL.

The 500 on runtime error is appropriate here because missing a required binary or configuration is a server-side problem, not a client mistake.

## `POST /remote/stop`

Terminates the Cloudflare subprocess via `TunnelManager.stop()`. Like the start endpoint, RuntimeError maps to 500. The stop operation should be idempotent at the manager level — calling stop when the tunnel is already stopped should not raise.

## Security Considerations

The tunnel feature bypasses network-level access controls. Once the tunnel is active, the public URL is accessible to anyone on the internet. PocketPaw's authentication layer (master token, session tokens, API keys, scope checks) is the only protection. This is why the remote endpoints should be placed behind admin scope in a future update — currently the router does not declare scope guards.

## Lazy Import Pattern

```python
from pocketpaw.tunnel import get_tunnel_manager
```

`TunnelManager` is imported lazily inside each handler so importing the `remote` module does not trigger process-level side effects (such as checking for `cloudflared` on `$PATH`) at startup.

## Known Gaps

- Neither the start nor stop endpoints require any scope guard. A caller with any valid session token can expose the server to the public internet. Admin scope (`require_scope("admin")`) should be added to both mutating endpoints.
- The public tunnel URL is not persisted across restarts. If the server restarts while the tunnel is active, the URL changes and any bookmarked links stop working.
- There is no webhook or callback mechanism to notify the client when the URL changes (e.g., after a tunnel reconnection).
