---
{
  "title": "Self-Audit Daemon: Periodic Security and Health Checks",
  "summary": "The self-audit module runs a suite of automated checks against the PocketPaw runtime environment — stale sessions, config conflicts, disk usage, audit log size, and orphaned OAuth tokens — and saves timestamped JSON reports to `~/.pocketpaw/audit_reports/`. It is designed to run on a schedule and surface problems before they become incidents.",
  "concepts": [
    "self-audit",
    "security checks",
    "stale sessions",
    "config conflicts",
    "disk usage",
    "audit log",
    "OAuth tokens",
    "health monitoring",
    "automated checks",
    "report generation"
  ],
  "categories": [
    "Daemon",
    "Security"
  ],
  "source_docs": [
    "e939f45126e5897f"
  ],
  "backlinks": null,
  "word_count": 426,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/daemon/self_audit.py` implements PocketPaw's self-healing audit loop. Rather than waiting for users to notice problems, the daemon proactively scans its own state and flags issues. This is part of the Phase 2 Integration Ecosystem, created 2026-02-07.

## Why Automated Self-Auditing?

PocketPaw runs as a persistent background process with access to API keys, session history, and file system. Without periodic self-checks, problems compound silently: a forgotten OAuth token accumulates stale grants, an oversized audit log slows writes, or contradictory config settings erode security assumptions. `run_self_audit()` is the daemon's introspection mechanism.

## Check Suite

### Stale Sessions (`_check_stale_sessions`)

Scans `~/.pocketpaw/memory/sessions/` for `.json` files whose modification time is older than `max_age_days` (default: 30). Stale sessions consume disk space and may contain outdated context that could mislead the agent if accidentally loaded. The check returns the count and names of stale session files.

### Config Conflicts (`_check_config_conflicts`)

Examines `Settings` for dangerous combinations:

- `bypass_permissions=True` with `plan_mode=True` — contradictory; bypass mode skips plan-mode safety checks.
- `injection_scan_enabled=False` with `plan_mode=False` — no safety net; both the prompt injection scanner and plan-mode review are disabled simultaneously.

These combinations don't cause immediate failures but create security-relevant states that operators may not have intended.

### Disk Usage (`_check_disk_usage`)

Recursively sums all file sizes under `~/.pocketpaw/`. Flags if total exceeds 500 MB. This prevents the config directory from growing unboundedly due to session history, cached model responses, or uploaded files.

### Audit Log Size (`_check_audit_log_size`)

Checks `~/.pocketpaw/audit.jsonl` file size. Flags if over 50 MB and recommends rotation. The audit log is append-only and never auto-trimmed; without this check, long-running instances would accumulate multi-gigabyte logs.

### Orphaned OAuth Tokens (`_check_orphan_oauth_tokens`)

Identifies OAuth token files that no longer correspond to a configured channel. For example, if a user removes their Discord integration, the OAuth token file may remain on disk. Orphaned tokens are a credential hygiene issue — they grant access to external services but serve no purpose.

## Report Format

`run_self_audit()` returns a dict with:

```python
{
    "timestamp": "ISO timestamp",
    "checks": {
        "stale_sessions": {"passed": bool, "message": str},
        "config_conflicts": {"passed": bool, "message": str},
        "disk_usage": {"passed": bool, "message": str},
        "audit_log_size": {"passed": bool, "message": str},
        "orphan_oauth_tokens": {"passed": bool, "message": str}
    },
    "overall": "ok" | "warning" | "error"
}
```

The report is also written to `~/.pocketpaw/audit_reports/<timestamp>.json` for historical review.

## Known Gaps

- The audit is not yet wired to the ProactiveDaemon scheduler automatically. It must be triggered explicitly or via a separate cron intention.
- There is no alerting path — a failed check is written to the report but does not push a notification to the user.