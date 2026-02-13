# Scheduler & Self-Audit Daemon

PocketPaw includes a cron-based reminder scheduler for recurring tasks and a self-audit daemon for automated health checks.

## Cron Scheduler

The reminder scheduler supports one-time and recurring reminders with natural language time parsing.

### Natural Language Parsing

The scheduler understands common time expressions:

| Input | Parsed As |
|-------|-----------|
| `"in 5 minutes"` | 5 minutes from now |
| `"in 2 hours"` | 2 hours from now |
| `"at 3:30pm"` | Today at 3:30 PM (or tomorrow if past) |
| `"tomorrow"` | Tomorrow at current time |
| `"tomorrow at 9am"` | Tomorrow at 9:00 AM |

### Recurring Reminders

Create recurring reminders that persist across restarts:

```
You: "Remind me to check email every morning at 9am"
Me:  *creates recurring reminder with cron: "0 9 * * *"*
Me:  "Set! I'll remind you every day at 9:00 AM."
```

Key methods:
- `add_recurring(name, cron_expr, callback)` — Add a cron-based recurring job
- `delete_recurring(name)` — Remove a recurring job
- `add_reminder(text, time)` — Add a one-time reminder

### Persistence

Reminders are stored in `~/.pocketclaw/reminders.json` and automatically re-scheduled when PocketPaw starts.

### Backend

Uses APScheduler's `AsyncIOScheduler` for async-compatible scheduling.

---

## Self-Audit Daemon

Automated daily health and security checks that produce JSON reports.

### Schedule

Runs daily at 3:00 AM by default (configurable via cron expression).

### Checks (12 total)

The daemon runs 7 checks from the [Security Audit CLI](security.md) plus 5 daemon-specific checks:

**From Audit CLI (7):**
- Config file permissions
- Plaintext API key exposure
- Audit log existence and permissions
- Guardian AI status
- File jail configuration
- Tool profile review
- Bypass permissions flag

**Daemon-specific (5):**

| Check | Threshold | Description |
|-------|-----------|-------------|
| Stale sessions | >30 days | Warns about old session files |
| Config conflicts | — | Detects contradictory settings |
| Disk usage | >500 MB | Warns if `~/.pocketclaw/` exceeds threshold |
| Audit log size | >50 MB | Warns if audit log is growing too large |
| OAuth tokens | — | Checks for expired or invalid tokens |

### Reports

Reports are saved as JSON at `~/.pocketclaw/audit_reports/{YYYY-MM-DD}.json`:

```json
{
  "timestamp": "2026-02-07T03:00:00",
  "checks": [
    {"name": "config_permissions", "status": "pass"},
    {"name": "disk_usage", "status": "warn", "detail": "523 MB used"}
  ],
  "summary": {"pass": 10, "warn": 2, "fail": 0}
}
```

### Configuration

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `self_audit_enabled` | bool | `true` | Enable the self-audit daemon |
| `self_audit_schedule` | string | `"0 3 * * *"` | Cron schedule (default: 3 AM daily) |

### Integration

The self-audit daemon is registered as a recurring job in the scheduler on startup when `self_audit_enabled` is `true`.

---

## Implementation

| File | Description |
|------|-------------|
| `scheduler.py` | `ReminderScheduler` — cron jobs, natural language parsing, persistence |
| `daemon/self_audit.py` | `run_self_audit()` — 12 checks, JSON report generation |
| `security/audit_cli.py` | Shared security checks (7) used by both CLI and daemon |

## Tests

- `tests/test_scheduler_cron.py` — Scheduler and recurring reminder tests
- `tests/test_self_audit_daemon.py` — Self-audit daemon tests
