---
{
  "title": "Browser Session Manager — Named Session Pool with Idle Cleanup",
  "summary": "BrowserSession and BrowserSessionManager provide a named pool of persistent browser sessions so that agent tools can reuse an open browser across multiple tool calls without launching and closing a new browser for every interaction. Per-session asyncio locks prevent race conditions when concurrent tool calls reference the same session, and idle cleanup reclaims resources from sessions that have not been used within a configurable timeout.",
  "concepts": [
    "BrowserSession",
    "BrowserSessionManager",
    "get_or_create",
    "session pool",
    "asyncio Lock",
    "idle cleanup",
    "cleanup_idle",
    "touch",
    "get_browser_session_manager",
    "session lifecycle",
    "concurrent tool calls"
  ],
  "categories": [
    "browser",
    "session-management",
    "concurrency"
  ],
  "source_docs": [
    "0000000000000008"
  ],
  "backlinks": null,
  "word_count": 425,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Problem Sessions Solve

Without session management, every browser tool call would open a new browser, navigate to the target page, perform the action, and close the browser. This is prohibitively slow (browser launch alone takes 1-3 seconds) and breaks multi-step workflows: step 2 cannot build on the logged-in state from step 1 if they use different browser instances.

`BrowserSessionManager` solves this by maintaining a dictionary of named sessions. A session persists across tool calls until explicitly closed or until idle cleanup reclaims it.

## BrowserSession

`BrowserSession` is a simple dataclass wrapping a `BrowserDriver` with lifecycle metadata:

- **`session_id`** — Caller-assigned string identifying the session. Convention is to use a task or conversation ID so all tool calls within the same agent turn share a session.
- **`created_at` / `last_used_at`** — UTC timestamps used by idle cleanup to identify stale sessions.
- **`touch()`** — Updates `last_used_at` to now. Called by the manager on every `get_or_create()` access so that actively-used sessions are never incorrectly flagged as idle.

Both timestamps use `datetime.now(tz=UTC)` consistently, which prevents off-by-one errors in idle time calculations that would occur if one timestamp used a naive datetime and another used a timezone-aware one.

## BrowserSessionManager

### get_or_create()
`get_or_create(session_id, headless)` is the primary entry point. It:

1. Acquires the per-session lock.
2. Returns the existing session if one exists for `session_id`.
3. Otherwise, creates a new `BrowserDriver`, launches it, wraps it in a `BrowserSession`, and stores it.

The per-session lock (acquired via `_get_lock()`) prevents two concurrent tool calls with the same `session_id` from each deciding the session doesn't exist and creating duplicate browser instances. The global lock in `_get_lock()` itself protects the `_locks` dict from simultaneous insertions.

### cleanup_idle(timeout_seconds)
`cleanup_idle()` closes any session whose `last_used_at` is older than `timeout_seconds` and returns the count of sessions cleaned up. This is intended to be called periodically by a background task to reclaim browser processes from agent sessions that ended without explicitly closing their browsers.

### close_all()
Closes every open session. Called during shutdown to ensure no browser processes are left running after the agent runtime exits.

## Singleton Access

`get_browser_session_manager()` returns the process-level singleton instance. Agent tools import this function rather than holding a direct reference to the manager, which allows the singleton to be replaced in tests with a fresh manager that has no pre-existing sessions.

## Known Gaps

`cleanup_idle()` must be called explicitly; there is no built-in background task that runs it on a schedule. A deployment that never calls it will accumulate idle browser processes until the host runs out of memory.