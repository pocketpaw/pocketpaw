---
{
  "title": "Google Calendar Tool Schema and Auth Error Tests",
  "summary": "This test module validates the three Google Calendar tools (`CalendarListTool`, `CalendarCreateTool`, `CalendarPrepTool`) — checking their names, trust levels, and required parameter schemas — and verifies that each tool returns a user-readable error message rather than propagating an exception when OAuth authentication is unavailable.",
  "concepts": [
    "CalendarListTool",
    "CalendarCreateTool",
    "CalendarPrepTool",
    "Google Calendar",
    "OAuth",
    "trust_level",
    "CalendarClient",
    "tool schema",
    "error handling",
    "authentication"
  ],
  "categories": [
    "testing",
    "Google integrations",
    "tools",
    "test"
  ],
  "source_docs": [
    "38297d45cc8df0c7"
  ],
  "backlinks": null,
  "word_count": 557,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw integrates with Google Calendar via three tools:

- **`CalendarListTool`** — lists upcoming events (`calendar_list`).
- **`CalendarCreateTool`** — creates a new event (`calendar_create`).
- **`CalendarPrepTool`** — prepares/summarizes calendar context for the agent (`calendar_prep`).

All three require a valid OAuth token. When the token is absent, `CalendarClient._get_token` raises `RuntimeError("Not authenticated")`. The tests verify that this exception is caught at the tool layer and converted into a string error message that the agent can relay to the user.

## Tool Definition Tests (TestToolDefinitions)

Schema tests serve as a contract — if anyone changes a tool's name or removes a required parameter, these tests fail immediately, preventing silent API breakage.

```python
def test_calendar_list_tool(self):
    tool = CalendarListTool()
    assert tool.name == "calendar_list"
    assert tool.trust_level == "high"
```

`CalendarListTool` and `CalendarPrepTool` have `trust_level == "high"`, meaning they require explicit user authorization in restrictive tool profiles. `CalendarCreateTool` is tested for parameter presence: `summary`, `start`, and `end` are all required fields, and their presence in `tool.parameters["properties"]` is verified.

The difference between `CalendarListTool` and `CalendarPrepTool` reflects two different use cases: list returns raw event data for the agent to process, while prep returns a formatted summary that the agent can inject directly into its context. Both share the high trust requirement because they expose the same sensitive calendar data.

## Error Path Tests (No OAuth Token)

Each tool is tested with `_get_token` mocked to raise `RuntimeError("Not authenticated")`:

```python
async def test_calendar_list_no_auth():
    tool = CalendarListTool()
    with patch(
        "pocketpaw.integrations.gcalendar.CalendarClient._get_token",
        side_effect=RuntimeError("Not authenticated"),
    ):
        result = await tool.execute()
        assert "Error" in result
        assert "authenticated" in result.lower()
```

The assertions check two things:

1. The result contains `"Error"` — confirming that the exception was caught and formatted.
2. The result contains `"authenticated"` (case-insensitive) — confirming the error message is user-readable and actionable, telling the user they need to connect their Google account.

Without this contract, a tool that bubbles up an uncaught `RuntimeError` would crash the agent's response loop, producing a 500 error in the dashboard instead of a helpful message. The `test_calendar_create_no_auth` test passes minimal valid parameters (`summary`, `start`, `end`) to reach the auth check, verifying that auth failure is caught before any API call is attempted.

## Why Trust Level Is High

Calendar access can reveal sensitive information: meeting attendees, locations, private notes, recurring appointment patterns, and executive schedules. Requiring `trust_level == "high"` means these tools are disabled in the default `"basic"` tool profile and must be explicitly enabled by the user. This is consistent with PocketPaw's layered tool policy system where data-reading tools that access external services require elevated trust.

The test pins this requirement so that a refactor cannot accidentally downgrade the trust level. If someone changed `trust_level` to `"medium"` to make the tools available in more profiles, this test would catch the change and force a deliberate decision.

## Integration Architecture

All three tools delegate to `CalendarClient`, which handles OAuth token management and the Google Calendar API HTTP calls. The test file patches `CalendarClient._get_token` directly rather than the HTTP layer, which tests the correct abstraction boundary — the tools should not know or care about HTTP; they only interact with the client object.

## Known Gaps

No tests cover the happy path (successful token fetch and API call). There are no tests for `CalendarCreateTool` with invalid date formats, missing timezones, or events in the past. Token refresh and OAuth flow are not tested here.