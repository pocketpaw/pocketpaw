# tests/test_session_supervisor.py
# Created: 2026-06-30 (feat/session-supervisor SS-4) — pins the SessionSupervisor
# policy/lifecycle engine WITHOUT a live model or real subprocess. A fake clock
# (injected via now=) makes TTL/reaping deterministic, and fake teardown
# recorders prove the supervisor releases slots at exactly the right moments.
#
# Coverage (the SS-4 done-when matrix):
#   * WARM reuse — re-acquire within TTL reuses the bound slot, no teardown.
#   * Idle reap — past warm_ttl, one reaper pass tears the idle slot down → COLD.
#   * Busy guard — a slot with active_runs>0 is never reaped, even when stale;
#     once the run ends and TTL passes it is.
#   * Per-tenant quota — a 3rd warm session for tenant A LRU-evicts A's oldest
#     IDLE warm; tenant B is untouched (quota is per-tenant).
#   * Turn-1 routing — acquire(cli_session_id=None) is FRESH + owns_capture; a
#     later acquire with a recorded id within TTL is WARM reuse.
#   * Crash → COLD — mark_crashed drops the slot but keeps cli_session_id, so the
#     next acquire is FRESH and resumes from the stored id.
#
# Updated 2026-06-30 (fix/session-supervisor-saas-hardening SH-2): adds the
# cold-prune coverage — reap_once now removes idle COLD runtimes (past cold_ttl,
# not busy) from _runtimes to bound memory, never prunes a COLD+busy turn-1 launch,
# keeps WARM governed by warm_ttl (not cold_ttl), and a pruned key re-creates COLD
# (and re-resolves its cli_session_id) on the next acquire — proving the drop safe.

from __future__ import annotations

import asyncio

from pocketpaw.agents.backend import LeasedClient
from pocketpaw.agents.session_supervisor import (
    Acquisition,
    SessionSupervisor,
    SessionTier,
    get_session_supervisor,
)


class FakeClock:
    """Deterministic monotonic clock — advance it explicitly in the test."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Teardown:
    """Fake teardown callback that records how many times it was called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class LeaseSpy:
    """WH-2 — a fake live ``ClaudeSDKClient`` plus the ASYNC teardown the Claude
    SDK backend hands the supervisor (mirrors ``claude_sdk._teardown``, which
    ``await client.disconnect()``s).

    ``disconnect_count`` increments only AFTER the disconnect coroutine runs to
    completion — so it proves the supervisor actually released the live client on
    a given path, not merely scheduled something. ``lease()`` wraps the spy in a
    real WH-1 ``LeasedClient`` so the supervisor holds the exact production type.
    """

    def __init__(self) -> None:
        self.disconnect_count = 0

    async def disconnect(self) -> None:
        # A realistic yield point, like a real subprocess disconnect — exercises
        # the "awaited to completion" path rather than a no-await fast finish.
        await asyncio.sleep(0)
        self.disconnect_count += 1

    async def teardown(self) -> None:
        """The async callback the supervisor stores as ``runtime.teardown``."""
        await self.disconnect()

    def lease(self, options_key: str = "opts-key") -> LeasedClient:
        return LeasedClient(client=self, options_key=options_key)


async def _drain() -> None:
    """Let fire-and-forget async-teardown tasks scheduled by ``_run_teardown``
    (the sync reap / quota-evict / crash callers) run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


# ===========================================================================
# WARM reuse
# ===========================================================================


def test_warm_reuse_within_ttl() -> None:
    """A bound warm slot is reused on the next acquire within TTL — no teardown."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    # Turn 1: fresh launch, owns capture.
    acq1 = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    assert acq1.fresh and acq1.owns_capture

    # Executor captures the native id and binds the warm slot it launched.
    sup.record_cli_session_id(acq1.runtime, "cli-123")
    td = Teardown()
    sup.bind_warm_slot(acq1.runtime, slot="warm-client", teardown=td)
    assert acq1.runtime.tier is SessionTier.WARM

    # Turn 2 within TTL → WARM reuse, slot reused, teardown NOT called.
    clock.advance(30)
    acq2 = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-123")
    assert acq2.warm_reuse is True
    assert acq2.owns_capture is False
    assert acq2.slot == "warm-client"
    assert acq2.cli_session_id == "cli-123"
    assert td.calls == 0


