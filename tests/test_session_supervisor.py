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

from __future__ import annotations

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
