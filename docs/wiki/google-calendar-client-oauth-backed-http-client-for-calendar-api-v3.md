---
{
  "title": "Google Calendar Client — OAuth-Backed HTTP Client for Calendar API v3",
  "summary": "`CalendarClient` provides async methods for listing upcoming events and creating new calendar events via the Google Calendar API v3, using OAuth bearer tokens managed by `OAuthManager` with automatic token refresh. It is part of PocketPaw's Phase 2 Integration Ecosystem.",
  "concepts": [
    "Google Calendar",
    "CalendarClient",
    "OAuth",
    "OAuthManager",
    "list_events",
    "create_event",
    "httpx",
    "bearer token",
    "token refresh",
    "async HTTP client",
    "Calendar API v3"
  ],
  "categories": [
    "integrations",
    "Google Workspace"
  ],
  "source_docs": [
    "77ce978a9771d913"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gcalendar.py` implements `CalendarClient`, PocketPaw's interface to the Google Calendar API. Like all Google integration clients in PocketPaw, it uses the shared `OAuthManager` and `TokenStore` for authentication rather than building its own OAuth logic. This keeps credential management centralized and consistent.

## Authentication Pattern

```python
async def _get_token(self) -> str:
    settings = get_settings()
    token = await self._oauth.get_valid_token(
        service="google_calendar",
        client_id=settings.google_oauth_client_id or "",
        client_secret=settings.google_oauth_client_secret or "",
    )
    if not token:
        raise RuntimeError(
            "Google Calendar not authenticated. Complete OAuth flow first "
            "(Settings > Google OAuth > Authorize Calendar)."
        )
    return token
```

`get_valid_token()` handles the full token lifecycle: returning a cached valid access token, or transparently refreshing an expired token using the stored refresh token. The `service="google_calendar"` key scopes the stored tokens to Calendar, allowing multiple Google services to maintain independent token records.

If no token is available (user has not completed OAuth), a `RuntimeError` is raised with a user-actionable message pointing to the Settings UI. This exception propagates to the agent tool that called the client, which can then relay the message to the user.

## list_events

```python
async def list_events(
    self,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    max_results: int = 10,
    calendar_id: str = "primary",
) -> list[dict[str, Any]]:
```

Default time range is `now` to `now + 7 days`. The `singleEvents=true` parameter expands recurring events into individual instances, and `orderBy=startTime` ensures chronological ordering — both essential for producing a meaningful "what's on my calendar this week" view.

Each event is normalized to a flat dict with `id`, `summary`, `start`, `end`, `location`, and `attendees`. The normalization strips the nested Google API response structure, which varies between all-day events (using `date`) and timed events (using `dateTime`).

## create_event

The `create_event` method constructs a Calendar API event body with `start`, `end`, `summary`, optional `description`, `location`, and `attendees`. Attendees are passed as email addresses and converted to the required `[{"email": "..."}]` format. The 15-second timeout (compared to 5 seconds in health checks) reflects that calendar API calls can be slower under load.

## Separation of Concerns

`CalendarClient` contains no agent tool logic — it is a pure HTTP client. The agent tools that expose calendar operations to the LLM are defined elsewhere and call this client. This separation means the client can be tested in isolation against a mocked OAuth token and HTTP responses without any LLM involvement.

## Known Gaps

- `list_events` fetches events from the primary calendar only by default. While `calendar_id` is configurable, the agent tools currently do not expose multi-calendar selection.
- There is no pagination support — only the first page of results up to `max_results` is returned. For users with dense calendars, events beyond the first page are invisible.
- The `create_event` method uses `raise_for_status()` which will raise an `httpx.HTTPStatusError` on API errors. The caller (agent tool) must handle this; there is no built-in retry or user-friendly error transformation at the client level.
