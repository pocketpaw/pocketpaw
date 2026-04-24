---
{
  "title": "Audit Logging System: Append-Only Agent Action Trail",
  "summary": "This module implements a secure, append-only audit log that records every critical agent action to a JSONL file at `~/.pocketpaw/audit.jsonl`. It provides structured severity tiers, optional PII filtering, a callback fan-out mechanism, and a singleton accessor — forming the forensic backbone of PocketPaw's security stack.",
  "concepts": [
    "audit logging",
    "AuditSeverity",
    "AuditEvent",
    "AuditLogger",
    "JSONL",
    "append-only log",
    "PII filtering",
    "scrub_event_dict",
    "callback fan-out",
    "singleton pattern",
    "agent security",
    "forensic trail"
  ],
  "categories": [
    "security",
    "observability",
    "agent runtime"
  ],
  "source_docs": [
    "f672bd1d7d8fe2d2"
  ],
  "backlinks": null,
  "word_count": 551,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The audit logging system exists because agent runtimes operate with broad system permissions: they execute shell commands, call APIs, read and write files, and handle user credentials. Without an immutable record of these operations, there is no way to investigate incidents after the fact, detect compromised behavior, or demonstrate compliance. `audit.py` is the answer to "what happened, when, and who did it?"

## Severity Tiers

The `AuditSeverity` enum establishes four escalating levels:

```python
class AuditSeverity(StrEnum):
    INFO = "info"      # Normal operation (e.g. reading a file)
    WARNING = "warning"  # Potentially dangerous (e.g. writing a file)
    CRITICAL = "critical" # High risk (e.g. shell command, deleting file)
    ALERT = "alert"    # Security violation (e.g. blocked command)
```

This tiering allows downstream consumers (dashboards, alerting systems) to filter noise from signal. A tool reading a config file is `INFO`; a blocked shell injection attempt is `ALERT`.

## AuditEvent: Structured Log Entry

`AuditEvent` is a dataclass capturing: `id` (UUID4), `timestamp` (UTC ISO8601), `severity`, `actor` (who acted — user ID or `"agent"`), `action` (what category of operation), `target` (the specific object acted upon), `status` (allow/block/error/success), and an open `context` dict for arbitrary metadata.

The factory method `AuditEvent.create()` generates the UUID and timestamp automatically, preventing common mistakes like missing IDs or non-UTC timestamps.

## AuditLogger: Append-Only Persistence

`AuditLogger` writes events to `~/.pocketpaw/audit.jsonl` in JSONL (newline-delimited JSON) format. JSONL was chosen deliberately: each line is a self-contained valid JSON object, making the file streamable, grep-able, and resilient to partial writes — a truncated final line does not corrupt earlier entries.

The `log()` method applies two mandatory scrubbing passes before any data touches disk:

1. `scrub_event_dict()` — strips credential-looking fields (API keys, tokens, passwords) from the event dict, preventing secrets embedded in tool params from riding along into logs.
2. Optional `_filter_pii()` — when enabled, recursively scans all string values against the `PIIScanner`, masking emails, phone numbers, and similar personal data.

If the write itself fails (disk full, permission error), the logger falls back to Python's standard `logging.critical()`. Critically, it re-scrubs the event before passing it to syslog, because the syslog destination may be less controlled than the local JSONL file.

## Callback Fan-Out

The `on_log()` method allows external consumers to register callbacks that fire after each successful write. This powers real-time alerting (e.g., dashboard websockets) without coupling the audit logger to any specific transport. Callbacks are isolated: one failing callback does not break others or the write itself.

## Convenience Helpers

`log_tool_use()` and `log_api_event()` are thin wrappers that pre-fill common fields (`actor="agent"`, `action="tool_use"`) so callers don't repeat boilerplate. Both return the event ID, enabling callers to correlate log entries with their own records.

## Singleton Pattern

`get_audit_logger()` returns a process-level singleton so all modules share one file handle and one callback registry. This avoids race conditions from multiple loggers writing to the same file.

## Known Gaps

- **No log rotation**: The JSONL file grows unbounded. There is no built-in rotation, compression, or archival. High-volume deployments will accumulate large files over time.
- **No integrity verification**: The file is append-only by convention, not by cryptographic enforcement. A process with write access to `~/.pocketpaw/` could modify or delete entries.
- **Callbacks are synchronous**: If a callback performs I/O (e.g., HTTP alert), it blocks the log path. An async callback queue would be safer for high-throughput scenarios.