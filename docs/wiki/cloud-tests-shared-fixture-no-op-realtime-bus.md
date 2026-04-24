---
{
  "title": "Cloud Tests Shared Fixture: No-Op Realtime Bus",
  "summary": "This conftest.py installs an autouse fixture that replaces the realtime event bus with a no-op AsyncMock for the duration of every cloud test. This prevents AssertionError crashes when services call emit() in tests that do not start the full application lifecycle.",
  "concepts": [
    "conftest.py",
    "autouse fixture",
    "realtime bus",
    "AsyncMock",
    "no-op bus",
    "test isolation",
    "emit",
    "init_realtime",
    "bootstrap problem",
    "teardown"
  ],
  "categories": [
    "testing",
    "fixtures",
    "realtime",
    "configuration",
    "test"
  ],
  "source_docs": [
    "tests/cloud/conftest.py"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/conftest.py` solves a bootstrap problem: the realtime event bus (`ee.cloud.realtime.bus`) is only initialized during application startup via `init_realtime`. Unit tests that call `GroupService`, `MessageService`, or any other service with emit calls do not start the app, so the bus module's internal `_bus` reference is `None`. Any `emit()` call in that state would raise an `AssertionError` (or `AttributeError`), failing the test for the wrong reason.

## The Fixture

```python
@pytest.fixture(autouse=True)
def _install_noop_bus():
    from ee.cloud.realtime import bus as bus_mod
    prev = bus_mod._bus
    bus_mod._bus = AsyncMock()
    yield
    bus_mod._bus = prev
```

The `autouse=True` makes this fixture run for every test in the `tests/cloud/` directory tree without requiring explicit opt-in. The fixture:

1. **Saves** the current `_bus` value (which may be `None` or a real bus from a previous test)
2. **Replaces** it with an `AsyncMock` — a coroutine that accepts any arguments and returns a mock
3. **Yields** control to the test
4. **Restores** the original value after the test completes

The restore step is the teardown guard. Without it, a test that sets up a real bus (e.g., an integration test later in the suite) would find the `AsyncMock` already installed.

## Why This Approach Instead of Patching Per-Test

Each test that calls a service with emit behavior would need to add a `patch("ee.cloud.realtime.bus._bus", AsyncMock())` context manager. This is repetitive and easy to forget. An autouse conftest fixture provides the guarantee at the directory level — any test added to `tests/cloud/` gets the no-op bus automatically.

This is also more resilient than patching `emit` directly in each test. Some tests (like the emit contract tests in `test_group_emits.py`) replace `emit` with a recording function. If those tests forgot to patch the bus, the recording function would run but any code path that falls through to the real `emit` would still crash. The no-op bus fixture provides a safe fallback layer underneath.

## Relationship to Emit Contract Tests

The emit contract tests (e.g., `test_group_emits.py`) install their own recording `fake_emit` via `patch("ee.cloud.chat.group_service.emit", new=fake_emit)`. That patch takes precedence over the no-op bus in the module-level namespace where `GroupService` calls `emit`. The no-op bus remains as a backstop for any emit calls that go through the bus module directly rather than the patched reference.

## Known Gaps

No TODOs or FIXMEs are present. If a future refactor moves `_bus` to a different attribute name or module path, this fixture will silently stop working — the `AsyncMock` will be installed on the wrong object and `emit()` will still crash in tests.
