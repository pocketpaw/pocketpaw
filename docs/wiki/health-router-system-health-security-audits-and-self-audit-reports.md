---
{
  "title": "Health Router — System Health, Security Audits, and Self-Audit Reports",
  "summary": "The health router aggregates PocketPaw's observability surface into a single API domain: version information, health engine summaries, error log management, on-demand health checks, security audit runs, and a scheduled self-audit report system. It was extracted from the monolithic `dashboard.py` to give each concern a clean, testable home.",
  "concepts": [
    "health engine",
    "HealthSummary",
    "security audit",
    "self-audit",
    "version endpoint",
    "error log",
    "health check",
    "audit log",
    "observability",
    "diagnostic API",
    "liveness"
  ],
  "categories": [
    "API",
    "Monitoring",
    "Security"
  ],
  "source_docs": [
    "d2c1a114bdeea568"
  ],
  "backlinks": null,
  "word_count": 422,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Operational visibility is a first-class concern in PocketPaw. The health router consolidates all diagnostic and audit endpoints under `/api/v1/`, covering everything from a simple version ping to a full security audit run. The design separates read operations (health summary, audit reports) from write operations (clear errors, trigger checks) at the HTTP method level.

## Version Endpoint

`GET /version` returns the PocketPaw version string, the Python interpreter version, and the configured `agent_backend`. This is the canonical liveness check — any HTTP client can hit this endpoint to confirm the server is up and determine its capabilities before making further API calls.

## Health Engine Integration

`GET /health` delegates to `get_health_engine().summary`, which aggregates the state of all registered health checks (disk space, memory, connectivity, backend reachability, etc.) into a `HealthSummary` Pydantic model. The endpoint wraps the call in a broad `except Exception` and returns a degraded `HealthSummary(error=str(e))` rather than propagating an HTTP 500 — this ensures the health endpoint itself is always reachable even when the health engine is broken.

`POST /health/check` triggers all health checks synchronously and returns their results — useful for diagnosing issues on demand rather than waiting for the periodic background run.

`POST /health/errors/clear` resets the persistent error log. This is important for operators who have investigated and resolved a known error and want to acknowledge it without restarting the server.

## Security Audit

`POST /audit/security` runs PocketPaw's built-in security audit checklist — credential exposure checks, config file permissions, key rotation age, etc. — and returns the results as a `SecurityAuditResponse`. The audit log can be cleared independently via `DELETE /audit/log`.

## Self-Audit Report System

PocketPaw includes a scheduled self-audit system that produces dated reports stored on disk:

- `GET /audit/self` — lists available reports (date-keyed summaries)
- `GET /audit/self/{date}` — retrieves a specific report
- `POST /audit/self/run` — triggers a new self-audit run immediately

The date-keyed report storage allows operators to track health trends over time without a separate monitoring database.

## Extraction from `dashboard.py`

The module comment notes extraction from `dashboard.py`. The original monolithic dashboard handled auth, chat, health, identity, and settings in a single file. Extracting health into its own router makes each concern independently testable and allows the health endpoints to be mounted (or omitted) independently of the rest of the API.

## Known Gaps

The health and audit endpoints have no authentication requirements in the visible source (no `require_scope` dependency on the router). If these endpoints are publicly accessible, an unauthenticated client could read the security audit results, which might reveal configuration weaknesses.