---
{
  "title": "CLI Logs Command: Audit Log Viewer and Tail Mode",
  "summary": "The `logs` command reads PocketPaw's JSONL audit log at `~/.pocketpaw/audit.jsonl`, rendering recent entries in a color-coded table or streaming new entries in real time via a tail loop. It handles malformed JSON lines gracefully and renders multi-field entries by normalizing common key aliases.",
  "concepts": [
    "audit log",
    "JSONL",
    "log tail",
    "follow mode",
    "CLI logs",
    "color coding",
    "log rotation",
    "polling",
    "event normalization"
  ],
  "categories": [
    "CLI",
    "Observability"
  ],
  "source_docs": [
    "9b5e514ddba292fd"
  ],
  "backlinks": null,
  "word_count": 518,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/logs.py` implements the `pocketpaw logs` subcommand. The audit log is a newline-delimited JSON (JSONL) file written by various PocketPaw subsystems to record tool executions, session events, errors, and other significant actions. This command makes that log human-accessible without requiring `jq` or manual file navigation.

## JSONL Format and Flexible Parsing

The audit log is JSONL — one JSON object per line. Because different subsystems may emit slightly different schemas, `_print_entry` normalizes several field name aliases:

```python
event = entry.get("event", entry.get("type", entry.get("action", "unknown")))
level = entry.get("level", entry.get("severity", "info"))
detail = entry.get("detail", entry.get("message", entry.get("data", "")))
```

This fallback chain means entries from tool executions (`event`/`detail`), health checks (`type`/`message`), and channel events (`action`/`data`) all render correctly without format-specific parsing logic. Invalid or non-JSON lines are silently dropped by `_parse_line`, preventing a single malformed entry from crashing the display.

## Efficient Tail: `_tail_lines`

Rather than streaming the entire file and discarding early lines, `_tail_lines` reads all lines into memory and slices the last `n`:

```python
all_lines = f.readlines()
lines = all_lines[-n:]
```

This is simple but reads the entire file into memory. For audit logs that grow large over months, this approach trades memory for implementation simplicity. The function wraps the read in a bare `except Exception: pass`, so permission errors or file system races during log rotation are silently ignored.

## Follow Mode: `_follow_log`

The `--follow` flag activates a simple polling loop that seeks to the end of the file on open, then repeatedly calls `readline()`:

```python
f.seek(0, 2)  # seek to end
while True:
    line = f.readline()
    if line:
        _print_entry(_parse_line(line))
    else:
        time.sleep(0.5)
```

Seeking to the end before the loop is the key idiom: it prevents replaying the entire existing log when follow mode starts. The 0.5-second sleep prevents busy-waiting. A `KeyboardInterrupt` exits cleanly with code 0.

This polling approach is portable (works on macOS, Linux, and Windows) but less efficient than `inotify`/`kqueue`-based file watching. For high-throughput audit logs, 0.5-second latency between writes and display is acceptable.

## Color Coding

Log levels map to colors:

- `error` / `critical` → red
- `warning` → yellow
- `ok` / `success` → green
- Everything else → no color (terminal default)

This makes it easy to scan a busy log for problems without reading every line.

## Timestamp Normalization

Timestamps are truncated to 19 characters (`YYYY-MM-DDTHH:MM:SS`), consistent with the errors command. This strips microseconds and timezone suffixes that clutter the display.

## Known Gaps

- **Memory usage for large logs**: `_tail_lines` reads the entire file into memory before slicing. A log that has been running for months and grows to hundreds of megabytes would exhaust memory on constrained hosts. A proper reverse-read implementation (seeking from the end) would fix this.
- **No filtering by level or source**: Unlike `pocketpaw errors`, there is no `--search`, `--level`, or `--source` flag. The operator must pipe the JSON output through `jq` to filter.
- **Log rotation not handled**: If the audit log is rotated (renamed and a new file started) while `--follow` is active, the command continues reading the renamed file and misses new entries. Inotify-based watching or periodic file re-open would address this.
