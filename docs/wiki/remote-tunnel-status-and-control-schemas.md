---
{
  "title": "Remote Tunnel Status and Control Schemas",
  "summary": "Defines the minimal Pydantic models for PocketPaw's remote tunnel API, which exposes the agent's local HTTP server to the internet via a secure tunnel. The two schemas cover tunnel status querying and tunnel start results, including error reporting for failed tunnel launches.",
  "concepts": [
    "TunnelStatusResponse",
    "TunnelStartResponse",
    "remote tunnel",
    "ngrok",
    "public URL",
    "localhost exposure",
    "Pydantic",
    "remote access",
    "reverse proxy",
    "tunnel lifecycle"
  ],
  "categories": [
    "api-schemas",
    "remote-access",
    "networking",
    "configuration"
  ],
  "source_docs": [
    "1c6df007ad31f0a7"
  ],
  "backlinks": null,
  "word_count": 514,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw runs as a local server by default, accessible only on `localhost`. The remote tunnel feature creates a publicly accessible URL — typically via a service like ngrok, Cloudflare Tunnel, or a similar reverse proxy — so the agent can be reached from mobile apps, external integrations, or remote dashboards without VPN or port forwarding.

This file defines the two lightweight schemas that govern the tunnel API.

## Models

### `TunnelStatusResponse`

```python
class TunnelStatusResponse(BaseModel):
    active: bool = False
    url: str | None = None
```

Reports whether a tunnel is currently running and what public URL it exposes. `active` defaults to `False` — the safe assumption when the system state is unknown. `url` is `None` when no tunnel is active, avoiding an empty-string URL that a client might try to use.

The minimal shape is intentional: tunnel status polling should be cheap. The client can call this endpoint frequently (e.g. every few seconds while waiting for a tunnel to start) without large response overhead.

### `TunnelStartResponse`

```python
class TunnelStartResponse(BaseModel):
    active: bool
    url: str | None = None
    error: str | None = None
```

Returns the result of a tunnel start request. Unlike `TunnelStatusResponse`, `active` has no default here — it must be explicitly set by the handler, reflecting the actual outcome of the start attempt. The `error` field carries a human-readable failure reason when the tunnel fails to start (e.g. tunnel binary not installed, port already in use, authentication failure with the tunnel service).

The distinction between `TunnelStatusResponse` and `TunnelStartResponse` matters:
- **Status** is a read operation — always safe to call, always returns current state.
- **Start** is a write operation — its response includes an `error` field because starting can fail in ways that polling status cannot.

Having `error` on the start response (rather than relying solely on HTTP error status codes) means clients can display a specific failure message to the user without parsing error body formats.

## Integration Context

The typical usage flow:

1. User clicks "Enable Remote Access" in the dashboard.
2. Dashboard POSTs to the tunnel start endpoint → receives `TunnelStartResponse`.
3. If `active: True`, the `url` is displayed as a shareable link.
4. If `error` is set, the dashboard shows the error message.
5. Dashboard periodically polls the status endpoint to detect if the tunnel drops.

## Defensive Patterns

- `active: bool = False` on `TunnelStatusResponse` — safe default prevents false positives.
- `url: str | None` — explicit null when inactive prevents client code from constructing invalid URLs from empty strings.
- Separate `error` field on start response — gives clients actionable failure information.

## Known Gaps

- No tunnel provider field — the schema doesn't expose which tunnelling service is in use (ngrok vs. Cloudflare vs. custom), making it impossible for clients to display provider-specific instructions.
- No authentication or token field on `TunnelStartResponse` — if the tunnel requires a token or password to access, there's no schema field to carry it.
- No `TunnelStopResponse` model is present in this file — stop operations likely return a generic success/error shape defined elsewhere.