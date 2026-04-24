---
{
  "title": "Health and Security Audit Schemas",
  "summary": "Defines Pydantic models for PocketPaw's health-monitoring and security-audit APIs, covering runtime health summaries, error log entries, per-check security results, and self-audit report summaries. These schemas give operators a structured view into the agent's operational integrity.",
  "concepts": [
    "HealthSummary",
    "SecurityCheckResult",
    "SecurityAuditResponse",
    "SelfAuditReportSummary",
    "health monitoring",
    "security audit",
    "fixable checks",
    "Pydantic",
    "agent runtime health",
    "error logging"
  ],
  "categories": [
    "api-schemas",
    "health-monitoring",
    "security"
  ],
  "source_docs": [
    "fdc405cef5dc5580"
  ],
  "backlinks": null,
  "word_count": 482,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's health subsystem runs background checks on the agent runtime — verifying configuration, connectivity, credential validity, and security posture. The five models in this file define what the health and security API endpoints return, forming a contract between the monitoring backend and the dashboard's status panels.

## Models

### `HealthSummary`

The top-level health snapshot.

```python
class HealthSummary(BaseModel):
    status: str = "unknown"
    message: str | None = None
    check_count: int = 0
    issues: list[dict] = []
    error: str | None = None
```

`status` defaults to `"unknown"` rather than `"healthy"` — a safe-fail choice. If the health engine hasn't run yet or failed to initialise, the dashboard shows unknown rather than falsely reporting health. `issues` is a list of raw dicts, keeping the shape flexible as different checks produce heterogeneous issue structures.

### `HealthErrorEntry`

A single entry from the health engine's error log.

```python
class HealthErrorEntry(BaseModel):
    timestamp: str
    level: str
    message: str
    source: str = ""
```

`timestamp` is a string rather than `datetime` to avoid timezone serialisation complexity. `source` identifies which component generated the error (e.g. `"memory_backend"`, `"mcp_server"`), enabling filtered views in the dashboard.

### `SecurityCheckResult`

The outcome of one discrete security check.

```python
class SecurityCheckResult(BaseModel):
    check: str
    passed: bool
    message: str
    fixable: bool
```

`fixable` is a critical UX flag. When `True`, the dashboard can offer a one-click remediation action. When `False`, the user knows manual intervention is required (e.g. rotating a leaked credential). Without this flag, the UI would have to guess whether to show an action button.

### `SecurityAuditResponse`

The aggregate result of a full security audit run.

```python
class SecurityAuditResponse(BaseModel):
    total: int
    passed: int
    issues: int
    results: list[SecurityCheckResult]
```

`total`, `passed`, and `issues` are pre-computed counters. Clients could derive these from `len(results)` and filtering on `passed`, but providing them directly avoids off-by-one errors in dashboard summary widgets and makes the response self-describing.

### `SelfAuditReportSummary`

A lightweight summary of a persisted audit report.

```python
class SelfAuditReportSummary(BaseModel):
    date: str
    total: int = 0
    passed: int = 0
    issues: int = 0
```

This model represents a stored audit record (not the live result) and is used for listing audit history. All counts default to zero so historical records with incomplete data don't cause `ValidationError` on deserialisation.

## Defensive Patterns

- `status: str = "unknown"` — safe default prevents false positives.
- Optional `error` and `message` fields allow partial health data to be returned even when some checks fail to execute.
- Pre-computed aggregate counts on `SecurityAuditResponse` reduce risk of client-side arithmetic errors.

## Known Gaps

- `issues: list[dict]` on `HealthSummary` is untyped. A dedicated `HealthIssue` model would make the issue shape explicit and allow per-issue severity levels.
- `HealthErrorEntry.level` is an unconstrained string. There's no validation against standard log levels (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`).
- `SecurityCheckResult.check` is a plain string with no registry, making it hard to know which checks exist without inspecting the health engine source.