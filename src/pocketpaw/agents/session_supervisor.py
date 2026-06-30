"""Session Supervisor — agent-session lifecycle across COLD/WARM tiers (SS-4).

Created 2026-06-30 (feat/session-supervisor SS-4): the policy / lifecycle engine
that owns one ``SessionRuntime`` per ``(workspace_id, session_id)`` and decides,
for each turn, whether to reuse a live warm slot (WARM) or take a fresh launch
(COLD/turn-1). It enforces a per-tenant warm quota, reaps idle warm sessions,
and NEVER touches a busy one — the same busy-guard + LRU idioms proven in
``pocketpaw.agents.pool`` (an instance with ``active_runs > 0`` is never evicted).

What this engine does NOT do: it does not spawn the OS subprocess. The executor
(SS-5) supplies the live warm "slot" plus a ``teardown`` callback via
``bind_warm_slot``; the supervisor owns COLD-vs-WARM routing, quota/reaping, and
— critically — turn-1 capture ownership: turn 1 of a supervised session ALWAYS
routes to its own fresh launch so the native ``cli_session_id`` the SDK captures
binds to THIS session, never a foreign warm client (the turn-1 capture concern
carried from SS-1). That keeps SS-4 fully unit-testable with fakes (a fake clock
injected via ``now=`` and fake ``teardown`` recorders) — no live model or real
subprocess required.

Tier model: COLD (no live slot — next acquire is a fresh launch, resuming from
the store via ``cli_session_id`` when one exists), WARM (a live slot bound, reused
within TTL), LIVE (a declared enum value reserved for a future always-on tier —
no behavior in v1).

Updated 2026-06-30 (fix/session-supervisor-saas-hardening SH-2): ``reap_once`` now
also PRUNES idle COLD runtimes (removes them from ``_runtimes`` past ``cold_ttl``)
so the runtime map stays bounded over long SaaS uptime. The durable
``cli_session_id`` lives in SS-3's Mongo map, so a later ``acquire`` re-creates a
pruned runtime COLD and re-resolves the id — the drop is safe. The busy-guard
still holds: a runtime with ``active_runs > 0`` (e.g. a turn-1 launch in flight,
which is COLD AND busy) is never pruned.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SessionTier(Enum):
    """Lifecycle tier of a supervised agent session.

    * ``COLD`` — no live warm slot. The next ``acquire`` is a FRESH launch; if a
      ``cli_session_id`` is known it resumes from the store, otherwise it is
      turn 1.
    * ``WARM`` — a live slot (process/client) is bound and reusable within TTL.
    * ``LIVE`` — RESERVED for a future always-on tier (e.g. a pinned, never-reaped
      session). Declared so callers can switch on it, but it carries NO behavior
      in v1 — nothing in this module ever puts a runtime into ``LIVE``.
    """

    COLD = "cold"
    WARM = "warm"
    LIVE = "live"


@dataclass
class SessionRuntime:
    """Per-``(workspace_id, session_id)`` descriptor the supervisor owns.

    Holds the session's identity, its current tier, the busy-counter, and the
    opaque warm slot + how to tear it down. The supervisor never interprets
    ``warm_slot`` — it is whatever the executor (SS-5) hands it (a process
    handle, a warm client, etc.); the supervisor only tracks its lifecycle.
    """

    workspace_id: str
    session_id: str
    agent_id: str
    cli_session_id: str | None = None
    project_key: str | None = None
    tier: SessionTier = SessionTier.COLD
    last_active: float = 0.0
    # In-flight ``run`` count. The reaper and the quota evictor NEVER touch a
    # runtime with ``active_runs > 0`` — tearing down a busy slot would abort
    # its live turn. ``last_active`` only ranks idle eviction candidates; this
    # counter is the authoritative "busy" signal (mirrors pool.AgentInstance).
    active_runs: int = 0
    # Opaque live warm slot supplied by the executor (SS-5) — never interpreted
    # here. Present only while ``tier is WARM``.
    warm_slot: Any | None = None
    # How to release ``warm_slot``. Called best-effort on reap / quota-evict /
    # crash / stop. May be sync or return an awaitable.
    teardown: Callable[[], Any] | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        """The supervisor key — tenancy boundary first."""
        return (self.workspace_id, self.session_id)


@dataclass(frozen=True)
class Acquisition:
    """The routing decision ``acquire`` returns for one turn.

    * ``warm_reuse`` — True when a live, unexpired warm slot exists and is being
      reused (the fast 2nd+ turn). False means a FRESH launch is required.
    * ``owns_capture`` — True on turn 1 (no ``cli_session_id`` known yet): this
      runtime OWNS the native-id capture, so the executor must route to its own
      fresh process and the id the SDK emits binds to THIS session.
    * ``cli_session_id`` — the resolved native session id (or ``None`` on turn 1)
      for the ``SessionHandle`` the executor builds.
    """

    runtime: SessionRuntime
    warm_reuse: bool
    owns_capture: bool
    cli_session_id: str | None

    @property
    def fresh(self) -> bool:
        """True when this turn needs a fresh launch (the inverse of warm reuse)."""
        return not self.warm_reuse

    @property
    def slot(self) -> Any | None:
        """Convenience: the warm slot to reuse (only meaningful when ``warm_reuse``)."""
        return self.runtime.warm_slot


class SessionSupervisor:
    """Owns agent-session lifecycle keyed by ``(workspace_id, session_id)``.

    A pure policy/lifecycle engine over ``SessionRuntime`` descriptors — it does
    not spawn subprocesses. The executor (SS-5) drives it: ``acquire`` per turn
    to learn COLD-vs-WARM + capture ownership, ``bind_warm_slot`` to register the
    live slot it launched, ``mark_run_start`` / ``mark_run_end`` around the turn,
    ``record_cli_session_id`` after turn-1 capture, and ``mark_crashed`` on
    failure. A background reaper (``start`` / ``stop``) releases idle warm slots
    past ``warm_ttl``.
    """

    def __init__(
        self,
        *,
        warm_ttl: float = 120,
        cold_ttl: float = 3600,
        max_warm_per_tenant: int = 8,
        max_warm_global: int = 64,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._warm_ttl = warm_ttl
        # How long a COLD runtime (no live slot) may sit idle in ``_runtimes``
        # before the reaper prunes the descriptor entirely. Much larger than
        # ``warm_ttl``: freeing the live slot is urgent, dropping the cheap
        # in-memory descriptor is just memory hygiene (resume rebuilds it).
        self._cold_ttl = cold_ttl
        self._max_warm_per_tenant = max_warm_per_tenant
        self._max_warm_global = max_warm_global
        # Injected clock — tests pass a fake closure so reap/TTL is deterministic.
        self._now: Callable[[], float] = now or time.monotonic
        self._runtimes: dict[tuple[str, str], SessionRuntime] = {}
        self._reaper_task: asyncio.Task | None = None
        # The reaper wakes at least as often as it would need to honor the TTL,
        # capped so a long TTL doesn't leave slots resident much past expiry.
        self._reap_interval: float = max(1.0, min(30.0, float(warm_ttl)))

    # -- lifecycle (background reaper) --------------------------------------

    async def start(self) -> None:
        """Start the idle-warm-slot reaper background task."""
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            logger.info(
                "SessionSupervisor started (warm_ttl=%ss, per_tenant=%d, global=%d)",
                self._warm_ttl,
                self._max_warm_per_tenant,
                self._max_warm_global,
            )

    async def stop(self) -> None:
        """Cancel the reaper and tear down every live warm slot."""
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        for runtime in list(self._runtimes.values()):
            await self._run_teardown_async(self._drop_slot(runtime))

    async def _reaper_loop(self) -> None:
        """Periodically reap idle warm slots past ``warm_ttl`` (mirrors _gc_loop)."""
        while True:
            await asyncio.sleep(self._reap_interval)
            try:
                self.reap_once()
            except Exception:
                logger.warning("SessionSupervisor reaper pass failed", exc_info=True)

    # -- acquire / routing ---------------------------------------------------

    def acquire(
        self,
        workspace_id: str,
        session_id: str,
        agent_id: str,
        *,
        cli_session_id: str | None = None,
        project_key: str | None = None,
    ) -> Acquisition:
        """Get-or-create the runtime for ``(workspace_id, session_id)`` and decide
        whether this turn is a WARM reuse or a FRESH launch.

        ``cli_session_id`` is what the caller recovered from the durable store
        (SS-3) for this key, or ``None`` on turn 1 / before any capture. When
        supplied it reconciles onto the in-memory runtime (the store is
        authoritative for resume); the resolved id is surfaced on the returned
        ``Acquisition`` for the ``SessionHandle`` the executor builds.

        FRESH (``warm_reuse=False``) when any of: no runtime yet, the runtime is
        COLD, its warm slot has expired past ``warm_ttl``, OR no ``cli_session_id``
        is known (turn 1 — and then ``owns_capture=True`` so the native id binds
        to THIS session rather than a foreign warm client). Otherwise WARM reuse.
        """
        key = (workspace_id, session_id)
        runtime = self._runtimes.get(key)
        if runtime is None:
            runtime = SessionRuntime(
                workspace_id=workspace_id,
                session_id=session_id,
                agent_id=agent_id,
                cli_session_id=cli_session_id,
                project_key=project_key,
                tier=SessionTier.COLD,
                last_active=self._now(),
            )
            self._runtimes[key] = runtime
        else:
            # Reconcile the durable id/key onto the live runtime.
            if cli_session_id is not None:
                runtime.cli_session_id = cli_session_id
            if project_key is not None:
                runtime.project_key = project_key
            runtime.agent_id = agent_id

        resolved_cli = runtime.cli_session_id

        # Evaluate warm liveness against the PRIOR last_active (before this turn
        # refreshes it) so an idle-expired slot is correctly seen as stale.
        warm_alive = (
            runtime.tier is SessionTier.WARM
            and runtime.warm_slot is not None
            and (self._now() - runtime.last_active) <= self._warm_ttl
        )
        runtime.last_active = self._now()

        # Turn 1 (no native id yet) ALWAYS takes its own fresh process so the
        # captured id binds to THIS session — never reuse a warm slot here even
        # if one somehow exists.
        owns_capture = resolved_cli is None
        warm_reuse = warm_alive and not owns_capture
        return Acquisition(
            runtime=runtime,
            warm_reuse=warm_reuse,
            owns_capture=owns_capture,
            cli_session_id=resolved_cli,
        )

    # -- executor hand-offs --------------------------------------------------

    def bind_warm_slot(
        self, runtime: SessionRuntime, slot: Any, teardown: Callable[[], Any] | None
    ) -> None:
        """Register the live warm slot the executor launched; move runtime → WARM.

        Tears down any stale slot already bound, refreshes ``last_active``, then
        enforces the per-tenant and global warm quotas (LRU-evicting the oldest
        IDLE warm runtime in scope when over the cap — never a busy one).
        """
        if runtime.warm_slot is not None and runtime.warm_slot is not slot:
            self._run_teardown(self._drop_slot(runtime))
        runtime.warm_slot = slot
        runtime.teardown = teardown
        runtime.tier = SessionTier.WARM
        runtime.last_active = self._now()
        self._enforce_quota(runtime)

    def mark_run_start(self, runtime: SessionRuntime) -> None:
        """Mark a turn as in-flight (busy-guard) and refresh activity."""
        runtime.active_runs += 1
        runtime.last_active = self._now()

    def mark_run_end(self, runtime: SessionRuntime) -> None:
        """Mark a turn finished; refresh activity for idle ranking."""
        runtime.active_runs = max(0, runtime.active_runs - 1)
        runtime.last_active = self._now()

    def record_cli_session_id(
        self, runtime: SessionRuntime, cli_session_id: str, project_key: str | None = None
    ) -> None:
        """Record the native session id captured on turn 1 onto the runtime.

        SS-5 ALSO persists it durably via the SS-3 service; this just keeps the
        in-memory runtime current so subsequent ``acquire`` calls resolve it.
        """
        runtime.cli_session_id = cli_session_id
        if project_key is not None:
            runtime.project_key = project_key

    def mark_crashed(self, runtime: SessionRuntime) -> None:
        """Demote a runtime to COLD after a slot failure; drop + tear down the slot.

        Keeps ``cli_session_id`` so the next ``acquire`` is FRESH but carries the
        prior id (the resume-from-store path).
        """
        self._run_teardown(self._drop_slot(runtime))

    # -- quota + reaping internals ------------------------------------------

    def _warm_runtimes(self, *, workspace_id: str | None = None) -> list[SessionRuntime]:
        """All runtimes holding a live warm slot, optionally scoped to a tenant."""
        return [
            r
            for r in self._runtimes.values()
            if r.tier is SessionTier.WARM
            and r.warm_slot is not None
            and (workspace_id is None or r.workspace_id == workspace_id)
        ]

    def _enforce_quota(self, protect: SessionRuntime) -> None:
        """Enforce per-tenant then global warm quotas, sparing ``protect``."""
        self._enforce_scope(
            limit=self._max_warm_per_tenant,
            protect=protect,
            workspace_id=protect.workspace_id,
            label="per-tenant",
        )
        self._enforce_scope(
            limit=self._max_warm_global,
            protect=protect,
            workspace_id=None,
            label="global",
        )

    def _enforce_scope(
        self,
        *,
        limit: int,
        protect: SessionRuntime,
        workspace_id: str | None,
        label: str,
    ) -> None:
        """LRU-evict the oldest IDLE warm runtime in scope until at/under ``limit``.

        Never evicts ``protect`` (the just-bound runtime) or any busy runtime
        (``active_runs > 0``). If the cap is exceeded but every candidate is busy,
        we accept being over quota this cycle (mirrors pool._evict_oldest).
        """
        while len(self._warm_runtimes(workspace_id=workspace_id)) > limit:
            idle = [
                r
                for r in self._warm_runtimes(workspace_id=workspace_id)
                if r.active_runs == 0 and r is not protect
            ]
            if not idle:
                logger.warning(
                    "SessionSupervisor %s warm quota exceeded but every candidate "
                    "is busy — skipping eviction this cycle",
                    label,
                )
                return
            victim = min(idle, key=lambda r: r.last_active)
            self._run_teardown(self._drop_slot(victim))
            logger.info(
                "SessionSupervisor evicted idle warm session %s (%s quota)",
                victim.key,
                label,
            )

    def reap_once(self) -> int:
        """Run one reaper pass: tear down idle WARM slots AND prune idle COLD runtimes.

        Two independent jobs, both gated by the busy-guard:

        * WARM reap — a runtime holding a live warm slot, idle past ``warm_ttl``,
          has its slot torn down (demoting it to COLD; the descriptor stays
          resident for a fast resume). Unchanged from v1.
        * COLD prune — a runtime with NO live slot (``tier is COLD``,
          ``warm_slot is None``), idle past ``cold_ttl``, is REMOVED from
          ``_runtimes`` entirely so the map stays bounded over long uptime. Safe
          because the durable ``cli_session_id`` lives in SS-3's Mongo map: a later
          ``acquire`` re-creates the runtime COLD and re-resolves the id.

        The busy-guard skips ANY runtime with ``active_runs > 0`` regardless of how
        stale ``last_active`` looks — a turn-1 fresh launch is COLD AND busy while
        in flight (v1 binds no warm slot), and pruning it mid-turn would drop live
        bookkeeping. WARM reaping stays governed by ``warm_ttl`` only; cold pruning
        by ``cold_ttl`` only.

        Returns the count of runtimes acted on (warm slots reaped + cold runtimes
        pruned). Exposed (not ``_``-private) so tests can drive a deterministic pass
        with the fake clock instead of waiting on the background loop.
        """
        now = self._now()
        reaped = 0
        pruned_keys: list[tuple[str, str]] = []
        for runtime in list(self._runtimes.values()):
            # Busy-guard — never touch an in-flight runtime (covers a COLD+busy
            # turn-1 launch as well as a busy warm slot).
            if runtime.active_runs > 0:
                continue
            idle = now - runtime.last_active
            if (
                runtime.tier is SessionTier.WARM
                and runtime.warm_slot is not None
                and idle > self._warm_ttl
            ):
                self._run_teardown(self._drop_slot(runtime))
                reaped += 1
            elif (
                runtime.tier is SessionTier.COLD
                and runtime.warm_slot is None
                and idle > self._cold_ttl
            ):
                # A just-reaped warm runtime (now COLD) is NOT pruned in the same
                # pass — the ``elif`` skips it; it stays resident until a later pass
                # finds it idle past ``cold_ttl``.
                pruned_keys.append(runtime.key)
        for key in pruned_keys:
            self._runtimes.pop(key, None)
        return reaped + len(pruned_keys)

    def _drop_slot(self, runtime: SessionRuntime) -> Callable[[], Any] | None:
        """Null the slot fields, demote to COLD, and return the old teardown."""
        teardown = runtime.teardown
        runtime.warm_slot = None
        runtime.teardown = None
        runtime.tier = SessionTier.COLD
        return teardown

    def _run_teardown(self, teardown: Callable[[], Any] | None) -> None:
        """Best-effort sync teardown; schedules an awaitable result on the loop.

        Sync teardowns (the common case, and what tests use) run inline. If a
        teardown returns a coroutine/awaitable and a loop is running, it is
        scheduled; with no running loop it is closed so it never warns. Never
        raises.
        """
        if teardown is None:
            return
        try:
            result = teardown()
        except Exception:
            logger.warning("SessionSupervisor warm-slot teardown raised", exc_info=True)
            return
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop().create_task(result)
            except RuntimeError:
                result.close()

    async def _run_teardown_async(self, teardown: Callable[[], Any] | None) -> None:
        """Await an async teardown to completion (used by ``stop``). Never raises."""
        if teardown is None:
            return
        try:
            result = teardown()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("SessionSupervisor warm-slot teardown raised", exc_info=True)


# Module-level singleton (mirrors pool.get_agent_pool).
_supervisor: SessionSupervisor | None = None


def get_session_supervisor() -> SessionSupervisor:
    """Get or create the global session supervisor."""
    global _supervisor
    if _supervisor is None:
        _supervisor = SessionSupervisor()
    return _supervisor