# ===========================================================================
# Idle reap
# ===========================================================================


def test_idle_warm_slot_reaped_past_ttl() -> None:
    """Past warm_ttl with no activity, one reaper pass tears the slot down → COLD."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    td = Teardown()
    sup.bind_warm_slot(acq.runtime, slot="slot", teardown=td)

    # Not yet expired — no reap.
    clock.advance(100)
    assert sup.reap_once() == 0
    assert td.calls == 0
    assert acq.runtime.tier is SessionTier.WARM

    # Past TTL — reaped.
    clock.advance(50)  # 150 > 120
    assert sup.reap_once() == 1
    assert td.calls == 1
    assert acq.runtime.tier is SessionTier.COLD
    assert acq.runtime.warm_slot is None


# ===========================================================================
# Busy guard
# ===========================================================================


def test_busy_runtime_never_reaped() -> None:
    """A busy slot (active_runs>0) is never reaped even when stale; idle+TTL is."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    td = Teardown()
    sup.bind_warm_slot(acq.runtime, slot="slot", teardown=td)

    # Run in flight — advance well past TTL, reaper must skip it.
    sup.mark_run_start(acq.runtime)
    clock.advance(500)
    assert sup.reap_once() == 0
    assert td.calls == 0
    assert acq.runtime.tier is SessionTier.WARM

    # Run ends; one more TTL window passes → now reapable.
    sup.mark_run_end(acq.runtime)
    assert acq.runtime.active_runs == 0
    clock.advance(121)
    assert sup.reap_once() == 1
    assert td.calls == 1
    assert acq.runtime.tier is SessionTier.COLD


# ===========================================================================
# Per-tenant quota
# ===========================================================================


def test_per_tenant_quota_lru_evicts_oldest_idle_in_tenant() -> None:
    """A 3rd warm session for tenant A LRU-evicts A's oldest IDLE warm; tenant B
    is untouched (quota is per-tenant)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=10_000, max_warm_per_tenant=2, now=clock)

    # Tenant B has a warm session — it must survive A hitting its cap.
    b = sup.acquire("ws-b", "sess-b1", "agent", cli_session_id="cli-b1")
    td_b = Teardown()
    sup.bind_warm_slot(b.runtime, slot="b1", teardown=td_b)

    # Tenant A: two warm sessions (a1 older than a2).
    clock.advance(1)
    a1 = sup.acquire("ws-a", "sess-a1", "agent", cli_session_id="cli-a1")
    td_a1 = Teardown()
    sup.bind_warm_slot(a1.runtime, slot="a1", teardown=td_a1)

    clock.advance(1)
    a2 = sup.acquire("ws-a", "sess-a2", "agent", cli_session_id="cli-a2")
    td_a2 = Teardown()
    sup.bind_warm_slot(a2.runtime, slot="a2", teardown=td_a2)

    # A 3rd warm session for A → over the per-tenant cap of 2 → evict A's oldest
    # IDLE warm (a1), tearing it down. a2 and the new one stay; B is untouched.
    clock.advance(1)
    a3 = sup.acquire("ws-a", "sess-a3", "agent", cli_session_id="cli-a3")
    td_a3 = Teardown()
    sup.bind_warm_slot(a3.runtime, slot="a3", teardown=td_a3)

    assert td_a1.calls == 1  # oldest idle in tenant A evicted
    assert a1.runtime.tier is SessionTier.COLD
    assert td_a2.calls == 0 and a2.runtime.tier is SessionTier.WARM
    assert td_a3.calls == 0 and a3.runtime.tier is SessionTier.WARM
    assert td_b.calls == 0 and b.runtime.tier is SessionTier.WARM  # per-tenant


def test_per_tenant_quota_skips_busy_candidate() -> None:
    """When the only over-cap candidate is busy, it is NOT evicted (busy-guard)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=10_000, max_warm_per_tenant=1, now=clock)

    a1 = sup.acquire("ws-a", "sess-a1", "agent", cli_session_id="cli-a1")
    td_a1 = Teardown()
    sup.bind_warm_slot(a1.runtime, slot="a1", teardown=td_a1)
    sup.mark_run_start(a1.runtime)  # busy — cannot be evicted

    clock.advance(1)
    a2 = sup.acquire("ws-a", "sess-a2", "agent", cli_session_id="cli-a2")
    td_a2 = Teardown()
    sup.bind_warm_slot(a2.runtime, slot="a2", teardown=td_a2)

    # Over cap (2 > 1) but the only idle candidate is a2 itself (protected as the
    # just-bound runtime); a1 is busy → nothing evicted this cycle.
    assert td_a1.calls == 0 and a1.runtime.tier is SessionTier.WARM
    assert td_a2.calls == 0 and a2.runtime.tier is SessionTier.WARM


