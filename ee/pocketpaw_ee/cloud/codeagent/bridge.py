# bridge.py — the delegate rendezvous ACROSS processes.
#
# Created 2026-08-07 (fix/code-delegate-cross-process).
#
# THE PROBLEM. ``delegates.PendingDelegates`` is in-process and single-loop by
# construction, and says so: "a resolve that lands on a different worker finds
# no entry and is rejected as unknown". In the deployed cloud stack that is not
# an edge case, it is EVERY delegate. deploy/coolify/docker-compose.yaml runs
# two containers off one image with ``POCKETPAW_CLOUD_RUN_EXECUTOR: arq``:
#
#     backend (web)   serves POST /codeagent/resolve
#     worker  (arq)   runs the chat turn, parks the 180s future
#
# The outbound leg already crosses that boundary — the worker XADDs SSE frames
# to ``run:{run_id}:events`` and the web process relays them (see
# runs/redis_stream.py). The RETURN leg had no such path: the browser posts its
# answer to the web process, whose registry is empty, so ``resolve_pending``
# raises NotFound and the worker's future runs out the full budget. Every Code
# Mode file tool therefore failed with "The browser did not finish the
# delegated task in 180s" while the browser had in fact finished it instantly.
#
# It does not reproduce on a single-process rig (``inprocess`` executor, which
# is the default and what local dev runs), which is why the in-process
# discovery fix that preceded this one tested clean and still failed in prod.
#
# WHY A LIST AND NOT PUB/SUB. Pub/sub drops a message with no subscriber
# attached, which would turn a fast browser into a lost answer and a full-budget
# park — the exact failure this module exists to prevent. A list is durable:
# ``deliver`` can RPUSH before or after ``listen`` starts and the value is still
# there, so there is no ordering race to reason about.
#
# WHY A SEPARATE PENDING KEY. ``resolve`` must keep answering honestly. The
# pending key is the shared existence record — written by the process that
# parks, read by the process that resolves — so an unknown id, an expired park
# and a wrong-tenant resolve all still produce NotFound rather than a silent
# success. It carries the workspace for the same reason ``_Pending`` does:
# tenancy that rests on an unguessable correlation id is not tenancy.
#
# The bridge is INERT without Redis (single-process dev, tests, CLI runs), and
# everything degrades to today's purely in-process behaviour.
from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Slack on the pending key's TTL, so the record outlives the park it describes.
# If the key expired first, a browser answering at the very edge of the budget
# would be told the id never existed — a confusing lie in place of a clean
# timeout.
_PENDING_TTL_SLACK_SECONDS = 30

# How long a delivered result waits to be collected before Redis reclaims it.
# Only reachable when the parked caller died between the deliver and the
# collect, so this is garbage collection rather than a functional window.
_RESULT_TTL_SECONDS = 60


def _pending_key(corr_id: str) -> str:
    return f"code_delegate:pending:{corr_id}"


def _result_key(corr_id: str) -> str:
    return f"code_delegate:result:{corr_id}"


class DelegateBridge(Protocol):
    """Cross-process half of the delegate rendezvous."""

    @property
    def enabled(self) -> bool:
        """False when there is no second process to reach (no Redis)."""
        ...

    async def announce(self, corr_id: str, workspace_id: str, *, ttl_seconds: int) -> None: ...

    async def listen(self, corr_id: str, *, timeout: float) -> dict[str, Any] | None:
        """Block until a result is delivered for ``corr_id``, or ``timeout``."""
        ...

    async def deliver(self, corr_id: str, workspace_id: str, result: dict) -> bool:
        """Hand a result to whichever process parked ``corr_id``.

        False when no park is on record for that id, or it belongs to another
        workspace — the caller turns that into the same NotFound an unknown id
        gets locally.
        """
        ...

    async def forget(self, corr_id: str) -> None: ...


