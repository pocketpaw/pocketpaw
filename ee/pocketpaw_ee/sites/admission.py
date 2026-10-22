# ee/pocketpaw_ee/sites/admission.py — admits site build / verify sandboxes against a
# memory budget BEFORE they ask Daytona, so concurrent builds wait for room instead of
# being refused by the org's total-memory cap and failing after the capacity retries.
#
# Daytona caps the org's total memory across running sandboxes. The sites lane runs
# ``build_worker._sites_max_jobs()`` jobs per worker times the replica count, each asking
# for 4 GiB (build) or 2 GiB (html verify), and workspace VMs (4 GiB, persistent) and
# Code Mode sandboxes draw from the same cap. Three pieces:
#
#   * ``memory_lease(gib)`` — async context manager held from before create until after
#     delete. OFF unless ``PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB`` is set: unset, empty or
#     invalid, the lease is a pass-through (no Redis call, no wait), because the limit
#     prod actually hits is not yet known to be memory. When set, blocks until ``gib``
#     fits that budget (set it to the org's memory cap). Strict FIFO, so a big request
#     is not starved by small ones; a request larger than the whole budget is admitted
#     once nothing else holds a lease.
#     Never raises for capacity: after ``MAX_WAIT_SECONDS`` in total (a Redis error part
#     way through hands the local fallback only what is left) it proceeds unleased and
#     the caller's create meets Daytona's own answer, so nothing waits forever.
#   * Backends. Redis when the worker called ``use_redis`` (``build_worker`` startup), so
#     the budget is shared across every worker and replica: one Lua script purges
#     expired leases, keeps a FIFO of waiters (each with a short heartbeat, so a dead
#     waiter cannot block the head) and grants. Each lease expires after
#     ``lease_ttl_seconds()`` (the reaper's orphan age: a crashed worker's box holds
#     real memory until the reaper deletes it, so its lease should too) and is refreshed
#     while held. A Redis error falls back to the process-local budget for that lease.
#     Without Redis (tests, scripts) the budget is per event loop. Keys share one hash
#     tag so the script also runs on Redis Cluster.
#   * ``free_capacity(client, need_gib=...)`` — OPT-IN (``PAW_SITES_EVICT_IDLE_WORKSPACE_VMS``,
#     default off), because stopping a VM can kill a dev server or test run an agent
#     left running. When on, on a capacity refusal it stops started ``paw-ws-*`` VMs
#     idle for at least the VM auto-stop (30 min), least recently active first, until
#     ``need_gib`` of their reported memory is freed. Idle is the later of Daytona's
#     ``last_activity_at`` and this process's last context resolve for the VM
#     (``context.last_resolved_at``); there is no cross-process last-tool-call record
#     and no cheap active-run check, so those are not consulted. Stop, never delete:
#     ``context._ensure_started`` resumes the VM on its next tool call. Build, verify
#     and Code Mode sandboxes are never touched. ``create_admitted`` wraps a create with
#     that one eviction and one retry, plus #2633's teardown-by-name. Every failed
#     create is logged at WARNING with Daytona's message (secrets redacted) and, for a
#     capacity refusal, which limit matched (``capacity.describe_refusal``).
#
# Telling a poller the build is waiting, not building, two ways:
#   * ``on_wait`` / ``on_admit`` (either backend): ``on_wait`` runs once when the lease
#     first has to wait, ``on_admit`` once that wait ends (granted, or given up and going
#     unleased) just before the caller creates. A lease granted at once runs neither.
#     The publish lane stamps its Site row and the project lane its build record with
#     them (``queued`` / ``waiting_for_capacity``, then ``building``). A hook that raises
#     is logged and never costs the lease.
#   * Under Redis, ``waiting:<tag>`` is set while queued (``wait_tag`` is the job's bounded
#     ``paw_job`` label) so ``build_job._preview_job_outcome`` can tell a poller of a
#     preview job ``queued`` / ``waiting_for_capacity`` rather than ``building``.
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.sites import capacity
from pocketpaw_ee.sites.sandbox_reaper import teardown_unreturned

logger = logging.getLogger(__name__)

BUDGET_ENV = "PAW_SITES_DAYTONA_MEMORY_BUDGET_GIB"

#: How long a lease may wait before the caller goes to Daytona unleased: the capacity
#: retry schedule's own window, so admission never waits longer than the retries would.
MAX_WAIT_SECONDS = float(sum(capacity.RETRY_DELAYS_SECONDS))