def test_quota_cap_soak_no_unbounded_warm_clients() -> None:
    """Soak (WH-4): binding warm clients across tenants far beyond the caps never
    lets the held warm count exceed ``max_warm_per_tenant`` (per tenant) or
    ``max_warm_global`` (overall), and every evicted slot is disconnected. Proves
    WARM can't grow unbounded — no leaked ``claude`` processes under load."""
    clock = FakeClock()
    PER_TENANT, GLOBAL = 2, 4
    sup = SessionSupervisor(
        warm_ttl=10_000, max_warm_per_tenant=PER_TENANT, max_warm_global=GLOBAL, now=clock
    )

    tenants = ["ws-a", "ws-b", "ws-c"]
    acqs = []
    teardowns = []
    for i in range(12):  # 12 binds round-robin across 3 tenants, all idle
        clock.advance(1)
        acq = sup.acquire(tenants[i % 3], f"sess-{i}", "agent", cli_session_id=f"cli-{i}")
        td = Teardown()
        sup.bind_warm_slot(acq.runtime, slot=f"slot-{i}", teardown=td)
        acqs.append(acq)
        teardowns.append(td)

    warm = [
        a.runtime
        for a in acqs
        if a.runtime.tier is SessionTier.WARM and a.runtime.warm_slot is not None
    ]
    # Global cap holds — never more live warm clients than the global ceiling.
    assert len(warm) <= GLOBAL
    # Per-tenant cap holds for every tenant.
    for ws in tenants:
        assert len([r for r in warm if r.workspace_id == ws]) <= PER_TENANT
    # Every evicted slot's teardown (the ``disconnect``) fired exactly once — the
    # released clients can't pile up. evicted == binds - held.
    evicted = sum(td.calls for td in teardowns)
    assert evicted == 12 - len(warm)
    assert evicted > 0  # the soak actually exercised eviction


# ===========================================================================
# Turn-1 routing / capture ownership
# ===========================================================================


def test_turn_one_routes_fresh_and_owns_capture() -> None:
    """Turn 1 (cli_session_id=None) is a FRESH launch that OWNS capture; a later
    acquire with the recorded id within TTL is WARM reuse."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq1 = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    assert isinstance(acq1, Acquisition)
    assert acq1.warm_reuse is False
    assert acq1.fresh is True
    assert acq1.owns_capture is True
    assert acq1.cli_session_id is None

    # Executor captures the native id, records it, binds the slot it launched.
    sup.record_cli_session_id(acq1.runtime, "cli-xyz")
    sup.bind_warm_slot(acq1.runtime, slot="proc", teardown=Teardown())

    # Next turn within TTL — recorded id resolves, warm slot reused.
    clock.advance(10)
    acq2 = sup.acquire("ws-a", "sess-1", "agent-x")  # caller passes no id
    assert acq2.warm_reuse is True
    assert acq2.owns_capture is False
    assert acq2.cli_session_id == "cli-xyz"


def test_turn_one_never_reuses_foreign_warm_slot() -> None:
    """Even if a warm slot somehow exists, a turn with no known id stays FRESH so
    capture binds to THIS session (the SS-1 turn-1 concern)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    sup.bind_warm_slot(acq.runtime, slot="proc", teardown=Teardown())
    # Force the in-memory id back to unknown to simulate a not-yet-captured turn.
    acq.runtime.cli_session_id = None

    again = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    assert again.warm_reuse is False
    assert again.owns_capture is True


