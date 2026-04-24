---
{
  "title": "BrowserTool: Playwright-Powered Web Automation with Accessibility Snapshots",
  "summary": "`BrowserTool` exposes browser automation to PocketPaw's agent via a Playwright-backed session manager, using accessibility tree snapshots (rather than raw HTML or pixel screenshots) for element identification — a technique that dramatically reduces token usage while maintaining LLM-parseable page structure.",
  "concepts": [
    "BrowserTool",
    "Playwright",
    "accessibility snapshot",
    "session manager",
    "ref markers",
    "browser automation",
    "token efficiency",
    "high trust",
    "action dispatch",
    "web scraping"
  ],
  "categories": [
    "tool-system",
    "browser-automation",
    "agent-capabilities"
  ],
  "source_docs": [
    "8595bb7f7c38ca08"
  ],
  "backlinks": null,
  "word_count": 421,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Web automation is one of the highest-value capabilities an AI agent can have. `BrowserTool` wraps Playwright through a session management layer, exposing a unified `action`-based API: navigate, click, type, scroll, snapshot, screenshot, and close. The design prioritizes token efficiency and LLM ergonomics over raw power.

## Accessibility Snapshots vs. Raw HTML

The critical design choice in `BrowserTool` is using accessibility tree snapshots rather than raw HTML or pixel screenshots for element identification. An accessibility snapshot returns a structured tree of interactive elements with `[ref=N]` numeric identifiers. The LLM can then issue actions like `click ref=3` without processing thousands of tokens of HTML.

This matters because:
- Raw HTML of a complex page can be 50,000+ tokens — prohibitively expensive for every page state check.
- Screenshots require vision models and are ambiguous for precise element targeting.
- Accessibility trees are typically 500–3,000 tokens and directly map to interactive elements.

## Session Manager Architecture

Rather than creating a new browser for each action, `BrowserTool` delegates to a `BrowserSessionManager` obtained via `get_browser_session_manager()`. Sessions are keyed by `session_id` (defaulting to `"default"`). This means:

- A single conversation can have one persistent browser session — the browser stays open between tool calls, maintaining login state, cookies, and navigation history.
- Multiple concurrent sessions (different `session_id` values) can run in isolation for multi-tab scenarios.
- The session manager handles Playwright lifecycle (browser, context, page) and cleanup.

## Action Dispatch

`execute(**params)` receives an `action` string and routes to the appropriate private method:

```python
async def execute(self, **params) -> str:
    action = params.get("action")
    session_id = params.get("session_id", self.DEFAULT_SESSION_ID)
    match action:
        case "navigate": return await self._navigate(params, session_id)
        case "click":    return await self._click(params, session_id)
        case "type":     return await self._type(params, session_id)
        case "scroll":   return await self._scroll(params, session_id)
        case "snapshot": return await self._snapshot(session_id)
        case "screenshot": return await self._screenshot(session_id)
        case "close":    return await self._close(session_id)
```

This single-tool, multi-action design reduces the number of tools the LLM must reason about — one `browser` tool covers the entire browser interaction surface.

## Trust Level: High

Browser automation is marked `trust_level = "high"` because it can visit arbitrary URLs, execute JavaScript (via Playwright), and exfiltrate page content. A basic or restricted agent profile will not receive this tool.

## Known Gaps

- **No headless/headed toggle** — the session manager defaults to headless mode. There is no per-call option to run a headed browser for visual debugging.
- **No proxy support** — all browser sessions use direct connections. Agents needing to access geo-restricted content or maintain specific egress IPs have no mechanism to configure a proxy.