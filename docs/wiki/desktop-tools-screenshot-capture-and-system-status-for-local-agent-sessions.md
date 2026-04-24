---
{
  "title": "Desktop Tools: Screenshot Capture and System Status for Local Agent Sessions",
  "summary": "`ScreenshotTool` and `StatusTool` are lightweight desktop interaction tools that give PocketPaw's agent visibility into the local machine — capturing the screen for visual context and querying system health metrics. Both tools are designed for local deployments where the agent runs on the same machine as the user.",
  "concepts": [
    "ScreenshotTool",
    "StatusTool",
    "desktop automation",
    "screenshot",
    "system status",
    "file jail",
    "UTC timestamp",
    "local agent",
    "pyscreenshot",
    "headless"
  ],
  "categories": [
    "tool-system",
    "desktop-integration",
    "monitoring"
  ],
  "source_docs": [
    "21cafe48434c0afc"
  ],
  "backlinks": null,
  "word_count": 383,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw can run locally, giving the agent access to the host machine beyond what a cloud-hosted agent can see. `ScreenshotTool` and `StatusTool` are the two desktop-level observation tools that make this local access useful: one shows the agent what the user's screen looks like, the other shows the health of the system the agent is running on.

## ScreenshotTool

`ScreenshotTool` calls `take_screenshot()` from `pocketpaw.tools.screenshot`, which wraps a platform-specific screenshot library (typically `pillow` + `pyscreenshot` or `mss`). The raw bytes are written to a timestamped file inside the agent's file jail:

```
~/.pocketpaw/files/screenshots/screenshot_20260423_143022.png
```

The tool returns the file path rather than the raw bytes or a base64 string. This is a deliberate design choice: base64-encoding a full-resolution screenshot can be 500KB–2MB of tokens. By saving to disk and returning the path, the agent can then use `deliver_artifact` to send it to the user, or use `BrowserTool`'s screenshot action (which returns a smaller, focused snapshot) for element-level inspection.

The UTC timestamp in the filename prevents collisions when multiple screenshots are taken in the same session.

If `take_screenshot()` returns empty bytes (which happens when no display is available — headless servers, CI environments), the tool returns a descriptive error rather than saving an empty file.

## StatusTool

`StatusTool` delegates to `get_system_status()` from `pocketpaw.tools.status`, which collects metrics like CPU usage, memory utilization, disk space, and running processes. The exact metrics depend on what `get_system_status()` returns, but the output is formatted as a human-readable string suitable for including in an agent response.

This tool is useful for debugging ("why is my agent slow?") and for agents running automated maintenance tasks that need to check system health before proceeding.

## No Trust Level Override

Neither tool overrides the `trust_level` property, inheriting the default (typically `"standard"`) from `BaseTool`. This reflects that reading a screenshot and system status is sensitive but not as dangerous as shell execution or file modification.

## Known Gaps

- **ScreenshotTool has no cropping or region selection** — it always captures the full primary monitor. For multi-monitor setups or for capturing a specific application window, there is no parameter to scope the capture.
- **StatusTool metrics are not structured** — the output is a formatted string rather than a JSON object. This makes it harder for the agent to act on specific metrics programmatically.