class NullDelegateBridge:
    """No Redis, so no second process to reach. Every operation is a no-op and
    ``deliver`` always misses, which leaves the in-process registry as the only
    path — exactly the behaviour before this module existed."""

    @property
    def enabled(self) -> bool:
        return False

    async def announce(self, corr_id: str, workspace_id: str, *, ttl_seconds: int) -> None:
        return None

    async def listen(self, corr_id: str, *, timeout: float) -> dict[str, Any] | None:
        return None

    async def deliver(self, corr_id: str, workspace_id: str, result: dict) -> bool:
        return False

    async def forget(self, corr_id: str) -> None:
        return None


class RedisDelegateBridge:
    """Redis-backed rendezvous: a pending record plus a one-shot result list."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @property
    def enabled(self) -> bool:
        return True

    async def announce(self, corr_id: str, workspace_id: str, *, ttl_seconds: int) -> None:
        """Publish that this process is parked on ``corr_id``.

        Must happen BEFORE the delegate frame is pushed, for the same reason
        ``PendingDelegates.open`` is split from ``wait``: the browser can answer
        faster than the next await, and a resolve that arrives before the record
        exists would be rejected as unknown.
        """
        await self._redis.set(_pending_key(corr_id), workspace_id, ex=ttl_seconds)

    async def listen(self, corr_id: str, *, timeout: float) -> dict[str, Any] | None:
        # COST: a blocking pop holds one connection from the pool for as long as
        # the park lasts, so concurrent delegates cost concurrent connections
        # (bounded in practice by how many file tools one turn runs at once, and
        # redis-py's pool grows on demand). Polling would trade that for latency
        # on every call; blocking is the right side of that trade while the
        # delegate count per turn is small. Revisit if a turn ever fans out file
        # operations in parallel.
        #
        # BLPOP returns as soon as a value is there, including one pushed before
        # this call — the durability that makes the ordering race disappear.
        # Redis takes an integer timeout; round UP so the bridge never gives up
        # marginally before the caller's own budget.
        blocking_for = max(1, int(timeout + 0.999))
        popped = await self._redis.blpop([_result_key(corr_id)], timeout=blocking_for)
        if popped is None:
            return None
        _key, raw = popped
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("codeagent.bridge: undecodable result corr=%s", corr_id)
            return None
        return payload if isinstance(payload, dict) else None

    async def deliver(self, corr_id: str, workspace_id: str, result: dict) -> bool:
        owner = await self._redis.get(_pending_key(corr_id))
        if owner is None:
            return False
        if owner != workspace_id:
            # Same posture as PendingDelegates.resolve: a mismatch is a miss, so
            # the caller cannot learn that the id exists.
            logger.warning("codeagent.bridge deliver rejected: workspace mismatch corr=%s", corr_id)
            return False
        await self._redis.rpush(_result_key(corr_id), json.dumps(result))
        await self._redis.expire(_result_key(corr_id), _RESULT_TTL_SECONDS)
        return True

    async def forget(self, corr_id: str) -> None:
        await self._redis.delete(_pending_key(corr_id), _result_key(corr_id))


_bridge: DelegateBridge | None = None


def get_delegate_bridge() -> DelegateBridge:
    """Redis bridge when a URL is configured, otherwise inert.

    Mirrors ``runs.transport.get_stream_transport``'s auto-detection so the two
    halves of the same crossing are configured by one env var. No WARN on the
    null path: single-process is a legitimate deployment, and the loud warning
    already exists on the stream transport that would notice first.
    """
    global _bridge
    if _bridge is None:
        if os.environ.get("POCKETPAW_REDIS_URL", "").strip():
            from pocketpaw_ee.cloud._core.redis_client import get_redis

            _bridge = RedisDelegateBridge(get_redis())
        else:
            _bridge = NullDelegateBridge()
    return _bridge


def set_delegate_bridge(bridge: DelegateBridge | None) -> None:
    """Inject a bridge (tests, and any future explicit wiring)."""
    global _bridge
    _bridge = bridge


def _reset_for_tests() -> None:
    global _bridge
    _bridge = None


__all__ = [
    "DelegateBridge",
    "NullDelegateBridge",
    "RedisDelegateBridge",
    "get_delegate_bridge",
    "set_delegate_bridge",
]
