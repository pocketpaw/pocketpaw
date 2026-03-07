# Changelog

All notable changes to PocketPaw will be documented in this file.

## [Unreleased]

### Fixed

- **Per-tool execution timeout** — `ToolRegistry.execute()` now wraps tool calls
  in `asyncio.wait_for()` with a configurable timeout (default: 60 s).  A stuck
  or unresponsive tool no longer blocks the agent session indefinitely.  Timeout
  events are recorded in the audit log with `action="tool_timeout"`.  Set
  `POCKETPAW_TOOL_TIMEOUT=0` to disable.  ([#494](https://github.com/pocketpaw/pocketpaw/issues/494))