#: Opt-in switch for stopping idle workspace VMs on a capacity refusal.
EVICT_ENV = "PAW_SITES_EVICT_IDLE_WORKSPACE_VMS"
#: Floor on the idle time before a workspace VM may be stopped: its own auto-stop
#: (``store.WORKSPACE_VM_DEFAULT_AUTO_STOP_MINUTES``), so we never stop a VM sooner
#: than Daytona would.
IDLE_FOR_SECONDS = 30 * 60
WORKSPACE_VM_PREFIX = "paw-ws-"

# One hash tag, so every key the Lua script touches lands in one Redis Cluster slot.
_KEY_PREFIX = "{paw-sites-admission}"
_LEASES_KEY = f"{_KEY_PREFIX}:leases"  # zset lease id -> expiry ms
_GIB_KEY = f"{_KEY_PREFIX}:gib"  # hash lease id -> gib
_QUEUE_KEY = f"{_KEY_PREFIX}:queue"  # zset waiter id -> enqueue ms (FIFO)
_HEARTBEAT_KEY = f"{_KEY_PREFIX}:heartbeat"  # zset waiter id -> expiry ms


def _waiting_key(tag: str) -> str:
    # Concatenated, not ``str.format``: the hash tag's braces are not a format field.
    return f"{_KEY_PREFIX}:waiting:{tag}"


#: A waiter that stops polling for this long drops out of the queue.
_WAITER_TTL_MS = 30_000
_POLL_SECONDS = 1.0

_redis: Any = None

#: A wait hook: no arguments, awaited (see the header).
WaitHook = Callable[[], Awaitable[None]]


def eviction_enabled() -> bool:
    """True when :data:`EVICT_ENV` opts in to stopping idle workspace VMs."""
    return os.environ.get(EVICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def use_redis(client: Any) -> None:
    """Share the budget through ``client`` (None reverts to the process-local budget)."""
    global _redis
    _redis = client


def budget_gib() -> int | None:
    """The sites memory budget in GiB from :data:`BUDGET_ENV`, or ``None`` when admission
    is off (unset, empty, not an int, or not positive; the last two also warn)."""
    raw = os.environ.get(BUDGET_ENV, "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; admission is off", BUDGET_ENV, raw)
        return None
    if val <= 0:
        logger.warning("%s=%d is not positive; admission is off", BUDGET_ENV, val)
        return None
    return val


def lease_ttl_seconds() -> int:
    """Expiry of a Redis lease: the reaper's orphan age (longest job budget + margin)."""
    from pocketpaw_ee.sites.sandbox_reaper import default_max_age_seconds

    return default_max_age_seconds()


def _fits(used: int, holders: int, gib: int, budget: int | None) -> bool:
    return budget is None or holders == 0 or used + gib <= budget


# ---------------------------------------------------------------------------
# Process-local backend
# ---------------------------------------------------------------------------


class _LocalBudget:
    """FIFO memory budget for one event loop."""

    def __init__(self) -> None:
        self.used = 0
        self.holders = 0
        self.queue: deque[tuple[int, asyncio.Future[bool]]] = deque()

    def _grant(self, gib: int) -> None:
        self.used += gib
        self.holders += 1

    def release(self, gib: int) -> None:
        self.used = max(0, self.used - gib)
        self.holders = max(0, self.holders - 1)
        self._wake()

    def _wake(self) -> None:
        budget = budget_gib()
        while self.queue:
            gib, fut = self.queue[0]
            if fut.done():
                self.queue.popleft()
                continue
            if not _fits(self.used, self.holders, gib, budget):
                return
            self.queue.popleft()
            self._grant(gib)
            fut.set_result(True)

    async def acquire(self, gib: int, timeout: float, on_wait: WaitHook | None = None) -> bool:
        if not self.queue and _fits(self.used, self.holders, gib, budget_gib()):
            self._grant(gib)
            return True
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        entry = (gib, fut)
        self.queue.append(entry)
        try:
            if on_wait is not None:
                await on_wait()
            async with asyncio.timeout(timeout):
                return await fut
        except TimeoutError:
            # Granted in the same tick the timeout fired: keep it.
            return fut.done() and not fut.cancelled()
        except BaseException:
            if fut.done() and not fut.cancelled():
                self.release(gib)
            raise
        finally:
            if entry in self.queue:
                self.queue.remove(entry)
                self._wake()


_local: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LocalBudget] = (
    weakref.WeakKeyDictionary()
)


