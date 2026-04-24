---
{
  "title": "Health Diagnostic Tools: System Checks, Error Logs, and Config Doctor",
  "summary": "The `health.py` module provides three diagnostic `BaseTool` subclasses — `HealthCheckTool`, `ErrorLogTool`, and `ConfigDoctorTool` — that give the PocketPaw agent access to the runtime health engine, persistent error log, and configuration validator. These tools exist so the agent can self-diagnose problems when a user reports something is broken, rather than returning a generic error and asking the user to check logs manually.",
  "concepts": [
    "HealthCheckTool",
    "ErrorLogTool",
    "ConfigDoctorTool",
    "health engine",
    "error log",
    "config validation",
    "playbook",
    "lazy import",
    "BaseTool",
    "diagnostic tools",
    "connectivity checks"
  ],
  "categories": [
    "builtin tools",
    "observability",
    "diagnostics",
    "system health"
  ],
  "source_docs": [
    "31174864749a343d"
  ],
  "backlinks": null,
  "word_count": 571,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`health.py` was created 2026-02-17 as part of the Phase 1 Health Engine. The design philosophy is: when something breaks, the agent should be able to diagnose the problem itself and suggest a fix, without requiring the user to open a terminal or read log files. The three tools in this module cover three diagnostic surfaces: live system checks, historical error logs, and configuration validation.

## HealthCheckTool

Tool name: `health_check`. Runs the PocketPaw health engine's startup checks and returns structured results with status codes (`ok`, `warning`, `critical`) and fix hints for each check. Checks include config validation, storage availability, and optionally LLM API connectivity.

The `include_connectivity` flag (default `False`) controls whether the connectivity checks run. These checks are intentionally opt-in because they make real outbound API calls and are slower than local checks. The description explains this tradeoff directly so the agent knows when to pass `true`:

```
"Also run connectivity checks (slower, tests LLM API). Default: false."
```

The health engine is imported lazily inside `execute` rather than at module level:

```python
async def execute(self, include_connectivity: bool = False) -> str:
    try:
        from pocketpaw.health import get_health_engine
        engine = get_health_engine()
        results = engine.run_startup_checks()
```

This lazy import prevents the health engine (which itself initializes subsystems) from loading when the tool module is imported, avoiding startup cost for sessions that never use the health tool.

## ErrorLogTool

Tool name: `error_log`. Reads recent entries from the persistent health error log — a file where the health engine and other subsystems write structured error records. Parameters: `limit` (default 20) and an optional `search` string to filter entries.

The persistent error log exists to capture errors that happen asynchronously or during startup, before the agent is available to receive them. When a user reports "the dashboard crashed last night," the agent can call `error_log` to retrieve the stored error records and diagnose the cause retroactively.

The `search` filter exists because logs can be large. Without filtering, returning 20 unrelated entries wastes context window space. The agent can pass a search term (e.g., `"google"`, `"auth"`) to focus the results.

## ConfigDoctorTool

Tool name: `config_doctor`. Validates the PocketPaw configuration and returns a playbook-backed diagnosis — not just "this field is missing" but "this field is missing; here is how to fix it." The optional `section` parameter scopes the check to a specific config section (e.g., `"llm"`, `"storage"`) rather than running a full validation.

The "playbook-backed" framing is significant: the config doctor maps each known misconfiguration to a remediation step from a maintained playbook. This turns a raw validation error into actionable guidance, which the agent can relay directly to the user.

## Usage pattern

The agent is instructed to call `health_check` when the user reports problems:

```
"Use this when the user reports problems or asks about system status."
```

This guidance is embedded in the tool description rather than in a system prompt, ensuring it travels with the tool regardless of which pocket or session uses it.

## Known Gaps

- **No auto-fix capability**: The config doctor diagnoses but cannot apply fixes. There is no `ConfigRepairTool` that could automatically correct known misconfigurations.
- **No health history**: `HealthCheckTool` returns the current state only. There is no way to retrieve historical check results to identify degradation trends over time.
- **No alert threshold configuration**: The health engine has hardcoded thresholds for `warning` vs `critical` status. There is no tool to adjust these thresholds for specific deployment environments.