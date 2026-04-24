---
{
  "title": "Presence Broadcast and Grace Window Tests",
  "summary": "This test module verifies the WebSocket endpoint's presence online/offline lifecycle, specifically the grace window that delays broadcasting a user offline until reconnection is no longer possible. Tests drive _schedule_presence_offline directly and use time-acceleration to keep the suite fast.",
  "concepts": [
    "presence",
    "WebSocket",
    "grace window",
    "PRESENCE_GRACE_SECONDS",
    "PresenceOffline",
    "ConnectionManager",
    "_schedule_presence_offline",
    "asyncio cancellation",
    "reconnect",
    "time acceleration"
  ],
  "categories": [
    "testing",
    "realtime",
    "WebSocket",
    "presence",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_presence_broadcast.py"
  ],
  "backlinks": null,
  "word_count": 467,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_presence_broadcast.py` covers Task 19, Cluster A sub-PR 4: the presence wiring that announces when users connect and disconnect from the WebSocket endpoint. Presence is not a simple toggle — a user who closes and reopens their browser within a few seconds should not appear briefly offline. The grace window is the mechanism that absorbs these transient disconnects.

## The Grace Window Pattern

When a WebSocket disconnects, the system does **not** immediately broadcast `presence.offline`. Instead it schedules a coroutine via `_schedule_presence_offline` that waits `PRESENCE_GRACE_SECONDS` before emitting. If the user reconnects before the timer fires, the scheduled task is cancelled and no offline event is sent.

This design prevents the common problem of users appearing to flicker online/offline during page navigations or brief network interruptions.

## Test: Grace Window Fires When User Stays Offline

```python
async def test_schedule_presence_offline_emits_after_grace_when_user_stays_offline():
    ...
    with patch.object(chat_router, "PRESENCE_GRACE_SECONDS", 0.05):
        await chat_router._schedule_presence_offline("user-42")
        await asyncio.sleep(0.12)
    events = [e for e in recorded if isinstance(e, PresenceOffline)]
    assert len(events) == 1
    assert events[0].data == {"user_id": "user-42"}
```

The test patches `PRESENCE_GRACE_SECONDS` to 0.05 seconds (50ms) rather than the production value, then sleeps 0.12 seconds to give the scheduled task time to complete. This time-acceleration approach keeps the test fast without requiring fake clocks or manual event loop control. The assertion confirms that exactly one `PresenceOffline` event was emitted with the correct user ID.

## Test: Reconnect Within Grace Window Cancels Offline

`test_schedule_presence_offline_cancelled_when_user_reconnects` simulates a reconnect arriving before the grace window expires. The test starts the offline schedule, then simulates a reconnect (which cancels the pending task) before 50ms has elapsed, then waits for the full grace period to pass. The assertion is that **zero** `PresenceOffline` events were emitted — the cancelled task should never have fired.

This test guards against a race condition where a reconnect arrives but the offline event still fires because the cancellation was missed or the task had already started executing.

## Why the Tests Import via importlib

The test imports `chat_router` via `importlib.import_module("ee.cloud.chat.router")` rather than a direct import. This is because `PRESENCE_GRACE_SECONDS` is patched as an attribute on the module object using `patch.object(chat_router, ...)`. The `importlib` import returns a live module reference that `patch.object` can modify at the module level, which is necessary when the constant is read inside a coroutine at call time rather than at import time.

## ConnectionManager Interaction

The tests also drive the `ConnectionManager` singleton from `ee.cloud.chat.ws` to verify that connecting a socket registers presence and disconnecting triggers the grace window scheduler. This ensures the full connect/disconnect lifecycle is wired correctly, not just the isolated scheduler function.

## Known Gaps

No TODOs or FIXMEs are present. The test suite does not cover multi-tab scenarios where a user has multiple WebSocket connections open simultaneously — the offline event should only fire when the *last* connection for a user closes.