def _local_budget() -> _LocalBudget:
    loop = asyncio.get_running_loop()
    budget = _local.get(loop)
    if budget is None:
        budget = _local[loop] = _LocalBudget()
    return budget


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

# KEYS: leases, gib, queue, heartbeat. ARGV: id, gib, budget, lease ttl ms, waiter ttl ms.
# Returns 1 when granted, 0 to keep waiting. Server time, so replica clocks never matter.
_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local id, gib, budget = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3])
local lease_ttl, waiter_ttl = tonumber(ARGV[4]), tonumber(ARGV[5])
for _, m in ipairs(redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)) do
  redis.call('HDEL', KEYS[2], m)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
for _, m in ipairs(redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now)) do
  redis.call('ZREM', KEYS[3], m)
end
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now)
if not redis.call('ZSCORE', KEYS[3], id) then
  redis.call('ZADD', KEYS[3], now, id)
end
redis.call('ZADD', KEYS[4], now + waiter_ttl, id)
for i = 1, 4 do redis.call('PEXPIRE', KEYS[i], lease_ttl * 2) end
if redis.call('ZRANGE', KEYS[3], 0, 0)[1] ~= id then
  return 0
end
local used, holders = 0, 0
for _, v in ipairs(redis.call('HVALS', KEYS[2])) do
  used = used + tonumber(v)
  holders = holders + 1
end
if holders > 0 and used + gib > budget then
  return 0
end
redis.call('ZREM', KEYS[3], id)
redis.call('ZREM', KEYS[4], id)
redis.call('ZADD', KEYS[1], now + lease_ttl, id)
redis.call('HSET', KEYS[2], id, gib)
return 1
"""

# KEYS: leases. ARGV: id, lease ttl ms. Extends a held lease; 0 if it already expired.
_REFRESH_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
return redis.call('ZADD', KEYS[1], 'XX', 'CH', now + tonumber(ARGV[2]), ARGV[1])
"""


async def _redis_release(client: Any, lease_id: str, tag: str | None) -> None:
    try:
        pipe = client.pipeline(transaction=True)
        pipe.zrem(_LEASES_KEY, lease_id)
        pipe.hdel(_GIB_KEY, lease_id)
        pipe.zrem(_QUEUE_KEY, lease_id)
        pipe.zrem(_HEARTBEAT_KEY, lease_id)
        if tag:
            pipe.delete(_waiting_key(tag))
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 — the lease TTL is the backstop
        logger.warning("admission: release of %s failed (%s); it expires on its TTL", lease_id, exc)


async def _redis_acquire(
    client: Any,
    lease_id: str,
    gib: int,
    timeout: float,
    tag: str | None,
    on_wait: WaitHook | None = None,
) -> bool:
    """Poll the acquire script until granted (True) or ``timeout`` (False). Raises on a
    Redis error so the caller can fall back to the local budget."""
    ttl_ms = lease_ttl_seconds() * 1000
    deadline = time.monotonic() + timeout
    waiting = False
    try:
        while True:
            args = (lease_id, gib, budget_gib(), ttl_ms, _WAITER_TTL_MS)
            granted = await client.eval(
                _ACQUIRE_LUA, 4, _LEASES_KEY, _GIB_KEY, _QUEUE_KEY, _HEARTBEAT_KEY, *args
            )
            if int(granted) == 1:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Out of the queue, so a caller going unleased does not block the head.
                await _redis_release(client, lease_id, tag)
                return False
            if tag:
                await client.set(_waiting_key(tag), "1", px=_WAITER_TTL_MS)
            if not waiting:
                waiting = True
                logger.info("admission: %s GiB lease %s waiting for the budget", gib, lease_id)
                if on_wait is not None:
                    await on_wait()
            await asyncio.sleep(min(remaining, _POLL_SECONDS * random.uniform(0.75, 1.25)))
    except BaseException:
        await asyncio.shield(_redis_release(client, lease_id, tag))
        raise
    finally:
        if waiting and tag:
            try:
                await client.delete(_waiting_key(tag))
            except Exception:  # noqa: BLE001 — it expires on its own
                pass


