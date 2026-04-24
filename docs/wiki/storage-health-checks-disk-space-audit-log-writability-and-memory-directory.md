---
{
  "title": "Storage Health Checks — Disk Space, Audit Log Writability, and Memory Directory",
  "summary": "This module provides three synchronous health checks for PocketPaw's local storage: verifying the config directory does not exceed 500 MB, confirming the audit log file is writable, and checking that the memory directory is accessible. All checks return `HealthCheckResult` objects with actionable fix hints.",
  "concepts": [
    "disk space check",
    "audit log",
    "writability check",
    "memory directory",
    "health checks",
    "HealthCheckResult",
    "rglob",
    "file permissions",
    "storage monitoring",
    "startup checks"
  ],
  "categories": [
    "health monitoring",
    "storage"
  ],
  "source_docs": [
    "67501e27db4c0748"
  ],
  "backlinks": null,
  "word_count": 566,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`storage.py` guards PocketPaw's local filesystem health. Three checks run at startup to catch storage problems before they silently corrupt session data, lose audit records, or block memory retrieval.

## Disk Space Check

`check_disk_space()` recursively sums the size of all files under `~/.pocketpaw/` using `Path.rglob("*")` and compares against a 500 MB threshold.

```python
total = sum(f.stat().st_size for f in config_dir.rglob("*") if f.is_file())
total_mb = total / (1024 * 1024)
if total_mb > 500:
    return HealthCheckResult(..., status="warning", message=f"Data directory is {total_mb:.0f} MB (>500 MB)")
```

The 500 MB threshold is chosen as a practical warning point. PocketPaw stores session transcripts, audit logs, and memory data — none of which should grow this large under normal conditions. Exceeding this limit typically signals accumulated old sessions or runaway audit logging. The check is `warning` rather than `critical` because the agent continues to function above 500 MB; the risk is eventual disk exhaustion, not immediate failure.

The `rglob("*")` approach is simple but performs a full directory walk every startup. On very large installs this could be slow, but for the typical home directory use case (~100 MB) it completes in milliseconds.

## Audit Log Writability Check

`check_audit_log_writable()` verifies that `audit.jsonl` can be written. PocketPaw appends security-relevant events to this file; if it is unwritable, those events are silently dropped.

The check handles two cases separately:

1. **File does not exist** — attempts to create it with `touch()`. If creation fails (e.g., parent directory is read-only), returns `warning` with a permissions hint.
2. **File exists** — opens it in append mode (`"a"`). This is the cheapest possible writability test: no data is written, but the OS will reject the `open()` call if the file is locked or permissions-blocked.

```python
with audit_path.open("a"):
    pass
```

Opening in append mode with no writes is the canonical pattern for testing writability without modifying file content. A `touch()` call would update the modification time, which could confuse monitoring tools watching the file.

## Memory Directory Check

`check_memory_dir_accessible()` verifies that the memory storage directory exists and is a directory (not a file with the same name). This check prevents a subtle failure mode where a plain file named `memory` exists at the config path — PocketPaw would silently fail to store or retrieve memories at runtime with confusing errors.

The check distinguishes between a missing directory (creates it via `mkdir`) and a path collision (a file exists where the directory should be), returning a `warning` in the latter case with an explicit "remove the file" instruction.

## Exception Handling Pattern

All three checks wrap their logic in broad `except Exception` handlers that return `warning` status. This prevents a failing check from propagating an exception to the health engine and crashing the startup sequence. A health check that cannot determine disk usage due to a permissions error should not itself become a critical error — it becomes a soft warning.

## Known Gaps

- The 500 MB threshold is hardcoded. A future improvement would make it configurable in settings.
- `check_disk_space()` performs a synchronous `rglob` walk on every call. For large installations this could block the event loop if called from an async context (it is currently called only from synchronous startup checks).
- There is no check for available free disk space on the filesystem — only used space in the PocketPaw directory. A full disk would not be caught until write operations fail at runtime.
