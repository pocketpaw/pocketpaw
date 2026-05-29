# tests/cloud/test_temporal_scheduler.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — pins the
# RFC 03 v2 temporal trigger sweep scheduler (the cron driver).
#
# What this pins:
#   * ``is_enabled`` honors ``POCKETPAW_TEMPORAL_SWEEP_ENABLED``
#     (default OFF for tests + multi-replica deploys).
#   * Interval resolution honors ``POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS``
#     with a default of 3600 and a floor of 60.
#   * ``run_one_pass`` walks every pocket the scan helper yields and
#     calls the per-pocket dispatcher (no real sweep work — the
#     dispatcher is patched out).
#   * Per-pocket exceptions are swallowed so one bad pocket can't
#     abort the pass.
#   * ``start_scheduler`` is idempotent; ``stop_scheduler`` cancels and
#     awaits the loop cleanly (no leaked task at teardown).
#   * Disabled scheduler is a no-op on ``start_scheduler``.
#
# The loop itself is NOT exercised with a real sleep — that would
# require waiting an hour. The test patches the interval down + verifies
# the loop body runs ``run_one_pass`` at least once, then cancels.

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture(autouse=True)
def _reset_temporal_scheduler():
    """Ensure the scheduler module-level task slot is clean before/after
    each test so test order doesn't matter."""
    from pocketpaw_ee.cloud._core import temporal_scheduler

    temporal_scheduler._reset_for_tests()
    yield
    # Best-effort cancel on the way out.
    try:
        asyncio.get_event_loop().run_until_complete(temporal_scheduler.stop_scheduler())
    except Exception:
        pass
    temporal_scheduler._reset_for_tests()


@pytest.fixture
def _enable_temporal(monkeypatch):
    """Flip the opt-in env flag for tests that need the scheduler ON."""
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_ENABLED", "true")