async def _refresh_loop(client: Any, lease_id: str) -> None:
    ttl = lease_ttl_seconds()
    while True:
        await asyncio.sleep(max(1.0, ttl / 3))
        try:
            await client.eval(_REFRESH_LUA, 1, _LEASES_KEY, lease_id, ttl * 1000)
        except Exception as exc:  # noqa: BLE001 — try again next tick
            logger.info("admission: refresh of %s failed (%s)", lease_id, exc)


async def is_waiting(client: Any, tag: str) -> bool:
    """True while a lease tagged ``tag`` is queued for the budget. Never raises."""
    try:
        return bool(await client.exists(_waiting_key(tag)))
    except Exception:  # noqa: BLE001 — a fake pool or Redis down reads as "not waiting"
        return False


# ---------------------------------------------------------------------------
# Public lease
# ---------------------------------------------------------------------------


async def _run_hook(hook: WaitHook | None, name: str) -> None:
    """Await a wait hook; a raise is logged, never propagated (a status write that
    failed must not cost the build its lease)."""
    if hook is None:
        return
    try:
        await hook()
    except Exception:  # noqa: BLE001
        logger.warning("admission: %s hook failed", name, exc_info=True)


@asynccontextmanager
async def memory_lease(
    gib: int,
    *,
    wait_tag: str | None = None,
    max_wait_seconds: float | None = None,
    on_wait: WaitHook | None = None,
    on_admit: WaitHook | None = None,
) -> AsyncIterator[bool]:
    """Hold ``gib`` of the sites memory budget for the body. Yields whether the caller
    was admitted: True when a lease was granted or admission is off (no budget set, a
    pass-through), False only after the bounded wait ran out. Released on every exit
    path, cancellation included.

    ``on_wait`` runs once if the lease has to wait; ``on_admit`` runs once after such a
    wait ends, before the body. Neither runs for an immediate grant or a pass-through,
    and a hook that raises is logged and ignored."""
    if budget_gib() is None:
        yield True
        return
    waited = False

    async def _notify_wait() -> None:
        nonlocal waited
        if not waited:  # once, even when a Redis error moves the wait to the local budget
            waited = True
            await _run_hook(on_wait, "on_wait")

    gib = max(0, int(gib))
    timeout = MAX_WAIT_SECONDS if max_wait_seconds is None else max_wait_seconds
    deadline = time.monotonic() + timeout
    client = _redis
    lease_id = uuid.uuid4().hex
    backend: str | None = None
    refresher: asyncio.Task[None] | None = None

    if client is not None:
        try:
            if await _redis_acquire(client, lease_id, gib, timeout, wait_tag, _notify_wait):
                backend = "redis"
                refresher = asyncio.create_task(_refresh_loop(client, lease_id))
        except Exception as exc:  # noqa: BLE001 — Redis down: still admit within this process
            logger.warning("admission: Redis budget unavailable (%s); using the local one", exc)
            client = None
    # Only what is left of the wait: a Redis error part way through must not restart it,
    # or the total could exceed the one wait the job timeout allows for.
    remaining = max(0.0, deadline - time.monotonic())
    try:
        if client is None and await _local_budget().acquire(gib, remaining, _notify_wait):
            backend = "local"
        if backend is None:
            logger.warning(
                "admission: no %s GiB lease within %.0fs; going to Daytona unleased", gib, timeout
            )
        if waited:
            await _run_hook(on_admit, "on_admit")
        yield backend is not None
    finally:
        if refresher is not None:
            refresher.cancel()
        if backend == "redis":
            await asyncio.shield(_redis_release(client, lease_id, wait_tag))
        elif backend == "local":
            _local_budget().release(gib)


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def _memory_gib(info: Any) -> int:
    try:
        return max(0, int(getattr(info, "memory", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _last_active(info: Any, now: datetime) -> datetime | None:
    """The later of Daytona's ``last_activity_at`` and our own last resolve of the VM."""
    from pocketpaw_ee.cloud.daytona.context import last_resolved_at

    theirs = _parse_time(getattr(info, "last_activity_at", None))
    if theirs is None:
        return None  # unreadable: leave the VM alone
    ours = last_resolved_at(str(info.id), now=now)
    return max(theirs, ours) if ours is not None else theirs


async def free_capacity(
    client: Any,
    *,
    need_gib: int,
    idle_for_seconds: int = IDLE_FOR_SECONDS,
    now: datetime | None = None,
) -> list[str]:
    """Stop idle workspace VMs, least recently active first, until ``need_gib`` is freed.

    A no-op unless :func:`eviction_enabled`. Only started ``paw-ws-*`` boxes idle for at
    least ``max(idle_for_seconds, IDLE_FOR_SECONDS)`` (see :func:`_last_active`) with a
    known memory size; one whose activity or memory cannot be read is left alone. A
    failed stop is logged and the next candidate tried. Returns the stopped ids. Never
    raises."""
    if not eviction_enabled():
        return []
    idle_for = max(idle_for_seconds, IDLE_FOR_SECONDS)
    now = now or datetime.now(UTC)
    try:
        boxes = await client.list_sandboxes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("admission: cannot list sandboxes to free capacity (%s)", exc)
        return []
    candidates: list[tuple[datetime, Any]] = []
    for info in boxes:
        name = str(getattr(info, "name", "") or "")
        if not name.startswith(WORKSPACE_VM_PREFIX) or getattr(info, "state", "") != "started":
            continue
        active = _last_active(info, now)
        if active is None or (now - active).total_seconds() < idle_for:
            continue
        if _memory_gib(info) <= 0:
            continue  # unknown size: stopping it frees an unknown amount
        candidates.append((active, info))
    candidates.sort(key=lambda pair: pair[0])

    stopped: list[str] = []
    freed = 0
    for active, info in candidates:
        if freed >= need_gib:
            break
        try:
            await client.stop_sandbox(info.id)
        except Exception as exc:  # noqa: BLE001 — try the next one
            logger.warning("admission: stop of idle workspace VM %s failed (%s)", info.id, exc)
            continue
        freed += _memory_gib(info)
        stopped.append(str(info.id))
        logger.warning(
            "admission: stopped idle workspace VM %s (%s, idle since %s) to admit a build",
            info.name,
            info.id,
            active.isoformat(),
        )
    return stopped


def _log_create_failure(exc: BaseException, create_kwargs: dict[str, Any], *, attempt: str) -> None:
    """WARNING with Daytona's own message (redacted) and the limit it names, so prod logs
    show what is actually exceeded. A cancel is not a failure and is not logged."""
    if not isinstance(exc, Exception):
        return
    labels = create_kwargs.get("labels") or {}
    limit, message = capacity.describe_refusal(exc)
    logger.warning(
        "admission: sandbox %s failed (%s; limit=%s; asked cpu=%s memory=%sGiB disk=%sGiB; "
        "pocket=%s job=%s): %s: %s",
        attempt,
        create_kwargs.get("name"),
        limit or "not a capacity refusal",
        create_kwargs.get("cpu"),
        create_kwargs.get("memory"),
        create_kwargs.get("disk"),
        labels.get("paw_pocket"),
        labels.get("paw_job"),
        type(exc).__name__,
        message,
    )


async def create_admitted(client: Any, create_kwargs: dict[str, Any]) -> Any:
    """``client.create_sandbox(**create_kwargs)``, evicting idle workspace VMs once.

    Any failed create first deletes the box it may have registered (by name, shielded).
    A capacity refusal then runs :func:`free_capacity` for the request's memory and, if
    that stopped anything, retries the create once. Otherwise the original error
    propagates, so the caller's capacity path is unchanged."""
    name = create_kwargs["name"]
    try:
        return await client.create_sandbox(**create_kwargs)
    except BaseException as exc:
        await teardown_unreturned(client, name, create_kwargs.get("labels"))
        _log_create_failure(exc, create_kwargs, attempt="create")
        if not capacity.is_capacity_error(exc):
            raise
        refusal = exc
    stopped = await free_capacity(client, need_gib=int(create_kwargs.get("memory", 0) or 0))
    if not stopped:
        raise refusal
    try:
        return await client.create_sandbox(**create_kwargs)
    except BaseException as exc:
        await teardown_unreturned(client, name, create_kwargs.get("labels"))
        _log_create_failure(exc, create_kwargs, attempt="retry after eviction")
        raise


__all__ = [
    "BUDGET_ENV",
    "EVICT_ENV",
    "IDLE_FOR_SECONDS",
    "MAX_WAIT_SECONDS",
    "WORKSPACE_VM_PREFIX",
    "WaitHook",
    "budget_gib",
    "create_admitted",
    "eviction_enabled",
    "free_capacity",
    "is_waiting",
    "lease_ttl_seconds",
    "memory_lease",
    "use_redis",
]