def test_expired_warm_slot_routes_fresh() -> None:
    """A warm slot idle past TTL routes FRESH on the next acquire (resume path)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    sup.bind_warm_slot(acq.runtime, slot="proc", teardown=Teardown())

    clock.advance(200)  # past TTL — slot stale, reaper hasn't run yet
    nxt = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    assert nxt.warm_reuse is False  # stale slot not reused
    assert nxt.owns_capture is False  # id known → resume, not turn 1
    assert nxt.cli_session_id == "cli-1"


# ===========================================================================
# Crash → COLD
# ===========================================================================


def test_crash_drops_slot_keeps_id_next_acquire_fresh() -> None:
    """mark_crashed → COLD + slot dropped; next acquire is FRESH and carries the
    prior cli_session_id (resume-from-store path)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    sup.record_cli_session_id(acq.runtime, "cli-survives")
    td = Teardown()
    sup.bind_warm_slot(acq.runtime, slot="proc", teardown=td)

    sup.mark_crashed(acq.runtime)
    assert td.calls == 1  # best-effort teardown of the dead slot
    assert acq.runtime.tier is SessionTier.COLD
    assert acq.runtime.warm_slot is None
    assert acq.runtime.cli_session_id == "cli-survives"  # kept for resume

    nxt = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-survives")
    assert nxt.warm_reuse is False  # FRESH launch
    assert nxt.owns_capture is False  # id known → resume from store
    assert nxt.cli_session_id == "cli-survives"


# ===========================================================================
# Cold-runtime pruning (SH-2 — bounded supervisor memory)
# ===========================================================================


def test_cold_runtime_pruned_past_cold_ttl() -> None:
    """A COLD runtime idle past cold_ttl is removed from _runtimes by a reaper pass."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, cold_ttl=3600, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    assert acq.runtime.tier is SessionTier.COLD  # no warm slot bound → COLD
    assert ("ws-a", "sess-1") in sup._runtimes

    # Idle but under cold_ttl → not pruned.
    clock.advance(3000)
    assert sup.reap_once() == 0
    assert ("ws-a", "sess-1") in sup._runtimes

    # Past cold_ttl → pruned out of the map entirely.
    clock.advance(601)  # 3601 > 3600
    assert sup.reap_once() == 1
    assert ("ws-a", "sess-1") not in sup._runtimes


def test_cold_busy_runtime_never_pruned() -> None:
    """A COLD runtime with active_runs>0 (a turn-1 launch in flight) is never pruned,
    even far past cold_ttl — the busy-guard wins."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, cold_ttl=3600, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)  # turn-1
    sup.mark_run_start(acq.runtime)  # COLD + busy while the turn is in flight
    assert acq.runtime.tier is SessionTier.COLD and acq.runtime.active_runs == 1

    clock.advance(10_000)  # way past cold_ttl
    assert sup.reap_once() == 0
    assert ("ws-a", "sess-1") in sup._runtimes  # survived — busy-guard held

    # Once the turn ends and cold_ttl passes, it becomes prunable.
    sup.mark_run_end(acq.runtime)
    clock.advance(3601)
    assert sup.reap_once() == 1
    assert ("ws-a", "sess-1") not in sup._runtimes


def test_warm_governed_by_warm_ttl_not_cold_ttl() -> None:
    """A WARM runtime is reaped on warm_ttl (not held to cold_ttl), and a fresh COLD
    runtime (idle < cold_ttl) survives the same pass."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, cold_ttl=3600, now=clock)

    warm = sup.acquire("ws-a", "warm-1", "agent", cli_session_id="cli-w")
    td = Teardown()
    sup.bind_warm_slot(warm.runtime, slot="s", teardown=td)

    fresh_cold = sup.acquire("ws-a", "cold-1", "agent", cli_session_id="cli-c")
    assert fresh_cold.runtime.tier is SessionTier.COLD

    # Past warm_ttl but well under cold_ttl: the warm slot is reaped; the fresh
    # COLD runtime is untouched (and the just-reaped warm stays resident as COLD).
    clock.advance(200)  # 200 > warm_ttl(120), < cold_ttl(3600)
    assert sup.reap_once() == 1
    assert td.calls == 1
    assert warm.runtime.tier is SessionTier.COLD
    assert ("ws-a", "warm-1") in sup._runtimes  # demoted, not yet cold-pruned
    assert ("ws-a", "cold-1") in sup._runtimes  # fresh cold survives


def test_acquire_after_cold_prune_recreates_and_resolves() -> None:
    """After a COLD prune, an acquire for that key re-creates the runtime COLD and
    re-resolves the cli_session_id from the (durable) store arg — the drop is safe."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, cold_ttl=3600, now=clock)

    sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    clock.advance(3601)
    assert sup.reap_once() == 1
    assert ("ws-a", "sess-1") not in sup._runtimes

    # Caller re-acquires (passing the id recovered from SS-3's Mongo map).
    again = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    assert ("ws-a", "sess-1") in sup._runtimes
    assert again.runtime.tier is SessionTier.COLD
    assert again.warm_reuse is False
    assert again.owns_capture is False  # id known → resume, not turn 1
    assert again.cli_session_id == "cli-1"