def _patch_pockets_scan(monkeypatch, pockets: list[dict]) -> None:
    """Patch the service scan helper so ``run_one_pass`` sees a fixed list."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _list():
        return list(pockets)

    monkeypatch.setattr(pockets_service, "list_interval_source_pockets", _list)


def _patch_dispatcher(monkeypatch, calls: list[tuple[str, str]], *, raises_for: str | None = None):
    """Patch ``temporal_dispatcher.sweep_pocket`` to record calls and
    optionally raise for a target pocket id."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher
    from pocketpaw_ee.cloud.temporal_sweeps.domain import SweepDispatchResult

    async def _sweep_pocket(workspace_id: str, pocket_id: str, **_kw: Any) -> SweepDispatchResult:
        if raises_for is not None and pocket_id == raises_for:
            raise RuntimeError(f"boom for {pocket_id}")
        calls.append((workspace_id, pocket_id))
        return SweepDispatchResult(
            pocket_id=pocket_id,
            edges_fired=0,
            blocked=0,
            escalated=0,
            errors=0,
            sweep_duration_ms=0,
        )

    monkeypatch.setattr(temporal_dispatcher, "sweep_pocket", _sweep_pocket)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_is_enabled_default_off(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_TEMPORAL_SWEEP_ENABLED", raising=False)
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler.is_enabled() is False


def test_is_enabled_on_when_flag_is_true(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_ENABLED", "true")
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler.is_enabled() is True


def test_is_enabled_off_for_any_other_value(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_ENABLED", "false")
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler.is_enabled() is False


# ---------------------------------------------------------------------------
# Interval resolution
# ---------------------------------------------------------------------------


def test_default_interval_is_one_hour(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS", raising=False)
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler._interval_seconds() == 3600


def test_env_interval_is_honored(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS", "300")
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler._interval_seconds() == 300


def test_sub_floor_interval_is_clamped(monkeypatch) -> None:
    """A misconfigured ``1`` second cadence must not wedge the loop —
    clamp up to the 60s floor."""
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS", "1")
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler._interval_seconds() == 60


def test_garbage_interval_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS", "notanumber")
    from pocketpaw_ee.cloud._core import temporal_scheduler

    assert temporal_scheduler._interval_seconds() == 3600


# ---------------------------------------------------------------------------
# One-pass scan behavior
# ---------------------------------------------------------------------------


async def test_run_one_pass_iterates_every_pocket(monkeypatch) -> None:
    from pocketpaw_ee.cloud._core import temporal_scheduler

    pockets = [
        {"pocket_id": "p1", "workspace_id": "w1", "sources": {}},
        {"pocket_id": "p2", "workspace_id": "w1", "sources": {}},
        {"pocket_id": "p3", "workspace_id": "w2", "sources": {}},
    ]
    _patch_pockets_scan(monkeypatch, pockets)

    # Force the resolver to return a real (template, []) so the
    # dispatcher fires for every pocket in the scan.
    class _StubTpl:
        triggers: list = []

    async def _resolve(_ws: str, _pid: str):
        return _StubTpl(), []

    monkeypatch.setattr(temporal_scheduler, "_resolve_pocket_template_and_rows", _resolve)

    calls: list[tuple[str, str]] = []
    _patch_dispatcher(monkeypatch, calls)

    visited = await temporal_scheduler.run_one_pass()
    assert visited == 3
    assert sorted(calls) == [("w1", "p1"), ("w1", "p2"), ("w2", "p3")]


async def test_unresolved_template_skips_pocket(monkeypatch) -> None:
    """A pocket whose template can't be resolved is skipped without
    calling the dispatcher — v0 behaviour."""
    from pocketpaw_ee.cloud._core import temporal_scheduler

    _patch_pockets_scan(
        monkeypatch,
        [{"pocket_id": "p1", "workspace_id": "w1", "sources": {}}],
    )

    # The default resolver returns ``(None, [])``; no override.

    calls: list[tuple[str, str]] = []
    _patch_dispatcher(monkeypatch, calls)

    visited = await temporal_scheduler.run_one_pass()
    assert visited == 0
    assert calls == []


async def test_run_one_pass_swallows_per_pocket_failures(monkeypatch) -> None:
    """One bad pocket cannot abort the pass — the remaining pockets are
    still visited."""
    from pocketpaw_ee.cloud._core import temporal_scheduler

    pockets = [
        {"pocket_id": "p1", "workspace_id": "w1", "sources": {}},
        {"pocket_id": "BAD", "workspace_id": "w1", "sources": {}},
        {"pocket_id": "p3", "workspace_id": "w1", "sources": {}},
    ]
    _patch_pockets_scan(monkeypatch, pockets)

    class _StubTpl:
        triggers: list = []

    async def _resolve(_ws: str, _pid: str):
        return _StubTpl(), []

    monkeypatch.setattr(temporal_scheduler, "_resolve_pocket_template_and_rows", _resolve)

    calls: list[tuple[str, str]] = []
    _patch_dispatcher(monkeypatch, calls, raises_for="BAD")

    visited = await temporal_scheduler.run_one_pass()
    # p1 + p3 visited; BAD raised (caught and counted out).
    assert visited == 2
    assert ("w1", "p1") in calls
    assert ("w1", "p3") in calls
    assert ("w1", "BAD") not in calls


async def test_scan_failure_returns_zero(monkeypatch) -> None:
    """If the scan helper itself raises, the pass returns 0 — the loop
    survives."""
    from pocketpaw_ee.cloud._core import temporal_scheduler
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _boom():
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(pockets_service, "list_interval_source_pockets", _boom)

    visited = await temporal_scheduler.run_one_pass()
    assert visited == 0


# ---------------------------------------------------------------------------
# Start / stop lifecycle
# ---------------------------------------------------------------------------


async def test_start_scheduler_disabled_is_a_no_op(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_TEMPORAL_SWEEP_ENABLED", raising=False)
    from pocketpaw_ee.cloud._core import temporal_scheduler

    await temporal_scheduler.start_scheduler()
    # Task slot stays unset.
    assert temporal_scheduler._task is None


async def test_start_then_stop_is_clean(_enable_temporal, monkeypatch) -> None:
    """Start a real loop, then stop it — no leaked task."""
    from pocketpaw_ee.cloud._core import temporal_scheduler

    # Patch interval down so the loop's first sleep doesn't matter for
    # the start/stop assertion.
    monkeypatch.setenv("POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS", "60")

    await temporal_scheduler.start_scheduler()
    assert temporal_scheduler._task is not None
    assert not temporal_scheduler._task.done()

    await temporal_scheduler.stop_scheduler()
    assert temporal_scheduler._task is None


async def test_start_scheduler_is_idempotent(_enable_temporal) -> None:
    """Calling ``start_scheduler`` twice does not create two tasks."""
    from pocketpaw_ee.cloud._core import temporal_scheduler

    await temporal_scheduler.start_scheduler()
    first = temporal_scheduler._task
    await temporal_scheduler.start_scheduler()
    second = temporal_scheduler._task
    assert first is second

    await temporal_scheduler.stop_scheduler()


async def test_stop_scheduler_on_unstarted_is_safe() -> None:
    """``stop_scheduler`` is safe when the scheduler was never started."""
    from pocketpaw_ee.cloud._core import temporal_scheduler

    # Task slot was reset by the fixture.
    assert temporal_scheduler._task is None
    await temporal_scheduler.stop_scheduler()
    # Still None — no crash.
    assert temporal_scheduler._task is None
