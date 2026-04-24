---
{
  "title": "Conversation Session Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's conversation session API — listing, creating, renaming, and searching sessions. The search model captures both the matched content and the message role, enabling contextually rich search result previews in the dashboard.",
  "concepts": [
    "SessionInfo",
    "SessionListResponse",
    "SessionTitleRequest",
    "SessionSearchResult",
    "SessionCreateResponse",
    "conversation sessions",
    "session search",
    "match_role",
    "channel",
    "Pydantic"
  ],
  "categories": [
    "api-schemas",
    "sessions",
    "search",
    "conversation-management"
  ],
  "source_docs": [
    "69b386b7aca9d026"
  ],
  "backlinks": null,
  "word_count": 534,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A session in PocketPaw is a persistent conversation thread between a user and an agent. Sessions have titles, are associated with a specific channel (e.g. `"web"`, `"slack"`, `"discord"`), accumulate messages over time, and can be searched by their message content. This file defines the schema layer for all session management operations.

## Models

### `SessionInfo`

The primary session metadata model:

```python
class SessionInfo(BaseModel):
    id: str
    title: str = "Untitled"
    channel: str = "unknown"
    last_activity: str = ""
    message_count: int = 0
```

`title` defaults to `"Untitled"` — sessions can be created without a title (auto-generated later) and the default keeps the UI consistent. `channel` defaults to `"unknown"` for backward compatibility with sessions created before channel tracking was added. `last_activity` is a string timestamp rather than `datetime`, keeping serialisation simple. `message_count` gives the list view enough data to show a session summary without fetching the full message history.

### `SessionListResponse`

```python
class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]
    total: int
```

`total` is provided alongside the list. This is a pagination hint — `total` represents the full count of sessions, even if the current response is a page. The dashboard uses `total` to show "Showing 20 of 147 sessions" without a separate count query.

### `SessionTitleRequest`

```python
class SessionTitleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
```

A targeted schema for the rename operation. `min_length=1` prevents setting an empty title; `max_length=200` keeps titles readable in the sidebar without truncation. Using a dedicated model rather than a generic `{"title": str}` dict allows field validation and makes the API intent explicit.

### `SessionSearchResult`

```python
class SessionSearchResult(BaseModel):
    id: str
    title: str = "Untitled"
    channel: str = "unknown"
    match: str = ""
    match_role: str = ""
    last_activity: str = ""
```

Extends the basic session metadata with search-specific fields. `match` contains the snippet of message text that matched the query — a content preview. `match_role` identifies whether the match came from a `"user"` message or an `"assistant"` message. This distinction matters for UX: finding your own words versus finding the agent's response is a different user experience, and the dashboard can style them differently.

### `SessionCreateResponse`

```python
class SessionCreateResponse(BaseModel):
    id: str
    title: str = "New Chat"
```

Minimal: just the new session's `id` and initial title. The client immediately navigates to the new session, so full metadata isn't needed in the creation response — it will be loaded on the session detail fetch.

### `SessionSearchResponse`

```python
class SessionSearchResponse(BaseModel):
    sessions: list[SessionSearchResult]
```

No `total` field here, unlike `SessionListResponse`. Search results are typically shown as a finite list without pagination in the current UI design.

## Defensive Patterns

- `"Untitled"` and `"unknown"` defaults provide safe display values without null checks in templates.
- `total` on `SessionListResponse` decouples count from pagination, supporting future page-size changes.
- `match_role` enables role-aware search UX without a separate API call.

## Known Gaps

- `SessionSearchResponse` has no `total` or `query` echo field — clients can't tell how many total matches exist or confirm which query produced the results.
- `last_activity` is a string with no enforced format, making client-side relative-time formatting ("2 hours ago") dependent on the string being parseable.
- No `archived` or `deleted` state on `SessionInfo` — soft-delete or archival would require adding a status field.