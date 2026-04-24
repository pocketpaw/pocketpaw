---
{
  "title": "CLI Errors Command: Viewing and Filtering Recent Runtime Errors",
  "summary": "The `errors` CLI command surfaces recent entries from the health engine's error store, showing timestamp, severity, source, message, and the last line of any traceback. It supports text-based filtering and JSON output to integrate with automated alerting workflows.",
  "concepts": [
    "error store",
    "health engine",
    "severity",
    "traceback",
    "CLI errors",
    "search filter",
    "audit log",
    "ANSI colors",
    "JSON output"
  ],
  "categories": [
    "CLI",
    "Diagnostics and Health"
  ],
  "source_docs": [
    "f3ea7fe51baf7560"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/errors.py` implements the `pocketpaw errors` subcommand. It queries the health engine's in-memory (or persisted) error store for recent entries and renders them in a compact, severity-colored format. This command exists because the audit log (`pocketpaw logs`) records all events, while the error store is focused exclusively on error-level and above events, making it faster to triage production failures.

## Why a Separate Errors Command?

The full audit log can grow large in busy deployments. Searching it for errors requires parsing every line. The health engine maintains a separate, bounded error store specifically for exception-level events, making `pocketpaw errors` much faster than `pocketpaw logs | grep ERROR`. It also surfaces data that may not appear in the audit log at all — for example, internal errors during health checks that occur before the audit logger is fully initialized.

## Severity Color Coding

```python
if severity == "critical":
    icon = f"{RED}[CRIT]{RESET}"
elif severity == "error":
    icon = f"{RED}[ERR]{RESET} "
else:
    icon = f"{YELLOW}[WARN]{RESET}"
```

The three-tier severity display (`CRIT`, `ERR`, `WARN`) maps directly to the health engine's severity taxonomy. Both critical and error severities use red to ensure they are visually distinct from warnings. The extra space after `[ERR]` aligns the source column consistently with the wider `[CRIT]` and `[WARN]` icons.

## Timestamp Truncation

```python
if isinstance(timestamp, str) and len(timestamp) > 19:
    timestamp = timestamp[:19]
```

ISO 8601 timestamps include microseconds and timezone offsets that are rarely useful at a glance. Truncating to 19 characters (`YYYY-MM-DDTHH:MM:SS`) keeps lines narrow enough to read without wrapping in standard terminal widths.

## Traceback Last Line

When a traceback is present, only the last line is shown:

```python
last_line = tb.strip().splitlines()[-1] if tb.strip() else ""
```

Full tracebacks can span dozens of lines and overwhelm the terminal. The last line is almost always the most actionable (`ValueError: invalid literal for int()`, `ConnectionRefusedError: [Errno 111]`). Operators who need the full traceback can access it via the JSON output (`--json`) or the raw error store.

## Search Filtering

The `search` parameter is passed directly to `engine.get_recent_errors(search=search or "")`. An empty string disables filtering, so the `search or ""` pattern unifies the two code paths without a conditional.

## JSON Output

When `--json` is requested, the raw error dicts from the engine are output directly without transformation. This preserves all fields, including full tracebacks and any custom metadata, making the JSON output suitable for ingestion by log aggregators like Elasticsearch or Loki.

## Known Gaps

- **No date range filtering**: The `limit` parameter controls how many recent errors to show, but there is no `--since` or `--until` flag to filter by time range. In high-traffic deployments where the error store cycles quickly, `--limit 100` may not reach errors from earlier in the day.
- **Error store persistence is engine-dependent**: Whether errors survive a restart depends on the health engine implementation. If the engine stores errors only in memory, `pocketpaw errors` shows nothing after a restart. The CLI command has no way to surface this limitation to the operator.
