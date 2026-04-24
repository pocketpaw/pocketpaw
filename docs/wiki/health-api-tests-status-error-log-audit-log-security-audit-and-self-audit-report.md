---
{
  "title": "Health API Tests: Status, Error Log, Audit Log, Security Audit, and Self-Audit Reports",
  "summary": "This test file covers PocketPaw's `/api/v1/health` router and related audit endpoints, verifying health check aggregation, error log retrieval and clearance, manual health check triggering, structured audit log reading, security audit execution, and the self-audit report lifecycle.",
  "concepts": [
    "health engine",
    "health check",
    "error log",
    "audit log",
    "JSONL",
    "security audit",
    "self-audit reports",
    "get_health_engine",
    "resilience",
    "reverse chronological order",
    "seven-check security audit"
  ],
  "categories": [
    "health monitoring",
    "security",
    "API",
    "testing",
    "audit",
    "test"
  ],
  "source_docs": [
    "ad5ff75908ad86da"
  ],
  "backlinks": null,
  "word_count": 542,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw runs a background health engine that continuously checks system components and accumulates errors. The health API surfaces this state to the dashboard. This test file also covers two audit subsystems: the append-only security audit log (JSONL) and the self-audit report store (date-keyed JSON files).

## Health Status (`GET /health`)

`TestHealthStatus` covers two cases:

- **Engine available**: The health engine's `summary` dict (containing `status`, `check_count`, and `issues`) is returned directly. The test asserts `status == "healthy"` to confirm the field is propagated.
- **Engine not available**: If `get_health_engine()` raises `RuntimeError` (e.g., the engine was never initialised), the route returns 200 with `status: "unknown"` rather than 500. This is a deliberate resilience pattern — the dashboard should always be able to render something, even when the health subsystem itself is broken. A 500 would mask the real problem and break the dashboard's polling loop.

## Error Log (`GET /health/errors`, `DELETE /health/errors`)

- **Filtered retrieval**: `test_get_errors_with_search` asserts that `limit` and `search` query parameters are forwarded to `engine.get_recent_errors(limit=5, search="test")`. This confirms the route does not silently ignore filter parameters.
- **Clear**: Deleting errors calls `engine.error_store.clear()` and returns `{"cleared": true}`. The test uses `assert_called_once()` to prevent double-clear bugs.

## Trigger Health Check (`POST /health/check`)

Allows the dashboard to force an immediate health check cycle rather than waiting for the next scheduled run. The test confirms `engine.run_all_checks` is awaited and the subsequent `summary` is returned.

## Audit Log (`GET /audit`, `DELETE /audit`)

`TestAuditLog` uses a real temporary JSONL file to test the audit log endpoints:

```python
f.write(json.dumps({"action": "login", ...}) + "\n")
f.write(json.dumps({"action": "logout", ...}) + "\n")
# GET /audit should return entries in reverse chronological order
assert logs[0]["action"] == "logout"
```

- **Reverse order**: The most recent entry comes first. The test specifically writes login before logout, then asserts logout is first in the response — catching any implementation that returns entries in insertion order.
- **Empty log**: When the log file does not exist, the endpoint returns an empty array rather than 404, preventing dashboard errors when audit logging has never triggered.
- **Clear**: The DELETE endpoint truncates the file to zero bytes (verified by `read_text() == ""`), preserving the file rather than deleting it so future writes do not need to recreate it.

## Security Audit (`POST /security-audit`)

The security audit runs seven checks: config permissions, plaintext API keys, audit log health, guardian reachability, file jail, tool profile, and bypass permissions. The test patches all seven to return `(True, "ok", False)` and asserts `total == 7`, `passed == 7`, `issues == 0`. This is a contract test — if a new check is added to the implementation without updating this test, the count assertion will fail.

## Self-Audit Reports (`GET /self-audit/reports`, `GET /self-audit/reports/{date}`, `POST /self-audit/run`)

Self-audit reports are stored as date-keyed JSON files under `<config_dir>/audit_reports/`. Tests cover:

- Empty directory returns `[]`.
- A report file named `2026-02-20.json` appears in the list with `date: "2026-02-20"`.
- Fetching by date returns the parsed JSON content.
- A non-existent date returns 404.
- `POST /self-audit/run` delegates to `run_self_audit` and returns its result.

## Known Gaps

No `TODO` or `FIXME` markers are present. The tests do not cover concurrent writes to the audit JSONL file, or what happens if an audit report JSON file is malformed.