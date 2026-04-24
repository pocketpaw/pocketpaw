---
{
  "title": "OAuth Integration Router — Google and Spotify Service Authorization",
  "summary": "The OAuth integrations router handles the user-facing authorize and callback flow for connecting PocketPaw to third-party services like Gmail, Google Calendar, Google Drive, Google Docs, and Spotify. It was extracted from the dashboard router so the same flows work in both dashboard and headless serve modes.",
  "concepts": [
    "OAuth integrations",
    "Google OAuth",
    "Spotify OAuth",
    "authorization flow",
    "token exchange",
    "OAuthManager",
    "TokenStore",
    "service scopes",
    "state parameter",
    "redirect URI",
    "serve mode"
  ],
  "categories": [
    "integrations",
    "OAuth2",
    "API"
  ],
  "source_docs": [
    "248bc15301117e16"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When users want PocketPaw's agent to read their Gmail, check their calendar, or control Spotify, they must grant PocketPaw access through each provider's OAuth2 flow. This router handles that handshake: launching the consent screen and processing the callback that delivers tokens.

## Canonical Scope Registry

The `OAUTH_SCOPES` dict is declared at module level and serves as the single source of truth for what permissions PocketPaw requests from each service:

```python
OAUTH_SCOPES: dict[str, list[str]] = {
    "google_gmail": ["https://mail.google.com/"],
    "google_calendar": ["https://www.googleapis.com/auth/calendar"],
    "google_drive": ["https://www.googleapis.com/auth/drive"],
    "google_docs": [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
    "spotify": [...],
}
```

Keeping scopes here rather than inside each handler ensures that adding a new service only requires one dict entry — the authorize and callback handlers read from the same registry automatically.

## `GET /oauth/integrations/authorize`

This endpoint starts the OAuth flow for the requested service. It validates that the service is known (returning 400 for unknown services), then checks that the relevant OAuth credentials are configured in settings. If `google_oauth_client_id` is empty, the user gets a clear error message directing them to Settings — failing fast here prevents the user from hitting a provider error page with no useful context.

The `state` parameter encodes both the provider (`google` or `spotify`) and the specific service key (`google_gmail`, `google_drive`, etc.) as `"provider:service"`. This compound state allows the single callback endpoint to handle all services without separate routes per integration.

The `redirect_uri` is constructed from `localhost:{web_port}` — always pointing to the local server. This is intentional: OAuth tokens for personal integrations stay on the user's machine and are never proxied through an external server.

## `GET /oauth/integrations/callback`

The callback endpoint handles both success and error cases:

- If the provider returns an `error` parameter (user denied, misconfigured app), it returns a plain HTML error page with `window.close()` omitted so the user sees the error before the window disappears.
- If `code` is missing (malformed redirect), it returns an explicit HTML message.
- On success, it exchanges the code for tokens via `OAuthManager.exchange_code()` and persists them to `TokenStore`. The HTML response includes `setTimeout(() => window.close(), 1500)` so the popup closes automatically after the user reads the confirmation.

Errors during exchange are caught broadly and rendered as HTML rather than raising exceptions — since this endpoint is the target of a browser redirect, there is no JSON client to parse a structured error response.

## Why Extracted from dashboard.py

The original dashboard.py contained these routes, which meant they were unavailable in `serve` mode (headless operation without the full dashboard). Extracting them into a standalone router registered on both apps ensures integration flows work regardless of which mode PocketPaw is running in.

## Known Gaps

- Token refresh is not handled here. If a stored token expires, the integration tool must detect the 401 and trigger a re-authorization flow externally.
- There is no listing endpoint showing which integrations are currently connected and when their tokens expire.
- Microsoft OAuth (Teams, OneDrive, Outlook) is not yet in `OAUTH_SCOPES` despite Teams being listed as a channel adapter.
