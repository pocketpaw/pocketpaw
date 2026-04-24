---
{
  "title": "Playwright Browser Driver — LLM-Oriented Browser Control",
  "summary": "BrowserDriver wraps Playwright's async API into a simplified interface designed for LLM-driven browser automation, where the agent reads accessibility tree snapshots instead of raw HTML and interacts via stable integer reference numbers. It handles Playwright import validation at construction time, auto-installs Chromium if no browser binary is found, and prefers the system's installed Chrome to avoid downloading a separate Chromium build.",
  "concepts": [
    "BrowserDriver",
    "Playwright",
    "NavigationResult",
    "auto-install Chromium",
    "system Chrome",
    "accessibility tree",
    "RefMap",
    "async context manager",
    "fail-fast import",
    "viewport",
    "browser automation"
  ],
  "categories": [
    "browser",
    "playwright",
    "agent-tools"
  ],
  "source_docs": [
    "0000000000000007"
  ],
  "backlinks": null,
  "word_count": 472,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why a Custom Driver Wrapper?

Playwright's raw API is powerful but verbose, and it returns page state as a live DOM that is too large and noisy to inject into a model prompt. `BrowserDriver` solves both problems: it provides a minimal method surface tailored to agent tool calls, and after every navigation or interaction it returns a `NavigationResult` containing a compact semantic snapshot plus the current `RefMap`.

## Key Design Decisions

### Fail-Fast Import Check
The constructor immediately attempts `import playwright` and calls `require_extra("playwright", "browser")` if the import fails. This surfaces a clear error at session creation time rather than at the first `navigate()` call, which could be minutes into an agent session. The pattern is consistent with PocketPaw's broader strategy of catching missing optional dependencies at the earliest reasonable point.

### System Chrome Priority
At launch time, `BrowserDriver` checks for an existing system Chrome or Chromium binary before falling back to auto-installing Playwright's bundled Chromium. The rationale: production servers and developer machines often already have Chrome installed. Downloading a ~120 MB Chromium bundle unnecessarily wastes bandwidth and disk space, and in environments with strict egress controls it may be outright blocked.

### Auto-Install Chromium
If no browser binary is found, `_install_chromium()` runs `playwright install chromium` as a subprocess. This enables zero-config deployment: a fresh PocketPaw instance with only `playwright` the Python package installed can acquire a working browser on its first use.

### Async Context Manager
`BrowserDriver` implements `__aenter__`/`__aexit__` so it can be used with `async with`, guaranteeing the browser is closed even if an exception occurs mid-session. The `close()` method closes the page, browser, and Playwright instance in order, preventing resource leaks that would accumulate over many agent sessions.

### NavigationResult Pattern
Methods that change page state (`navigate`, `click`, `type_text`, `scroll`) return a `NavigationResult` dataclass containing the post-action snapshot and the updated `RefMap`. This immutable snapshot-per-action design means the agent always sees the state *after* the action completes, and the caller never needs to call a separate `snapshot()` method manually.

### Default Viewport
The `DEFAULT_VIEWPORT` of 1280×720 is a common desktop resolution that renders most responsive websites in their "desktop" layout without triggering mobile-specific UI patterns that may be harder for the model to navigate.

## Interaction Methods

| Method | What It Does |
|---|---|
| `navigate(url)` | Load a URL, wait for network idle, return snapshot |
| `click(ref)` | Click element by `[ref=N]`, return snapshot |
| `type_text(ref, text)` | Type into element by `[ref=N]`, return snapshot |
| `scroll(direction)` | Scroll up/down by `SCROLL_AMOUNT` pixels, return snapshot |
| `screenshot(path)` | Save PNG screenshot to disk (no snapshot returned) |

## Known Gaps

The `SCROLL_AMOUNT` constant (500 pixels) is fixed. Pages with very tall fixed headers or sticky navigation may require multiple scrolls to reach content that a larger increment could reach in one step.