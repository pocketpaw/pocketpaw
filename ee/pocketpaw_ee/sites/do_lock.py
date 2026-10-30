# ee/pocketpaw_ee/sites/do_lock.py: one lock per Worker script, so a deploy and a
# live settings PATCH (``durable_objects.set_platform_vars_live``) never interleave.
# The PATCH sends the script's whole binding list, so a deploy landing between its
# GET and PATCH would be overwritten.
#
# In-process an ``asyncio.Lock`` per script (per event loop). With the multi-worker
# switch on (``_core.lease.multi_worker_enabled``) the holder also takes the Redis
# lease ``sites-do-script:<script>`` (``_core.lease.Lease``: SET NX PX, renewed by a
# background task, compare-and-delete release), so other processes wait too. Waiting past
# ``wait_s`` raises ``sites.do_busy``: a publish says "try again", a sweep logs it and
# retries next run.
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pocketpaw_ee.cloud._core import lease as lease_mod
from pocketpaw_ee.cloud._core.errors import ValidationError

LEASE_NAME_PREFIX = "sites-do-script:"
DEFAULT_WAIT_S = 60.0
DEFAULT_TTL_MS = 60_000

_local: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _busy(script: str) -> ValidationError:
    return ValidationError(
        "sites.do_busy",
        f"Another change to this site's Worker ({script}) is in progress. Try again in a minute.",
    )


def _lock_for(script: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    held = _local.get(script)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _local[script] = held
    return held[1]


@asynccontextmanager
async def script_lock(
    script: str, *, wait_s: float = DEFAULT_WAIT_S, ttl_ms: int = DEFAULT_TTL_MS
) -> AsyncIterator[None]:
    """Hold ``script``'s lock for the block (see the module header)."""
    lock = _lock_for(script)
    try:
        await asyncio.wait_for(lock.acquire(), wait_s)
    except TimeoutError as exc:
        raise _busy(script) from exc
    try:
        if not lease_mod.multi_worker_enabled():
            yield
            return
        lease = lease_mod.Lease(LEASE_NAME_PREFIX + script, ttl_ms=ttl_ms)
        deadline = time.monotonic() + wait_s
        while not await lease.try_acquire():
            if time.monotonic() > deadline:
                raise _busy(script)
            await asyncio.sleep(0.1)

        async def _renew() -> None:
            while True:
                await asyncio.sleep(ttl_ms / 1000 / 3)
                await lease.try_acquire()

        renewer = asyncio.create_task(_renew())
        try:
            yield
        finally:
            renewer.cancel()
            # Never ``start``ed, so ``stop`` only releases the key (if still ours).
            await lease.stop()
    finally:
        lock.release()


__all__ = ["LEASE_NAME_PREFIX", "script_lock"]
