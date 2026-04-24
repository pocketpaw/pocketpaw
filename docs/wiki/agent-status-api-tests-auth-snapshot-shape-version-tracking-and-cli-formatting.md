---
{
  "title": "Agent Status API Tests: Auth, Snapshot Shape, Version Tracking, and CLI Formatting",
  "summary": "Tests for PocketPaw's agent status API endpoint, covering authentication via header and query parameter, snapshot structure validation against the API contract, version-based change detection for efficient polling, and CLI duration formatting helpers.",
  "concepts": [
    "StatusTracker",
    "status API",
    "version tracking",
    "long polling",
    "auth guard",
    "snapshot shape",
    "CLI formatting",
    "x-status-key",
    "HTTPException"
  ],
  "categories": [
    "testing",
    "API",
    "monitoring",
    "test"
  ],
  "source_docs": [
    "f6776d1af59e7b3e"
  ],
  "backlinks": null,
  "word_count": 436,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw exposes a `/status` API endpoint that lets external tools — dashboards, CLI monitors, and orchestrators — observe the agent's current state without coupling to its internal event bus. This test file validates the endpoint's authentication, data shape, change-detection protocol, and CLI output formatting.

## Authentication

`TestAgentStatusAuth` covers four auth scenarios:

- Wrong key → `HTTPException` (401/403). Prevents unauthorized status inspection in multi-tenant deployments.
- Correct key via `x-status-key` header → allowed.
- Correct key via `?key=` query parameter → allowed. Supporting both access methods makes the endpoint usable from both programmatic clients (header) and browser/curl (query param).
- No key configured → allowed. When `POCKETPAW_STATUS_KEY` is unset, the endpoint is open, which is appropriate for single-user local deployments.

The `_clear_status_key_cache()` autouse fixture is critical: the key is cached after first read (an optimization to avoid repeated env lookups). Without resetting this cache between tests, a test that sets the env var would corrupt the cached value for subsequent tests, causing order-dependent failures.

## Snapshot Shape

`TestSnapshotShape` validates that the snapshot structure matches the documented API contract:

- Idle snapshot has `global.state == "idle"` and `active_sessions == 0` with an empty `sessions` list.
- Active snapshot includes the running session's metadata.

These shape tests prevent the API from silently drifting from its contract when internal `StatusTracker` fields are renamed or restructured.

## Version Tracking

`TestVersionTracking` validates the optimistic polling protocol:

- The `version` counter increments on every state change. Clients can store their last-seen version and poll only for changes.
- `test_wait_for_change_returns_immediately_when_version_advanced`: If the client's version is already behind the current version when they call `wait_for_change`, the call returns immediately rather than blocking. This prevents a "missed wake-up" where the client is stuck waiting for a change that already happened.

Version-based change detection is the mechanism that allows the PocketPaw dashboard to update in near-real-time without WebSocket infrastructure.

## CLI Formatting

`TestCLIFormat` tests three duration formatting helpers used by the CLI status display:

- Seconds-only output for short durations.
- Minutes format for medium durations.
- Hours format for long-running sessions.

These helpers exist because exposing raw seconds to CLI users is poor UX — "Running for 3732s" is less readable than "Running for 1h 2m 12s".

## Known Gaps

The `wait_for_change` long-poll mechanism is tested for the "already stale" case but not for the "actually waits N seconds then returns" case, which would require time-mocking to test reliably without making the test suite slow.

```python
# Auth fixture pattern — cache reset between tests
@pytest.fixture(autouse=True)
def _clear_status_key_cache():
    from pocketpaw.api.v1 import agent_status
    if hasattr(agent_status._get_status_api_key, "_value"):
        del agent_status._get_status_api_key._value
    yield
    if hasattr(agent_status._get_status_api_key, "_value"):
        del agent_status._get_status_api_key._value
```