# ===========================================================================
# Identity helpers + singleton
# ===========================================================================


def test_runtime_key_is_tenancy_first() -> None:
    """SessionRuntime.key is (workspace_id, session_id) — tenancy boundary first."""
    sup = SessionSupervisor()
    acq = sup.acquire("ws-a", "sess-1", "agent-x")
    assert acq.runtime.key == ("ws-a", "sess-1")


def test_get_session_supervisor_singleton() -> None:
    """get_session_supervisor returns a stable process-global instance."""
    assert get_session_supervisor() is get_session_supervisor()


# ===========================================================================
# Background reaper lifecycle (async)
# ===========================================================================


async def test_start_stop_tears_down_all_slots() -> None:
    """stop() cancels the reaper and tears down every live warm slot."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)
    await sup.start()

    a = sup.acquire("ws-a", "sess-1", "agent", cli_session_id="cli-a")
    td_a = Teardown()
    sup.bind_warm_slot(a.runtime, slot="a", teardown=td_a)

    b = sup.acquire("ws-b", "sess-2", "agent", cli_session_id="cli-b")
    td_b = Teardown()
    sup.bind_warm_slot(b.runtime, slot="b", teardown=td_b)

    await sup.stop()
    assert td_a.calls == 1 and td_b.calls == 1
    assert a.runtime.tier is SessionTier.COLD
    assert b.runtime.tier is SessionTier.COLD


# ===========================================================================
# WH-2 — the WARM tier holds a real LeasedClient and DISCONNECTS the live
# claude client on every release path (idle reap, quota evict, crash, stop).
# Fakes only: a LeaseSpy whose async ``teardown`` mirrors claude_sdk._teardown.
# ===========================================================================


def test_warm_reuse_returns_leased_client_within_ttl() -> None:
    """A bound ``LeasedClient`` slot is returned via ``Acquisition.slot`` on a
    warm-reuse turn within TTL — and the live client is NOT disconnected."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq1 = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    sup.record_cli_session_id(acq1.runtime, "cli-1")

    spy = LeaseSpy()
    lease = spy.lease()
    sup.bind_warm_slot(acq1.runtime, slot=lease, teardown=spy.teardown)
    assert isinstance(acq1.runtime.warm_slot, LeasedClient)

    clock.advance(30)
    acq2 = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    assert acq2.warm_reuse is True
    assert acq2.slot is lease  # the exact LeasedClient is handed back for reuse
    assert acq2.slot.client is spy
    assert spy.disconnect_count == 0  # warm reuse never disconnects


async def test_idle_reap_disconnects_leased_client() -> None:
    """Past ``warm_ttl``, one reaper pass disconnects the live client (releases it)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    spy = LeaseSpy()
    sup.bind_warm_slot(acq.runtime, slot=spy.lease(), teardown=spy.teardown)

    clock.advance(150)  # 150 > 120
    assert sup.reap_once() == 1
    await _drain()  # let the scheduled async disconnect complete
    assert spy.disconnect_count == 1
    assert acq.runtime.tier is SessionTier.COLD
    assert acq.runtime.warm_slot is None


async def test_quota_evict_disconnects_evicted_client_and_spares_busy() -> None:
    """A per-tenant over-cap bind disconnects the evicted (oldest idle) client; a
    BUSY warm runtime is never disconnected."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=10_000, max_warm_per_tenant=2, now=clock)

    # a1: oldest, idle. a2: busy (must be spared). Both warm for tenant A.
    a1 = sup.acquire("ws-a", "sess-a1", "agent", cli_session_id="cli-a1")
    spy1 = LeaseSpy()
    sup.bind_warm_slot(a1.runtime, slot=spy1.lease(), teardown=spy1.teardown)

    clock.advance(1)
    a2 = sup.acquire("ws-a", "sess-a2", "agent", cli_session_id="cli-a2")
    spy2 = LeaseSpy()
    sup.bind_warm_slot(a2.runtime, slot=spy2.lease(), teardown=spy2.teardown)
    sup.mark_run_start(a2.runtime)  # busy — cannot be evicted

    # a3 → over the cap of 2. Only idle candidate is a1 (a2 busy, a3 protected).
    clock.advance(1)
    a3 = sup.acquire("ws-a", "sess-a3", "agent", cli_session_id="cli-a3")
    spy3 = LeaseSpy()
    sup.bind_warm_slot(a3.runtime, slot=spy3.lease(), teardown=spy3.teardown)
    await _drain()

    assert spy1.disconnect_count == 1  # oldest idle evicted → live client released
    assert a1.runtime.tier is SessionTier.COLD
    assert spy2.disconnect_count == 0  # busy runtime never torn down
    assert a2.runtime.tier is SessionTier.WARM
    assert spy3.disconnect_count == 0  # the just-bound runtime is protected
    assert a3.runtime.tier is SessionTier.WARM


