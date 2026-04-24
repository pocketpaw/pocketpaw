---
{
  "title": "API Backward Compatibility and Versioned Endpoint Tests",
  "summary": "This module verifies that both the legacy `/api/` endpoints and the versioned `/api/v1/` endpoints in the PocketPaw dashboard respond with HTTP 200, guarding against breaking changes during the v1 API migration. It also checks that OpenAPI documentation endpoints remain accessible.",
  "concepts": [
    "backward compatibility",
    "API versioning",
    "FastAPI",
    "TestClient",
    "dashboard",
    "/api/v1/",
    "OpenAPI",
    "health endpoint",
    "localhost auth",
    "route testing"
  ],
  "categories": [
    "API",
    "testing",
    "backward compatibility",
    "FastAPI",
    "test"
  ],
  "source_docs": [
    "d8dc79d3b3ec1e66"
  ],
  "backlinks": null,
  "word_count": 474,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_api_backward_compat.py` module was created on 2026-02-20 to protect the PocketPaw API surface during the introduction of versioned `/api/v1/` endpoints. The risk during API versioning is that adding the new prefix causes the old paths to stop routing — either due to route ordering bugs, middleware interference, or prefix conflicts.

## Why Backward Compat Tests Matter

PocketPaw has deployed clients (browser extension, mobile apps, third-party integrations) that call `/api/` paths directly. Even if the team is migrating to `/api/v1/`, the old paths must continue working. Breaking them would require coordinated rollout across all clients, which is impractical. These tests are the automated enforcement of that stability contract.

## Test Infrastructure

The `client` fixture creates a `fastapi.testclient.TestClient` from the actual `pocketpaw.dashboard.app` FastAPI instance. Every test class decorates with `@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)` to bypass the localhost-only authentication gate that would reject requests in a test environment.

## TestBackwardCompatEndpoints

This class exercises twelve legacy `/api/` paths:
- `/api/health` and `/api/health/errors` — service health
- `/api/telegram/status` — Telegram bot integration status (requires additional `Settings.load` mock)
- `/api/remote/status`, `/api/backends`, `/api/sessions`, `/api/channels/status`
- `/api/skills`, `/api/webhooks`, `/api/memory/long-term`, `/api/audit-log`, `/api/identity`

Every assertion is `assert resp.status_code == 200`. The tests are intentionally shallow — they check reachability, not response bodies. This is appropriate for backward-compat tests: the contract is "endpoint exists and responds," not "response shape is exact."

## TestV1Endpoints

This class mirrors the backward-compat tests for the `/api/v1/` equivalents, confirming the versioned paths work alongside the legacy ones. The same endpoints are covered: health, backends, sessions, channels, skills, webhooks, memory, and identity.

```python
def test_v1_health(self, _mock, client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
```

## TestOpenAPIDocs

Three tests verify that the OpenAPI documentation endpoints are reachable:
- `/openapi.json` — the machine-readable schema
- `/docs` — Swagger UI
- `/redoc` — ReDoc UI

These matter because external developers and automated SDK generators depend on the OpenAPI schema. If a route registration bug breaks the schema endpoint, SDK generation silently produces stale clients.

## Design Choice: Shallow Assertions

The decision to assert only on status codes rather than response bodies is deliberate for backward-compat tests. Response bodies are covered by the API contract tests in `test_api_contracts.py`, which use `frozenset` key assertions. Mixing body assertions into backward-compat tests would create fragile, redundant coverage. The backward-compat layer's only job is to ensure routes exist and respond — body correctness is a separate concern tested elsewhere. This separation keeps each test file focused on a single failure mode.

## Known Gaps

No TODO or FIXME markers. The tests check status codes only, not response body shapes — a route that returns `{}` with a 200 would pass. The Telegram status test requires an additional mock for `Settings.load`; other endpoints that have similar config dependencies might silently depend on defaults that could change. There are no tests for error conditions (401, 404, 500) on these paths.