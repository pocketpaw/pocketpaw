# Leading-edge push coalescer (feat/push-user-message-notifications).
# Created: 2026-07-02 — throttles per-recipient notification sends so a burst of
# messages in one conversation doesn't fan out one Web Push per message (which
# wastes encrypt+POST work and can trip the push vendor's per-endpoint rate
# limit with 429s). The OS already collapses same-``tag`` notifications on the
# view side; this bounds the SEND side.
#
# Semantics — LEADING-EDGE throttle, keyed by (workspace_id, user_id, group_id):
#   - First submit for an idle key   → emit IMMEDIATELY (zero added latency),
#                                        then open a cooldown window.
#   - Submits during the cooldown     → suppressed; the latest emit is remembered.
#   - When the window elapses          → if anything was suppressed, emit ONCE
#                                        (a trailing flush carrying the freshest
#                                        payload + a recomputed count) and keep
#                                        the window open one more cycle so a
#                                        sustained stream fires at most once per
#                                        window. An idle window closes and frees
#                                        the key.
#
# So a 20-message burst becomes at most 2 sends (leading + one coalesced), the
# recipient is still pinged the instant the first message lands, and a
# continuous stream is capped at 1 send per window.
#
# Window length: ``CLOUD_PUSH_COALESCE_SECONDS`` (default 5.0). ``0`` disables
# coalescing entirely — every submit emits immediately (pre-throttle behaviour).
# Named ``CLOUD_*`` to match the sibling ``CLOUD_PUSH_CONTACT`` — this cloud
# layer reads its own knobs from ``os.environ`` directly.
#
# Updated 2026-07-02: the leading edge now fires DETACHED (a background task,
# like the trailing flush) instead of being awaited inline. ``submit`` is
# awaited by the realtime bus handler, which the bus dispatches on the chat-send
# request path — awaiting the push fan-out there coupled send latency to
# delivery. ``submit`` now returns promptly and the leading push runs in the
# background.
#
# Scope: in-process (single backend). The state lives in module-level dicts, so
# two backend replicas would each fire their own leading push. That's the
# documented v1 limit; the ``submit`` seam is the swap point for a Redis-backed
# coordinator (POCKETPAW_REDIS_URL is already wired) when multi-replica push
# delivery lands. NO Beanie writes happen here — the emit callback routes
# through push/dispatch.py exactly as the direct path did.

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_ENV_SECONDS = "CLOUD_PUSH_COALESCE_SECONDS"
_DEFAULT_SECONDS = 5.0

# A key is (workspace_id, user_id, group_id). ``_tasks`` holds the open cooldown
# window for a key; ``_pending`` holds the latest suppressed emit awaiting the
# next flush. Both are cleared when a window closes.
_Key = tuple[str, str, str]
_Emit = Callable[[], Awaitable[None]]

_tasks: dict[_Key, asyncio.Task] = {}
_pending: dict[_Key, _Emit] = {}
# In-flight DETACHED leading-edge emits. Held only to keep a strong reference so
# the event loop can't garbage-collect a leading push mid-send; each removes
# itself on completion. See ``_spawn_detached`` / ``submit``.
_leading: set[asyncio.Task] = set()


async def _guarded_emit(emit: _Emit) -> None:
    """Await one emit, logging (not raising) on failure.

    The leading edge runs detached (no caller to catch its exception), so a
    failed send must be swallowed here or it would surface as an unretrieved
    task exception.
    """
    try:
        await emit()
    except Exception:
        logger.exception("push: leading-edge emit failed")


def _spawn_detached(emit: _Emit) -> None:
    """Fire one emit as a background task, decoupled from the caller.

    ``submit`` is awaited by the realtime bus handler, which the bus dispatches
    INLINE on the chat-send request path — awaiting delivery there would couple
    send latency to the Web Push fan-out. Running the leading emit detached
    (like the trailing flush already is) lets ``submit`` return promptly.
    """
    task = asyncio.create_task(_guarded_emit(emit))
    _leading.add(task)
    task.add_done_callback(_leading.discard)


def _coalesce_seconds() -> float:
    """Window length in seconds. ``<= 0`` (or unset-to-default) tunable via env.

    Malformed values fall back to the default rather than raising — a bad env
    value must not break notification delivery.
    """
    raw = os.environ.get(_ENV_SECONDS, "").strip()
    if not raw:
        return _DEFAULT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not a number; using default %s", _ENV_SECONDS, raw, _DEFAULT_SECONDS
        )
        return _DEFAULT_SECONDS


async def submit(key: _Key, emit: _Emit) -> None:
    """Route one notification through the leading-edge throttle.

    ``emit`` is an idempotent-safe coroutine that sends exactly one notification
    when awaited (it recomputes the unread count at send time, so a trailing
    flush carries a current count). It is called at most once on the leading
    edge and at most once per window thereafter.

    With coalescing disabled (window ``<= 0``) this is a pass-through: ``emit``
    is awaited immediately and no window is opened.
    """
    seconds = _coalesce_seconds()
    if seconds <= 0:
        await emit()
        return

    if key in _tasks:
        # Inside the cooldown window: suppress now, remember the freshest emit
        # to fire at the next flush.
        _pending[key] = emit
        return

    # Leading edge. Reserve the window SYNCHRONOUSLY (before scheduling) so a
    # second submit arriving while this leading emit is in flight coalesces
    # instead of racing to a second leading push. Fire the leading emit DETACHED
    # (not awaited) so this ``submit`` — dispatched inline by the realtime bus on
    # the chat-send request path — returns without blocking on the push fan-out.
    _tasks[key] = asyncio.create_task(_cooldown(key, seconds))
    _spawn_detached(emit)


async def _cooldown(key: _Key, seconds: float) -> None:
    """Hold a key's window open, flushing one coalesced emit per cycle.

    Sleeps ``seconds``; if a submit was suppressed meanwhile, fires it once and
    loops (so a sustained stream is capped at one send per window). An empty
    cycle closes the window and frees the key.
    """
    try:
        while True:
            await asyncio.sleep(seconds)
            emit = _pending.pop(key, None)
            if emit is None:
                return  # nothing suppressed this cycle → close the window
            try:
                await emit()
            except Exception:
                logger.exception("push: coalesced flush failed for key=%s", key)
    finally:
        _tasks.pop(key, None)


def reset() -> None:
    """Cancel all open windows and drop pending emits (best-effort, sync).

    Fire-and-forget: it requests cancellation but does not await the tasks, so a
    caller that needs the cancellations to finish cleanly on a live loop (tests,
    graceful shutdown) should use :func:`aclose` instead.
    """
    for task in (*_tasks.values(), *_leading):
        task.cancel()
    _tasks.clear()
    _pending.clear()
    _leading.clear()


async def aclose() -> None:
    """Cancel all open windows and AWAIT their teardown on the running loop.

    Awaiting the cancelled tasks lets each process ``CancelledError`` and run
    its ``finally`` before the loop moves on, so no cancellation callback lands
    on an already-closed loop.
    """
    tasks = [*_tasks.values(), *_leading]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _pending.clear()
    _leading.clear()


__all__ = ["aclose", "reset", "submit"]