async def test_crash_disconnects_leased_client_and_demotes_cold() -> None:
    """mark_crashed disconnects the live client and demotes the runtime to COLD,
    keeping ``cli_session_id`` for the resume-from-store path."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id=None)
    sup.record_cli_session_id(acq.runtime, "cli-survives")
    spy = LeaseSpy()
    sup.bind_warm_slot(acq.runtime, slot=spy.lease(), teardown=spy.teardown)

    sup.mark_crashed(acq.runtime)
    await _drain()
    assert spy.disconnect_count == 1
    assert acq.runtime.tier is SessionTier.COLD
    assert acq.runtime.warm_slot is None
    assert acq.runtime.cli_session_id == "cli-survives"


async def test_stop_awaits_every_leased_client_disconnect() -> None:
    """stop() awaits each slot's async disconnect to completion (no _drain needed —
    _run_teardown_async awaits inline)."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, now=clock)
    await sup.start()

    a = sup.acquire("ws-a", "sess-1", "agent", cli_session_id="cli-a")
    spy_a = LeaseSpy()
    sup.bind_warm_slot(a.runtime, slot=spy_a.lease(), teardown=spy_a.teardown)

    b = sup.acquire("ws-b", "sess-2", "agent", cli_session_id="cli-b")
    spy_b = LeaseSpy()
    sup.bind_warm_slot(b.runtime, slot=spy_b.lease(), teardown=spy_b.teardown)

    await sup.stop()
    assert spy_a.disconnect_count == 1
    assert spy_b.disconnect_count == 1
    assert a.runtime.tier is SessionTier.COLD
    assert b.runtime.tier is SessionTier.COLD


async def test_reaped_lease_then_cold_pruned_without_second_disconnect() -> None:
    """SS-4 COLD-prune still holds with a real lease: a warm-reaped runtime (now
    COLD, no slot) is pruned past ``cold_ttl`` with NO second disconnect; a busy
    runtime is never pruned."""
    clock = FakeClock()
    sup = SessionSupervisor(warm_ttl=120, cold_ttl=3600, now=clock)

    acq = sup.acquire("ws-a", "sess-1", "agent-x", cli_session_id="cli-1")
    spy = LeaseSpy()
    sup.bind_warm_slot(acq.runtime, slot=spy.lease(), teardown=spy.teardown)

    # Warm reap disconnects once and demotes to COLD (slot dropped).
    clock.advance(200)  # > warm_ttl, < cold_ttl
    assert sup.reap_once() == 1
    await _drain()
    assert spy.disconnect_count == 1
    assert acq.runtime.tier is SessionTier.COLD
    assert ("ws-a", "sess-1") in sup._runtimes  # resident as COLD, not yet pruned

    # A busy COLD runtime past cold_ttl is never pruned (busy-guard).
    busy = sup.acquire("ws-a", "sess-busy", "agent-x", cli_session_id=None)
    sup.mark_run_start(busy.runtime)

    # Past cold_ttl → the slotless COLD runtime is pruned; no extra disconnect.
    clock.advance(3601)
    assert sup.reap_once() == 1  # only sess-1 pruned; busy spared
    await _drain()
    assert spy.disconnect_count == 1  # NOT torn down a second time
    assert ("ws-a", "sess-1") not in sup._runtimes
    assert ("ws-a", "sess-busy") in sup._runtimes  # busy survives
