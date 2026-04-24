---
{
  "title": "Google Calendar Tools: Event Listing, Creation, and Meeting Preparation",
  "summary": "The `calendar.py` module provides three `BaseTool` implementations — `CalendarListTool`, `CalendarCreateTool`, and `CalendarPrepTool` — that give PocketPaw's agent read/write access to Google Calendar and the ability to generate pre-meeting briefings. These tools were part of the Phase 2 Integration Ecosystem and require an authenticated Google OAuth session.",
  "concepts": [
    "CalendarListTool",
    "CalendarCreateTool",
    "CalendarPrepTool",
    "Google Calendar",
    "OAuth",
    "UTC datetime",
    "meeting prep",
    "attendees",
    "Phase 2",
    "calendar integration"
  ],
  "categories": [
    "tool-system",
    "google-integration",
    "productivity"
  ],
  "source_docs": [
    "219449881f366a82"
  ],
  "backlinks": null,
  "word_count": 476,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Calendar management is a common personal productivity task that benefits from AI assistance. The three calendar tools in this module cover the primary use cases: reviewing what's coming up, creating new events, and preparing for the next meeting.

## CalendarListTool

Lists upcoming events from Google Calendar within a configurable time window. Key parameters:

- `days_ahead` (default: 1) — how far ahead to look.
- `max_results` (default: 10) — caps results to avoid overwhelming the agent context.

The tool returns event titles, times, locations, and attendee lists in a formatted string. The `days_ahead` default of 1 is optimized for the common query "what do I have today?" — the most frequent calendar lookup in daily assistant workflows.

Trust level is `"high"` because accessing calendar data requires OAuth credentials and reveals schedule information. The tool will fail gracefully (returning an error string) if the Google OAuth session is not configured.

## CalendarCreateTool

Creates a Google Calendar event with support for all standard event fields:

- `summary` (required) — event title.
- `start` / `end` — ISO 8601 datetime strings; the tool parses these using `datetime.fromisoformat()` with UTC normalization.
- `description`, `location`, `attendees` — optional enrichment fields.

The `attendees` parameter accepts a list of email addresses. The tool constructs a Calendar API request and sends the event invite to all attendees. This is marked `"high"` trust because it sends calendar invites to external email addresses — an action that has social and professional consequences if triggered incorrectly.

## CalendarPrepTool

`CalendarPrepTool` is the most sophisticated of the three. It finds the next upcoming meeting, fetches its event details (title, description, attendees, location), and generates a briefing. The briefing typically includes:

- A summary of who is attending and their roles (if the agent has contact context).
- Agenda items extracted from the event description.
- Suggested talking points or questions.

This tool chains two operations: a calendar list call to find the next event, followed by a cognitive synthesis step that generates the briefing text. The synthesis is done by the agent's LLM backend rather than the tool itself — the tool returns structured data and the agent composes the narrative.

```python
async def execute(self) -> str:
    events = await self._list_next_event()
    if not events:
        return "No upcoming meetings found."
    event = events[0]
    return self._format_briefing(event)
```

## UTC Handling

All datetime operations use `datetime.UTC` explicitly, avoiding naive datetime objects that could produce incorrect results when users are in non-UTC timezones. The API returns times in RFC 3339 format; the tool normalizes to UTC before display.

## Known Gaps

- **No recurring event support** — the create tool creates single-occurrence events only. Recurring events (weekly standups, etc.) require direct Calendar API calls not exposed through this tool.
- **No event update or delete** — only list and create operations are implemented. Editing or cancelling existing events requires manual Calendar access.