---
{
  "title": "Agent Loop Pocket Threading Tests: Cloud User and Workspace Context Resolution",
  "summary": "Tests for the `_create_pocket_and_session` and `_publish_pocket_event` functions in `pocketpaw.agents.loop`, covering all resolution paths for threading cloud user identity and workspace context through pocket creation: explicit ids, active-workspace fallback, first-user fallback for self-hosted deployments, graceful handling of invalid ids, and metadata propagation in pocket events.",
  "concepts": [
    "pocket threading",
    "cloud_user_id",
    "workspace resolution",
    "lazy imports",
    "sys.modules stubbing",
    "active_workspace fallback",
    "self-hosted fallback",
    "pocket creation",
    "session linking",
    "AsyncMock"
  ],
  "categories": [
    "testing",
    "agent loop",
    "cloud integration",
    "multi-tenancy",
    "test"
  ],
  "source_docs": [
    "6f1144a7ea27116c"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_agent_loop_pocket_threading.py` was created on 2026-04-22 to cover the pocket-creation path in `src/pocketpaw/agents/loop.py`. When an agent processes a message, it must create or retrieve a Pocket (a conversation container in the ee/cloud layer) and link it to the correct user and workspace. The resolution logic handles multiple deployment modes — multi-tenant cloud (explicit ids), single-user cloud (active_workspace fallback), and self-hosted (first-user fallback).

## Why Stub sys.modules Instead of Mocking

The loop's pocket-creation code imports `ee.cloud` models inside function bodies (lazy imports) to avoid a hard dependency on Mongo at startup. This means `unittest.mock.patch` cannot easily intercept the imports — by the time the patch runs, the import machinery has already resolved the module. The tests instead install fake modules directly into `sys.modules` before calling the function:

```python
monkeypatch.setitem(sys.modules, "ee.cloud.models.user", fake_user_mod)
monkeypatch.setitem(sys.modules, "ee.cloud.models.workspace", fake_ws_mod)
```

This technique is necessary and not a hack — it is the correct way to stub lazy-imported dependencies without modifying production code. The `_install_ee_cloud_stubs` helper centralises the wiring so individual tests only specify the behaviour they care about.

## Test Scenarios

### Explicit User and Workspace IDs
When `cloud_user_id` and `cloud_workspace_id` are both provided, the pocket is created against exactly those ids. No fallback logic runs. This is the standard multi-tenant cloud path.

### Active Workspace Fallback
When only `cloud_user_id` is provided, the loop reads `user.active_workspace` and uses that as the workspace id. If the user has no active workspace, the pocket is created with `workspace_id=None`.

### First-User Fallback for Self-Hosted
When no ids are provided at all, the loop falls back to the first user in the database and their owned workspace. This preserves the single-user self-hosted behaviour that predates the multi-tenant cloud layer.

### Invalid User ID — Graceful Degradation
```python
async def test_invalid_user_id_falls_back_cleanly(monkeypatch, caplog):
    # An unparseable cloud_user_id logs a warning and falls back to
    # the first-user behaviour rather than crashing the message pipeline.
```

This test is a regression guard: an early version of the code raised `ValueError` when `cloud_user_id` was a non-ObjectId string (e.g., a debug test value), crashing the entire message processing pipeline. After the fix, the invalid id is logged as a warning and the fallback runs instead.

### Session Linking
The Session document created alongside the pocket must be keyed to the resolved workspace id, not to any intermediate value. This ensures the ee/cloud session lookup (used by the conversation history API) returns the correct pocket.

### Pocket Event Metadata
`_publish_pocket_event` pulls `cloud_user_id` and `cloud_workspace_id` from the message metadata dict and forwards them to the event payload. Tests verify propagation when both are present and a safe no-op when metadata is `None`.

## Stub Architecture

`_install_ee_cloud_stubs` returns a `SimpleNamespace` with all the `AsyncMock` references so individual tests can assert call counts and arguments:

```python
stubs = _install_ee_cloud_stubs(monkeypatch, user=_mk_user("u1", active_workspace="ws-active"), ...)
assert stubs.pocket_create.call_count == 1
```

## Known Gaps

No test covers the case where both `User.get` and `User.find_one` return `None` simultaneously (no users in the database). The fallback chain does not define behaviour for this edge case.