---
{
  "title": "Browser Automation Module Public API",
  "summary": "The browser package re-exports the public surface of PocketPaw's Playwright-based browser automation subsystem: snapshot types (RefMap, AccessibilityNode, SnapshotGenerator), the low-level driver (BrowserDriver, NavigationResult), and the session management layer (BrowserSession, BrowserSessionManager, get_browser_session_manager). These exports give agent tools a single import point for all browser interaction primitives.",
  "concepts": [
    "browser automation",
    "Playwright",
    "RefMap",
    "AccessibilityNode",
    "SnapshotGenerator",
    "BrowserDriver",
    "BrowserSession",
    "BrowserSessionManager",
    "get_browser_session_manager",
    "NavigationResult",
    "accessibility tree"
  ],
  "categories": [
    "browser",
    "agent-tools",
    "package-structure"
  ],
  "source_docs": [
    "0000000000000006"
  ],
  "backlinks": null,
  "word_count": 537,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `pocketpaw.browser` package provides Playwright-based browser automation purpose-built for LLM agent control. Unlike raw Playwright usage, this subsystem converts the browser's state into a semantic text snapshot that the model can read and act on using stable `[ref=N]` element identifiers rather than fragile CSS selectors or XPath expressions.

## Exported Symbols

### Snapshot Layer
- **`RefMap`** — A bidirectional mapping between integer reference numbers and element selectors. The LLM sees `[ref=5]` in the snapshot and uses that integer to request a click; `RefMap` resolves it back to the underlying selector.
- **`AccessibilityNode`** — Internal representation of a node in the browser's accessibility tree, converted from Playwright's raw dict format. Captures role, name, children, and properties like `focused`, `checked`, `disabled`.
- **`SnapshotGenerator`** — Converts a full accessibility tree into a compact, indented text representation suitable for inclusion in a model prompt.

### Driver Layer
- **`BrowserDriver`** — The async context manager wrapping Playwright. Exposes `navigate()`, `click()`, `type_text()`, `scroll()`, `screenshot()`, and `snapshot()` methods. Handles browser launch, auto-install of Chromium, and cleanup.
- **`NavigationResult`** — Dataclass pairing a rendered snapshot string with the current `RefMap` after any navigation or interaction. Returned by most driver methods so callers always have an up-to-date view.

### Session Layer
- **`BrowserSession`** — Tracks a single active browser session (driver instance + timestamps). Sessions are identified by a string `session_id` and support lifecycle metadata (`created_at`, `last_used_at`, `touch()`).
- **`BrowserSessionManager`** — Manages a pool of named sessions with `get_or_create()`, `close_session()`, `cleanup_idle()`, and `close_all()`. Uses per-session asyncio locks to prevent races when two tool calls reference the same session simultaneously.
- **`get_browser_session_manager()`** — Returns the process-level singleton `BrowserSessionManager`. Agent tools import this function to access the shared session pool without needing dependency injection.

## Design Rationale

The three-layer separation (snapshot, driver, session) allows each concern to be tested and replaced independently. The snapshot format is the key innovation: by working with the accessibility tree rather than raw HTML, the model receives a structured, low-noise view of the page that is far more token-efficient than the full DOM and far more stable than screenshot-based approaches.

## Why Accessibility Trees?

A rendered web page's full HTML can contain tens of thousands of nodes, inline scripts, stylesheets, and deeply nested `div` structures that convey no semantic meaning. Injecting raw HTML into a model prompt would consume the majority of the context window while making it harder for the model to identify what is actionable. The browser's built-in accessibility tree — the same structure used by screen readers — contains only semantically meaningful nodes: buttons, links, form inputs, headings, and text. `SnapshotGenerator` serialises this tree into plain indented text that is typically 90% smaller than the equivalent HTML.

## Typical Agent Tool Usage

Agent tools that perform web automation import `get_browser_session_manager` to obtain the singleton pool, call `get_or_create(session_id)` to retrieve or start a named session, then call methods on `session.driver` to navigate and interact. Snapshots returned by each driver call contain `[ref=N]` markers that the model can directly reference in subsequent tool calls. The session persists across multiple tool invocations in the same agent turn, preserving cookies, local storage, and page state.

## Known Gaps

None at the package level. Individual module gaps are noted in their respective articles.