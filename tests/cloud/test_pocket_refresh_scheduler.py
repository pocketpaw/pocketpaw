# tests/cloud/test_pocket_refresh_scheduler.py — RFC 04 M3.
# Created: 2026-05-22 — Coverage for the pocket data-source interval-refresh
# scheduler and the auto-refresh cost controls.
#
# What this pins:
#   - An interval source is re-run when it is DUE (and not before).
#   - A sub-floor `refresh_interval_seconds` is CLAMPED to the configured
#     minimum — a hallucinated `1` never makes a source due every tick.
#   - The per-pocket auto-refresh budget caps an interval/webhook flood.
#   - One pocket erroring NEVER kills the scan pass — the loop survives it.
#   - The scheduler is gated off by default and start/stop are idempotent.
#
# No real network: the source executor is monkeypatched so the scheduler's
# scan-and-refresh logic is tested in isolation from outbound HTTP.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.pockets import (  # noqa: E402
    _refresh_budget,
    refresh_scheduler,
    source_executor,  # noqa: E402
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear the scheduler's last-run map + the auto-refresh budget log."""
    refresh_scheduler._reset_for_tests()
    _refresh_budget.reset_budget()
    yield
    refresh_scheduler._reset_for_tests()
    _refresh_budget.reset_budget()


def _interval_pocket(pocket_id="p1", interval_seconds=300, refresh=None):
    """A pocket row as `list_interval_source_pockets` would return it."""
    return {
        "pocket_id": pocket_id,
        "workspace_id": "ws-1",
        "sources": {
            "prs": {
                "method": "GET",
                "path": "/pulls",
                "bind": "state.prs",
                "refresh": refresh or ["interval"],
                "refresh_interval_seconds": interval_seconds,
            }
        },
    }


def _patch_executor(monkeypatch, calls: list, *, raises_for=None):
    """Patch `source_executor.run_sources` to record calls (no HTTP)."""

    async def _fake_run_sources(*, pocket_id, only_source=None, **_kw):
        if raises_for is not None and pocket_id == raises_for:
            raise RuntimeError(f"boom for {pocket_id}")
        calls.append((pocket_id, only_source))
        return {"ran": [{"source": only_source, "bind": "prs", "value": []}], "errors": []}

    monkeypatch.setattr(source_executor, "run_sources", _fake_run_sources)


