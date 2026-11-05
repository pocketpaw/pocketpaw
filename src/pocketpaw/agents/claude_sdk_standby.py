# src/pocketpaw/agents/claude_sdk_standby.py — pre-connected Claude CLI clients
# for one-shot isolated side completions (a site's AI API).
#
# Starting the Claude Code CLI (``ClaudeSDKClient.connect()``) costs seconds, and
# a side completion used to pay it on every call: a fresh ``side:<uuid>`` session
# key never matched the backend's one warm client, so each call evicted the chat
# turn's warm client and connected a new one (or ran a cold stateless query).
#
# ``SideStandbys`` keeps connected, NEVER-QUERIED clients per warm-client key (cwd,
# model, tools, prompt digest, tenant scope; see ``_client_cache_key``). A call
# takes one (no spawn on its critical path), sends its one conversation, and the
# client is then disconnected; a replacement is connected in the background.
#
# Invariants:
#   * A client serves exactly one conversation. It is never returned to the pool
#     after ``query()``, so no history, memory or context can carry from one site
#     visitor to the next. The CLI has no supported in-process reset, which is why
#     this is a standby pool and not a reuse pool.
#   * One pool per backend instance: a backend's env (provider key, BYOK
#     credentials) is not in the key, so standbys never cross backends.
#   * Bounded: ``per_key`` ready clients per key, ``max_keys`` keys per backend
#     (least recently used key evicted), and ``GLOBAL_MAX`` standbys per process.
#     A standby idle past ``idle_s`` is discarded instead of used.
#   * ``close()`` (from the backend's ``cleanup``) disconnects every standby, and
#     a refill that finishes after it is discarded, never kept.
#   * Disconnects run in the background; they never delay a caller.

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

PER_KEY = 1
MAX_KEYS = 3
IDLE_S = 300.0
# Each standby is a live CLI process (~150-250 MB); cap them per process.
GLOBAL_MAX = 4


class SideStandbys:
    """Connected, unused Claude CLI clients, keyed by warm-client key."""

    _global_count = 0  # standbys held across every pool in this process

    def __init__(
        self, *, per_key: int = PER_KEY, max_keys: int = MAX_KEYS, idle_s: float = IDLE_S
    ) -> None:
        self.per_key = per_key
        self.max_keys = max_keys
        self.idle_s = idle_s
        self._ready: OrderedDict[str, list[tuple[Any, float]]] = OrderedDict()
        self._filling: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ------------------------------------------------------------ take / refill

    def take(self, key: str) -> Any | None:
        """A connected, never-queried client for ``key``, or None (the caller
        connects one itself). Stale standbys found on the way are discarded."""
        entries = self._ready.get(key)
        now = time.monotonic()
        while entries:
            client, since = entries.pop(0)
            self._release_slot()
            if now - since < self.idle_s:
                self._ready.move_to_end(key)
                if not entries:
                    self._ready.pop(key, None)
                return client
            self.discard(client)
        self._ready.pop(key, None)
        return None

    def refill(self, key: str, connect: Callable[[], Awaitable[Any]]) -> None:
        """Connect a standby for ``key`` in the background, unless the key is
        full, already filling, or the process is at ``GLOBAL_MAX``."""
        if self._closed or key in self._filling:
            return
        if len(self._ready.get(key, ())) >= self.per_key:
            return
        if SideStandbys._global_count >= GLOBAL_MAX:
            self._evict_idle()
            if SideStandbys._global_count >= GLOBAL_MAX:
                return
        self._filling.add(key)
        self._spawn(self._fill(key, connect))

    async def _fill(self, key: str, connect: Callable[[], Awaitable[Any]]) -> None:
        try:
            client = await connect()
        except Exception as exc:  # noqa: BLE001 - a standby is an optimisation
            logger.info("side standby: connect failed (%s)", type(exc).__name__)
            return
        finally:
            self._filling.discard(key)
        if self._closed or SideStandbys._global_count >= GLOBAL_MAX:
            self.discard(client)
            return
        self._ready.setdefault(key, []).append((client, time.monotonic()))
        self._ready.move_to_end(key)
        SideStandbys._global_count += 1
        while len(self._ready) > self.max_keys:
            _, evicted = self._ready.popitem(last=False)
            for old, _since in evicted:
                self._release_slot()
                self.discard(old)

    # ------------------------------------------------------------ teardown

    def discard(self, client: Any) -> None:
        """Disconnect ``client`` in the background."""
        if client is None:
            return
        self._spawn(_disconnect(client))

    async def close(self) -> None:
        """Disconnect every standby; later refills are discarded on arrival."""
        self._closed = True
        ready, self._ready = self._ready, OrderedDict()
        for entries in ready.values():
            for client, _since in entries:
                self._release_slot()
                await _disconnect(client)

    def _evict_idle(self) -> None:
        now = time.monotonic()
        for key in list(self._ready):
            kept = []
            for client, since in self._ready[key]:
                if now - since > self.idle_s:
                    self._release_slot()
                    self.discard(client)
                else:
                    kept.append((client, since))
            if kept:
                self._ready[key] = kept
            else:
                self._ready.pop(key, None)

    @staticmethod
    def _release_slot() -> None:
        SideStandbys._global_count = max(0, SideStandbys._global_count - 1)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        except RuntimeError:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def ready_count(self, key: str | None = None) -> int:
        if key is not None:
            return len(self._ready.get(key, ()))
        return sum(len(v) for v in self._ready.values())

    async def settle(self) -> None:
        """Wait for in-flight refills and disconnects (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def _disconnect(client: Any) -> None:
    try:
        await client.disconnect()
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.debug("side standby: disconnect failed: %s", exc)


__all__ = ["GLOBAL_MAX", "IDLE_S", "MAX_KEYS", "PER_KEY", "SideStandbys"]
