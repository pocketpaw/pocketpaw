---
{
  "title": "CLI Status Command: Live Agent Status with Watch Mode",
  "summary": "The `status` command polls the PocketPaw agent's REST status endpoint to display global state (active sessions, uptime, concurrent session capacity) and per-session detail (channel, tool, duration, error). It supports a `--watch` mode that redraws the terminal at a configurable interval, providing a live dashboard view.",
  "concepts": [
    "agent status",
    "watch mode",
    "REST API",
    "session list",
    "ANSI clear screen",
    "httpx",
    "duration formatting",
    "status API key",
    "authentication"
  ],
  "categories": [
    "CLI",
    "Observability"
  ],
  "source_docs": [
    "43ab2dda94e8891b"
  ],
  "backlinks": null,
  "word_count": 482,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/status.py` implements the `pocketpaw status` subcommand. It queries `http://localhost:{port}/api/v1/agent/status` and renders a structured view of the running agent's state. Unlike the health command (which checks configuration), `status` reflects runtime activity — what sessions are active and what the agent is currently doing.

## API Authentication

The status endpoint supports an optional API key via the `X-Status-Key` header:

```python
key = os.environ.get("POCKETPAW_STATUS_API_KEY", "")
if key:
    headers["X-Status-Key"] = key
```

The key is read from the environment rather than from the config file. This allows CI systems and monitoring agents to pass the key without loading the full PocketPaw config stack. When the key is absent, the header is omitted — the endpoint decides whether to require it based on its own settings (`status_api_key` in `Settings`).

## Error Handling

Three failure modes are distinguished:

- **`httpx.ConnectError`** → returns `None` (agent not running)
- **`httpx.HTTPStatusError`** → prints status code to stderr, returns `None`
- **Generic `Exception`** → prints error to stderr, returns `None`

Returning `None` instead of raising allows the callers (`run_status` and `_run_watch`) to handle "not running" without exception handling at the call site. Errors go to stderr so they do not pollute stdout when `--json` mode is in use by a script.

## Status Table Rendering

The human-readable table format is structured in two sections: global summary and per-session detail.

```
PocketPaw Status
  State:    IDLE
  Sessions: 0 / 10
  Uptime:   2h 15m 42s

Active Sessions
  SESSION              CHANNEL      STATE              TOOL         DURATION
  chat-abc123          discord      tool_use           shell        1m 23s
```

Session state is truncated to 18 characters to keep the table aligned. Error states are rendered inline:

```python
if state == "error":
    msg = s.get("error_message", "")
    state = f"error: {msg}"[:18]
```

Truncating to 18 characters preserves alignment at the cost of potentially cutting off error messages. The full error is available via `--json`.

## Duration Formatting

`_format_duration` converts float seconds into a human-readable `Xs`, `Xm Ys`, or `Xh Ym Zs` string. This three-tier format covers session durations from seconds to multi-hour conversations without unnecessary precision.

## Watch Mode

`_run_watch` uses ANSI escape sequences to clear the screen between refreshes:

```python
print("[2J[H", end="")
```

`[2J` clears the screen; `[H` moves the cursor to the top-left. This combination produces a smooth in-place update without the flicker of a true `curses`-based UI. The refresh interval is user-configurable via `--watch <seconds>`. A `KeyboardInterrupt` exits cleanly.

## Known Gaps

- **No differential highlighting**: In watch mode, sessions that appeared since the last refresh are not highlighted. An operator managing a busy deployment cannot easily spot new sessions.
- **`title` field falls back to `session_id`**: Sessions that have not been named (via `chat_title_generation_enabled`) show their raw session ID, which is typically a UUID — not very readable.
- **ANSI clear screen breaks non-terminal consumers**: If `--watch --json` is used, the ANSI clear sequences are emitted before each JSON block, corrupting the output for scripts. This combination is not guarded against.