def _patch_service(
    monkeypatch, pockets: list, *, creds=("https://api.example.com", "none", None, "", [], None)
):
    """Patch the pockets service calls the scheduler makes."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _list():
        return pockets

    async def _creds(_ws, _pid):
        return creds

    async def _spec(_ws, pid):
        for p in pockets:
            if p["pocket_id"] == pid:
                return {"sources": p["sources"]}
        return {}

    monkeypatch.setattr(pockets_service, "list_interval_source_pockets", _list)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _creds)
    monkeypatch.setattr(pockets_service, "get_pocket_ripple_spec", _spec)


# ---------------------------------------------------------------------------
# Interval source is re-run when due
# ---------------------------------------------------------------------------


async def test_interval_source_runs_on_first_pass(monkeypatch):
    """A never-run interval source is due immediately."""
    calls: list = []
    pockets = [_interval_pocket()]
    _patch_executor(monkeypatch, calls)
    _patch_service(monkeypatch, pockets)

    visited = await refresh_scheduler.run_one_pass()
    assert visited == 1
    assert calls == [("p1", "prs")]


async def test_interval_source_not_rerun_before_interval_elapses(monkeypatch):
    """A second pass right after the first does NOT re-run the source —
    its interval has not elapsed."""
    calls: list = []
    pockets = [_interval_pocket(interval_seconds=300)]
    _patch_executor(monkeypatch, calls)
    _patch_service(monkeypatch, pockets)

    await refresh_scheduler.run_one_pass()
    await refresh_scheduler.run_one_pass()
    # Only the first pass ran it — 300s has not passed between passes.
    assert calls == [("p1", "prs")]


async def test_interval_source_rerun_once_due(monkeypatch):
    """When monotonic time advances past the interval, the source re-runs."""
    import time as _time

    calls: list = []
    pockets = [_interval_pocket(interval_seconds=300)]
    _patch_executor(monkeypatch, calls)
    _patch_service(monkeypatch, pockets)

    await refresh_scheduler.run_one_pass()
    assert len(calls) == 1

    # Fast-forward monotonic time by 10 minutes — the source is now due.
    real_monotonic = _time.monotonic
    monkeypatch.setattr(refresh_scheduler.time, "monotonic", lambda: real_monotonic() + 600)
    await refresh_scheduler.run_one_pass()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Sub-floor interval is clamped
# ---------------------------------------------------------------------------


async def test_sub_floor_interval_is_clamped(monkeypatch):
    """A hallucinated `refresh_interval_seconds: 1` is clamped to the floor
    (default 60s) — it does NOT make the source due on every pass."""
    import time as _time

    calls: list = []
    # interval_seconds=1 — far below the 60s floor.
    pockets = [_interval_pocket(interval_seconds=1)]
    _patch_executor(monkeypatch, calls)
    _patch_service(monkeypatch, pockets)

    await refresh_scheduler.run_one_pass()
    assert len(calls) == 1

    # 30s later — past the authored `1`, but the clamp floored it to 60s,
    # so the source is NOT due yet.
    real_monotonic = _time.monotonic
    monkeypatch.setattr(refresh_scheduler.time, "monotonic", lambda: real_monotonic() + 30)
    await refresh_scheduler.run_one_pass()
    assert len(calls) == 1  # still not due — clamp held

    # 90s out — now past the 60s floor; the source re-runs.
    monkeypatch.setattr(refresh_scheduler.time, "monotonic", lambda: real_monotonic() + 90)
    await refresh_scheduler.run_one_pass()
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Per-pocket auto-refresh budget caps a flood
# ---------------------------------------------------------------------------


async def test_budget_caps_an_interval_flood(monkeypatch):
    """With the per-pocket budget set to 2, only 2 interval refreshes of a
    pocket go through — a flood of due passes beyond that is skipped."""
    import time as _time

    calls: list = []
    pockets = [_interval_pocket(interval_seconds=60)]
    _patch_executor(monkeypatch, calls)
    _patch_service(monkeypatch, pockets)

    # Pin the per-pocket auto-refresh budget to 2/hour.
    monkeypatch.setattr(_refresh_budget, "max_per_hour", lambda: 2)

    real_monotonic = _time.monotonic
    offset = {"v": 0.0}
    monkeypatch.setattr(refresh_scheduler.time, "monotonic", lambda: real_monotonic() + offset["v"])

    # 5 passes, each 60s+ apart so the source is due every time. The budget
    # only permits 2 — passes 3-5 are skipped.
    for i in range(5):
        offset["v"] = i * 120
        await refresh_scheduler.run_one_pass()

    assert len(calls) == 2


async def test_budget_is_separate_from_manual_run_log(monkeypatch):
    """The auto-refresh budget and the manual `source_executor._run_log`
    limiter are SEPARATE counters — spending one does not touch the other."""
    # Spend the whole auto-refresh budget for a pocket.
    monkeypatch.setattr(_refresh_budget, "max_per_hour", lambda: 1)
    assert await _refresh_budget.consume_auto_refresh("pocket-x") is True
    assert await _refresh_budget.consume_auto_refresh("pocket-x") is False

    # The manual run-log for the same pocket is untouched — a human can
    # still run sources manually.
    source_executor._run_log.clear()
    assert await source_executor._rate_limited("pocket-x", "human-user") is False


# ---------------------------------------------------------------------------
# One pocket erroring never kills the loop
# ---------------------------------------------------------------------------


async def test_one_pocket_error_does_not_abort_the_pass(monkeypatch):
    """A pocket whose refresh raises is logged and skipped — every OTHER
    pocket in the pass still refreshes."""
    calls: list = []
    pockets = [
        _interval_pocket(pocket_id="bad"),
        _interval_pocket(pocket_id="good-1"),
        _interval_pocket(pocket_id="good-2"),
    ]
    _patch_executor(monkeypatch, calls, raises_for="bad")
    _patch_service(monkeypatch, pockets)

    # The pass completes — it does not raise — and visits all 3 pockets.
    visited = await refresh_scheduler.run_one_pass()
    assert visited == 3
    # The two healthy pockets refreshed despite the bad one erroring.
    assert ("good-1", "prs") in calls
    assert ("good-2", "prs") in calls
    assert ("bad", "prs") not in calls


async def test_scan_failure_returns_zero_not_raises(monkeypatch):
    """If the pocket-scan helper itself fails, run_one_pass returns 0
    rather than propagating — the loop survives."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _boom():
        raise RuntimeError("mongo down")

    monkeypatch.setattr(pockets_service, "list_interval_source_pockets", _boom)
    assert await refresh_scheduler.run_one_pass() == 0


# ---------------------------------------------------------------------------
# Pocket with interval sources but no backend
# ---------------------------------------------------------------------------


async def test_interval_pocket_without_backend_is_skipped(monkeypatch):
    """A pocket with an interval source but no backend configured is
    skipped cleanly — no refresh call, no error."""
    calls: list = []
    pockets = [_interval_pocket()]
    _patch_executor(monkeypatch, calls)

    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _list():
        return pockets

    async def _no_creds(_ws, _pid):
        return None

    monkeypatch.setattr(pockets_service, "list_interval_source_pockets", _list)
    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _no_creds)

    visited = await refresh_scheduler.run_one_pass()
    assert visited == 1
    assert calls == []


# ---------------------------------------------------------------------------
# Gating + start/stop wiring
# ---------------------------------------------------------------------------


async def test_scheduler_disabled_by_default(monkeypatch):
    """Without the env flag, is_enabled is False and start is a no-op."""
    monkeypatch.delenv("POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED", raising=False)
    assert refresh_scheduler.is_enabled() is False
    await refresh_scheduler.start_scheduler()
    assert refresh_scheduler._task is None


async def test_scheduler_start_and_stop_idempotent(monkeypatch):
    """With the flag on, start spawns one task; double-start/stop are safe."""
    monkeypatch.setenv("POCKETPAW_POCKET_REFRESH_SCHEDULER_ENABLED", "true")
    try:
        await refresh_scheduler.start_scheduler()
        task = refresh_scheduler._task
        assert task is not None and not task.done()

        # Second start is a no-op — same task instance.
        await refresh_scheduler.start_scheduler()
        assert refresh_scheduler._task is task

        await refresh_scheduler.stop_scheduler()
        assert refresh_scheduler._task is None
        # Second stop is also a no-op.
        await refresh_scheduler.stop_scheduler()
    finally:
        await refresh_scheduler.stop_scheduler()
