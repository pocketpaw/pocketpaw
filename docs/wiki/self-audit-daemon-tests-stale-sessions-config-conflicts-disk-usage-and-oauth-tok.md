---
{
  "title": "Self-Audit Daemon Tests — Stale Sessions, Config Conflicts, Disk Usage, and OAuth Tokens",
  "summary": "This test file covers `pocketpaw.daemon.self_audit`, a background health-check daemon that monitors for stale sessions, dangerous config combinations, excessive disk usage, oversized audit logs, and orphaned OAuth tokens. Tests validate each individual check and the full `run_self_audit` orchestrator.",
  "concepts": [
    "self-audit daemon",
    "stale sessions",
    "config conflicts",
    "bypass_permissions",
    "plan_mode",
    "injection_scan_enabled",
    "disk usage",
    "audit log size",
    "OAuth tokens",
    "background health check",
    "run_self_audit"
  ],
  "categories": [
    "testing",
    "daemon",
    "security",
    "health monitoring",
    "test"
  ],
  "source_docs": [
    "48f729eb12391315"
  ],
  "backlinks": null,
  "word_count": 464,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_self_audit_daemon.py` (created 2026-02-07) tests the self-audit daemon module. Unlike the security audit CLI (which is run manually), this daemon runs periodically in the background during normal PocketPaw operation, detecting configuration drift and resource accumulation before they cause user-visible problems.

## Stale Session Detection

`TestStaleSessions` tests `_check_stale_sessions()`:

- **No sessions directory** — OK with message `"No sessions"`. This is the state on first run; the check should not error.
- **No stale sessions** — a `recent.json` file in the sessions directory is treated as a recent-activity marker; the check passes.

The stale session check exists to detect abandoned WebSocket sessions that were never cleaned up (e.g., the client disconnected without a clean close). Over time these accumulate disk space and can confuse session listing.

## Config Conflict Detection

`TestConfigConflicts` tests `_check_config_conflicts()`:

- **No conflicts** — `bypass_permissions=False`, `plan_mode=False`, `injection_scan_enabled=True` → OK.
- **Bypass with plan mode** — `bypass_permissions=True` combined with `plan_mode=True` → not OK. This combination means the agent will plan actions without permission checks — a dangerous operational mode. The message includes `"bypass_permissions"` for actionable feedback.
- **No safety net** — `injection_scan_enabled=False` alone → not OK. Injection scanning is the last line of defense against prompt injection attacks; disabling it without any other compensating control is flagged.

```python
def test_no_safety_net(self):
    mock_settings.injection_scan_enabled = False
    ok, msg = _check_config_conflicts()
    assert ok is False
    assert "safety net" in msg
```

## Disk Usage

`TestDiskUsage` tests `_check_disk_usage()` with a small test directory. The test just verifies the check runs without error — thresholds and exact warning conditions are implicit in the production implementation.

## Audit Log Size

`TestAuditLogSize` tests `_check_audit_log_size()`:

- **No audit log** — OK (log hasn't been created yet).
- **Small audit log** — OK. The check warns only when the log grows beyond a configured threshold, which a fresh small file doesn't reach.

An oversized audit log is a performance concern: it can slow down append operations and make incident review unwieldy.

## OAuth Token Cleanup

`TestOAuthTokens` tests `_check_orphan_oauth_tokens()`:

- **No OAuth directory** — OK.
- **Tokens present** — runs without crashing. The test does not assert a specific outcome, only that the check completes, because the production logic inspects token expiry timestamps which are hard to control in unit tests.

## Full Audit Orchestrator

`test_run_self_audit` runs `run_self_audit(tmp_path)` as an integration test and asserts it completes without raising. This smoke test catches wiring errors (e.g., a check that crashes instead of returning a result tuple) that individual unit tests would miss.

## Known Gaps

No `TODO` or `FIXME` markers. The stale session, disk usage, and audit log size tests are minimal — they verify the checks don't crash but don't test the warning thresholds or the specific conditions that trigger not-OK results. More thorough threshold testing is likely needed as those values are tuned.
