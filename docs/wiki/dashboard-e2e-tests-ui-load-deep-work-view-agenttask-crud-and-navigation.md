---
{
  "title": "Dashboard E2E Tests: UI Load, Deep Work View, Agent/Task CRUD, and Navigation",
  "summary": "This Playwright-based end-to-end test suite verifies that the PocketPaw dashboard loads correctly in a real browser, that key UI elements (chat view, Deep Work view, agent/task creation modals, sidebar, remote access) are present and functional, and that the dashboard title matches the expected beta branding.",
  "concepts": [
    "Playwright",
    "dashboard UI",
    "Deep Work view",
    "agent creation",
    "task creation",
    "Alpine.js",
    "browser automation",
    "end-to-end",
    "networkidle",
    "DOM assertions"
  ],
  "categories": [
    "testing",
    "end-to-end",
    "dashboard",
    "UI",
    "test"
  ],
  "source_docs": [
    "fb99e45563f843bf"
  ],
  "backlinks": null,
  "word_count": 412,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The dashboard is PocketPaw's primary user interface — a web application that non-developers use to manage agents, tasks, and conversations. These tests run against a live server instance using a real Chromium browser, catching rendering bugs, missing DOM elements, and JavaScript errors that unit tests cannot detect.

## Test Structure

The file is organized into six test classes, each covering a distinct UI surface:

- `TestDashboardLoads` — Page title, default chat view visibility, view tabs, agent mode toggle.
- `TestCrewView` — Deep Work tab switching, new agent button, new task button, stats bar.
- `TestAgentCreation` — Modal open, agent creation flow, agent deletion flow.
- `TestTaskCreation` — Modal open, task creation flow.
- `TestSidebarNavigation` — Sidebar presence, settings panel open.
- `TestRemoteAccessModal` — Remote access button presence.

## Why Browser Tests for a Dashboard?

PocketPaw's dashboard is built with Alpine.js and uses server-side HTML rendering with client-side hydration. Many behaviors (tab switching, modal open/close, stats bar population) are implemented in JavaScript that runs in the browser after the HTML loads. Unit tests for the Python backend cannot catch a broken JavaScript handler or a missing `x-data` attribute on an Alpine component. Playwright executes real browser JavaScript and asserts on the resulting DOM state.

## Key Test: Dashboard Title

```python
def test_dashboard_title(self, page: Page, dashboard_url: str):
    page.goto(dashboard_url, wait_until="networkidle")
    expect(page).to_have_title("PocketPaw (Beta)")
```

The `wait_until="networkidle"` strategy waits for all network requests to complete before asserting. This prevents flaky failures caused by asserting the title before the page's JavaScript has finished modifying `document.title`.

## Agent Creation Flow

`test_create_agent_flow` opens the agent creation modal and submits a form. It verifies that after submission the new agent appears in the agent list. `test_delete_agent_flow` then deletes that agent and asserts it disappears. These tests together confirm that the full CRUD cycle works end-to-end through the browser, including the WebSocket update that refreshes the list without a page reload.

## Stats Bar

`test_stats_bar_shows_numbers` checks that the Deep Work view's stats bar displays numeric values (not blank or placeholder text). This catches a regression where the stats endpoint returns an empty response or the Alpine.js binding fails to populate the element.

## Known Gaps

No test verifies error states — what happens when agent creation fails (e.g., validation error) or when the WebSocket disconnects mid-flow. All tests assume a clean server state; if a previous test leaves the agent list in an unexpected state, later tests may fail non-deterministically. There is also no accessibility testing (ARIA roles, keyboard navigation).