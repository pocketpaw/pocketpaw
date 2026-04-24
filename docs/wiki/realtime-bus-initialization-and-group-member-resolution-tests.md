---
{
  "title": "Realtime Bus Initialization and Group Member Resolution Tests",
  "summary": "This test module verifies the wiring of `init_realtime`, ensuring the correct bus backend is selected by default and that the `AudienceResolver` is properly exposed. It also covers `GroupService.list_member_ids` behavior for found and missing groups.",
  "concepts": [
    "init_realtime",
    "InProcessBus",
    "AudienceResolver",
    "POCKETPAW_REALTIME_BUS",
    "GroupService",
    "list_member_ids",
    "realtime bus",
    "singleton wiring",
    "fallback behavior",
    "monkeypatch"
  ],
  "categories": [
    "realtime",
    "testing",
    "event bus",
    "group service",
    "test"
  ],
  "source_docs": [
    "5a1a5967f14317d0"
  ],
  "backlinks": null,
  "word_count": 454,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/realtime/test_wiring.py` module validates the initialization contract for the PocketPaw realtime subsystem. When a client calls `init_realtime()`, the system must configure a message bus and a resolver that downstream code can rely on. These tests exist because the bus and resolver are global singletons — incorrect wiring here silently breaks all realtime event delivery.

## Bus Selection: InProcess Default

The first test, `test_init_realtime_uses_inprocess_by_default`, clears the `_bus` singleton and removes the `POCKETPAW_REALTIME_BUS` environment variable before calling `init_realtime()`. The test then asserts that the singleton is an `InProcessBus` instance.

The reason for resetting `_bus = None` before each test is critical: since the bus is a module-level singleton, a previous test run could leave it populated. Without the reset, the assertion would pass trivially without actually exercising the initialization path. This pattern prevents false positives that mask configuration bugs.

## Resolver Exposure

`test_init_realtime_exposes_resolver` checks that after `init_realtime()`, calling `get_resolver()` returns an `AudienceResolver` instance. The `AudienceResolver` is responsible for computing which WebSocket connections should receive a given event — it powers the fan-out logic. Without it being registered, all realtime broadcasts silently reach nobody.

Both `_bus` and `_resolver` singletons are explicitly set to `None` before the test, for the same idempotency reasons described above.

## Graceful Fallback for Unsupported Backends

`test_init_realtime_falls_back_to_inprocess_for_unsupported_bus` sets `POCKETPAW_REALTIME_BUS=redis` and then calls `init_realtime()`. The test asserts that despite the env-var pointing to a named bus type, the result is still `InProcessBus`, and that a warning log containing "redis" was emitted.

This fallback prevents a startup crash when an operator configures an unsupported or mistyped bus name. By degrading gracefully to in-process mode, the system remains functional in development and misconfigured environments, while the warning surfaces the configuration mistake without bringing down the service.

## GroupService Member Resolution

The two async tests for `GroupService.list_member_ids` cover the happy path (group found, returns `members` list) and the missing-group path (returns empty list). Both patch the internal `_fetch_group` method directly, keeping the tests unit-scoped without requiring a database.

The empty-list fallback for a missing group is significant: callers that iterate over member IDs to fan out messages must receive an iterable, never `None`. A `None` return would raise a `TypeError` at broadcast time — a failure that could drop messages silently in fire-and-forget contexts.

```python
async def test_group_list_member_ids_returns_empty_for_missing_group():
    from ee.cloud.chat.group_service import GroupService

    async def fake_get(_gid: str):
        return None

    from unittest.mock import patch
    with patch("ee.cloud.chat.group_service.GroupService._fetch_group", fake_get, create=True):
        ids = await GroupService.list_member_ids("gmissing")
    assert ids == []
```

## Known Gaps

No TODO or FIXME markers are present. The tests do not cover a Redis bus being fully initialized (only the fallback case). There is no test for what happens when `_resolver` is already set — the resolver initialization is not tested for idempotency independently of the bus.