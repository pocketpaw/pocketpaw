# tests/cloud/test_belt_scheduler.py — the Belt MANDATE cadence scheduler + the
# first LIVE-signal patrol (feat/patrol-engine).
#
# Created: 2026-06-13.
# Updated: 2026-06-13 (PR #1463 review) — added a shutdown-during-immediate-tick
#   regression test (the loop must not hang), an exact-7-day cadence boundary
#   test, and a user_id-threading assertion on the issues patrol.
#
# Two pieces under test:
#
#   PIECE 1 — the CADENCE SCHEDULER. A single sweeper loop ticks every interval
#     and fires ``service.trigger_shift`` for each ACTIVE mandate whose charter
#     cadence is DUE (a "weekly" mandate is due when its last shift was >= 7 days
#     ago, or it has never had a shift). A "manual" mandate is NEVER fired by the
#     scheduler. The clock is injectable (a ``now()`` callable) so due-ness is
#     deterministic in tests — no wall-clock sleeps. ``trigger_shift`` is invoked
#     through an injectable callable so the tick can be proven WITHOUT a real LLM.
#     Lifecycle mirrors autopilot: a startup reconciler starts the loop, a
#     shutdown drain cancels + awaits it.
#
#   PIECE 2 — the ISSUES patrol (the FIRST live patrol). It reads a REAL signal —
#     open issues from the bound repo's GitLab project — via the EXISTING
#     ``connectors_service.execute`` cloud path, and emits a populated
#     ``SightingDraft`` per issue (NOT the hardcoded deps stub). The connector
#     call is injectable so tests mock the payload instead of hitting the network.
#
# All tests use a deterministic clock / injected trigger / mocked connector —
# none call a real LLM or the network.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.mandates import patrols as patrols_mod  # noqa: E402
from pocketpaw_ee.cloud.mandates import scheduler as scheduler_mod  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402

WS = "w1"
USER = "u1"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _drain_scheduler_task():
    """Cancel + AWAIT any scheduler loop a test left running before clearing the
    registry, so a leaked loop never bleeds into the next test."""
    yield
    await scheduler_mod.shutdown_scheduler()


def _charter(cadence: str = "weekly", budget: int = 3) -> dict:
    return {
        "goal": "keep the surface healthy",
        "kpis": [{"name": "open_issues", "target": 0, "direction": "down"}],
        "says_no": ["major version bumps"],
        "boundaries": ["never touch auth code"],
        "budget": {"max_tasks_per_shift": budget, "gate_minutes_per_week": 15},
        "cadence": cadence,
    }


async def _make_mandate(name: str, *, cadence: str = "weekly", repo_id: str = "/tmp/x") -> str:
    created = await mandate_service.create_mandate(
        WS,
        USER,
        {"name": name, "surface": {"repo_id": repo_id}, "charter": _charter(cadence=cadence)},
    )
    return created["mandate"]["id"]


# ---------------------------------------------------------------------------
# PIECE 1 — list_cadence_due: a weekly mandate with no shift is due; a manual
# mandate is never due; a weekly mandate whose last shift is recent is not due.
# ---------------------------------------------------------------------------


async def test_list_cadence_due_weekly_never_shifted_is_due(tmp_path, mongo_db):
    weekly = await _make_mandate("weekly-fresh", cadence="weekly")
    manual = await _make_mandate("manual", cadence="manual")

    now = datetime(2026, 6, 13, tzinfo=UTC)
    due = await mandate_service.list_cadence_due(now)
    due_ids = {row["mandate_id"] for row in due}

    assert weekly in due_ids, "a weekly mandate that has never shifted is due"
    assert manual not in due_ids, "a manual mandate is never scheduled"


async def test_list_cadence_due_recent_shift_not_due(tmp_path, mongo_db):
    from pocketpaw_ee.cloud.mandates.domain import ShiftDoc

    weekly = await _make_mandate("weekly-recent", cadence="weekly")

    now = datetime(2026, 6, 13, tzinfo=UTC)
    # A shift two days ago — inside the weekly window, so NOT due.
    shift = ShiftDoc(workspace=WS, mandate_id=weekly, no=1, state="done")
    shift.createdAt = now - timedelta(days=2)
    await shift.insert()

    due_ids = {row["mandate_id"] for row in await mandate_service.list_cadence_due(now)}
    assert weekly not in due_ids, "a weekly mandate shifted 2 days ago is not yet due"

    # ...but eight days later the same mandate IS due again.
    later = now + timedelta(days=8)
    due_ids_later = {row["mandate_id"] for row in await mandate_service.list_cadence_due(later)}
    assert weekly in due_ids_later, "a weekly mandate becomes due once a week has elapsed"


async def test_list_cadence_due_exact_7_day_boundary_is_due(tmp_path, mongo_db):
    """The window is EXCLUSIVE: a shift EXACTLY 7 days ago is due (a mandate must
    not slip a beat at the boundary), and a shift a hair under 7 days is not."""
    from pocketpaw_ee.cloud.mandates.domain import ShiftDoc

    weekly = await _make_mandate("weekly-boundary", cadence="weekly")
    now = datetime(2026, 6, 13, tzinfo=UTC)

    shift = ShiftDoc(workspace=WS, mandate_id=weekly, no=1, state="done")
    shift.createdAt = now - timedelta(days=7)  # exactly the interval ago
    await shift.insert()

    due_ids = {row["mandate_id"] for row in await mandate_service.list_cadence_due(now)}
    assert weekly in due_ids, "a shift exactly 7 days ago is DUE (exclusive boundary)"

    # A minute short of 7 days is still inside the window → NOT due.
    just_short = now - timedelta(minutes=1)
    due_short = {row["mandate_id"] for row in await mandate_service.list_cadence_due(just_short)}
    assert weekly not in due_short, "a hair under 7 days is not yet due"


async def test_list_cadence_due_excludes_paused(tmp_path, mongo_db):
    from bson import ObjectId
    from pocketpaw_ee.cloud.mandates.domain import MandateDoc

    weekly = await _make_mandate("weekly-paused", cadence="weekly")
    doc = await MandateDoc.find_one(MandateDoc.workspace == WS, MandateDoc.id == ObjectId(weekly))
    doc.status = "paused"
    await doc.save()

    now = datetime(2026, 6, 13, tzinfo=UTC)
    due_ids = {row["mandate_id"] for row in await mandate_service.list_cadence_due(now)}
    assert weekly not in due_ids, "a paused mandate is inert — never scheduled"


# ---------------------------------------------------------------------------
# PIECE 1 — the scheduler TICK fires trigger_shift for the due mandate and NOT
# for the manual one, using a deterministic clock + an injected trigger (no LLM).
# ---------------------------------------------------------------------------


async def test_scheduler_tick_fires_only_due_mandates(tmp_path, mongo_db):
    weekly = await _make_mandate("weekly-due", cadence="weekly")
    manual = await _make_mandate("manual-never", cadence="manual")

    fired: list[str] = []

    async def _fake_trigger(workspace_id: str, user_id: str, mandate_id: str) -> dict:
        fired.append(mandate_id)
        return {}

    now = datetime(2026, 6, 13, tzinfo=UTC)
    fired_ids = await scheduler_mod.run_scheduler_tick(now=lambda: now, trigger=_fake_trigger)

    assert weekly in fired, "the due weekly mandate's shift must fire"
    assert manual not in fired, "the manual mandate's shift must NOT fire on a tick"
    assert fired_ids == fired == [weekly]


async def test_scheduler_tick_swallows_trigger_failure(tmp_path, mongo_db):
    """A failing trigger_shift on one mandate must not sink the whole tick — the
    scheduler swallows it and keeps sweeping (always-on perception must not crash
    the app)."""
    boom = await _make_mandate("weekly-boom", cadence="weekly")
    ok = await _make_mandate("weekly-ok", cadence="weekly")

    fired_ok: list[str] = []

    async def _trigger(workspace_id: str, user_id: str, mandate_id: str) -> dict:
        if mandate_id == boom:
            raise RuntimeError("foreman exploded")
        fired_ok.append(mandate_id)
        return {}

    now = datetime(2026, 6, 13, tzinfo=UTC)
    # Must not raise even though one mandate's trigger blew up.
    fired_ids = await scheduler_mod.run_scheduler_tick(now=lambda: now, trigger=_trigger)
    assert ok in fired_ok, "the healthy mandate still fires after a sibling's failure"
    # The boom mandate counts as attempted-but-failed → not in the fired total.
    assert boom not in fired_ok
    assert boom not in fired_ids and ok in fired_ids


# ---------------------------------------------------------------------------
# PIECE 1 — lifecycle: startup reconciler starts the loop, shutdown drains it
# (mirrors the autopilot reconciler/drain tests).
# ---------------------------------------------------------------------------


async def test_scheduler_lifecycle_start_and_drain(tmp_path, mongo_db):
    assert not scheduler_mod.is_running()

    # A huge interval keeps the loop alive on its sleep so we can observe it.
    await scheduler_mod.start_scheduler(interval_seconds=10_000, run_immediate=False)
    assert scheduler_mod.is_running()

    await scheduler_mod.shutdown_scheduler()
    assert not scheduler_mod.is_running()
    # Idempotent — a second drain is a no-op.
    await scheduler_mod.shutdown_scheduler()


async def test_shutdown_during_immediate_tick_does_not_hang(tmp_path, mongo_db, monkeypatch):
    """REGRESSION: a cancel WHILE the run_immediate tick is in flight must abort
    the loop promptly, NOT fall through into the hour-long ``asyncio.sleep``.
    Before the fix, ``contextlib.suppress(CancelledError)`` ate the cancel and
    shutdown blocked until the next interval (up to 3600s)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_tick(*args, **kwargs):
        started.set()
        await release.wait()  # park here until the test cancels us
        return []

    monkeypatch.setattr(scheduler_mod, "run_scheduler_tick", _slow_tick)

    # Huge interval: if the cancel were swallowed, the loop would park on this
    # sleep and the drain below would time out.
    await scheduler_mod.start_scheduler(interval_seconds=10_000, run_immediate=True)
    await asyncio.wait_for(started.wait(), timeout=1.0)  # the immediate tick is running

    # Drain must complete fast — the cancel propagates out of the immediate tick.
    await asyncio.wait_for(scheduler_mod.shutdown_scheduler(), timeout=1.0)
    assert not scheduler_mod.is_running()


async def test_scheduler_reconciler_starts_loop(tmp_path, mongo_db):
    """The lifespan startup reconciler starts exactly one sweeper loop."""
    assert not scheduler_mod.is_running()
    started = await scheduler_mod.reconcile_scheduler(interval_seconds=10_000)
    assert started == 1
    assert scheduler_mod.is_running()

    # A second reconcile is idempotent — it does not stack a second loop.
    started_again = await scheduler_mod.reconcile_scheduler(interval_seconds=10_000)
    assert started_again == 0
    assert scheduler_mod.is_running()

    await scheduler_mod.shutdown_scheduler()
    assert not scheduler_mod.is_running()


# ---------------------------------------------------------------------------
# PIECE 2 — the ISSUES patrol (FIRST live patrol) reads a real connector signal
# and produces a POPULATED SightingDraft per open issue (not the hardcoded stub).
# ---------------------------------------------------------------------------


async def test_issues_patrol_produces_sightings_from_connector_payload():
    """Given a mocked connector-execute returning a GitLab-shaped issues payload,
    the issues patrol produces a populated SightingDraft per open issue — proving
    the patrol reads a LIVE signal path, not a hardcoded table."""

    from types import SimpleNamespace

    seen_user: list[str | None] = []

    async def _fake_execute(workspace_id, name, body, *, user_id=None):
        # Mirror the ExecuteActionResponse shape: success + a data payload.
        seen_user.append(user_id)
        return SimpleNamespace(
            success=True,
            data=[
                {
                    "iid": 42,
                    "title": "Login button is broken",
                    "labels": ["bug"],
                    "state": "opened",
                },
                {"iid": 43, "title": "Slow dashboard load", "labels": [], "state": "opened"},
                {"iid": 7, "title": "Old closed thing", "state": "closed"},
            ],
        )

    drafts = await patrols_mod.issues_patrol(
        "/tmp/repo",
        workspace_id=WS,
        user_id="system:scheduler",
        project="my-group/my-project",
        execute=_fake_execute,
    )

    assert seen_user == ["system:scheduler"], "the actor is threaded to the connector"
    assert len(drafts) == 2, "only OPEN issues become sightings"
    summaries = [d["summary"] for d in drafts]
    assert any("Login button is broken" in s for s in summaries)
    for d in drafts:
        assert d["patrol"] == "issues"
        assert isinstance(d["severity"], int) and 1 <= d["severity"] <= 5
        assert d["evidence"].get("source") == "gitlab:list_issues"
        assert "iid" in d["evidence"]


async def test_issues_patrol_empty_on_connector_failure():
    """A connector failure (or an unbound connector) yields ZERO sightings — the
    patrol never raises into the shift trigger."""

    async def _boom_execute(workspace_id, name, body, *, user_id=None):
        raise RuntimeError("connector not connected")

    drafts = await patrols_mod.issues_patrol(
        "/tmp/repo", workspace_id=WS, project="g/p", execute=_boom_execute
    )
    assert drafts == [], "a connector failure degrades to zero sightings"


async def test_issues_patrol_registered_in_catalog():
    """The issues patrol is registered so the sense loop can run it."""
    assert "issues" in patrols_mod.PATROLS


async def test_run_patrols_persists_issue_sightings_via_connector_seam(
    tmp_path, mongo_db, monkeypatch
):
    """End-to-end through the REAL ``service.run_patrols`` sense loop: a mandate
    with the ``issues`` patrol enabled, the DEFAULT connector seam patched to
    return a live-shaped payload, persists a SightingDoc per open issue with the
    live ``gitlab:list_issues`` source — proving the patrol rides the real
    signature-inspection call path AND the default ``connectors_service.execute``
    seam (not just the injected mock)."""
    from types import SimpleNamespace

    # Patch the patrol's DEFAULT connector seam — proves run_patrols reaches the
    # live execute path without injecting ``execute`` at the call site.
    async def _fake_default_execute(workspace_id, name, body, *, user_id=None):
        assert name == "gitlab"
        assert body["action"] == "list_issues"
        # The run_patrols actor is threaded to the connector for audit attribution.
        assert user_id == USER
        return SimpleNamespace(
            success=True,
            data=[
                {"iid": 11, "title": "Crash on save", "labels": ["bug"], "state": "opened"},
                {"iid": 12, "title": "Typo in footer", "labels": [], "state": "opened"},
            ],
        )

    monkeypatch.setattr(patrols_mod, "_default_execute", _fake_default_execute)

    created = await mandate_service.create_mandate(
        WS,
        USER,
        {
            "name": "issues-mandate",
            "surface": {"repo_id": str(tmp_path / "my-project")},
            "charter": _charter(),
            "patrols": ["issues", "feedback"],
        },
    )
    mandate_id = created["mandate"]["id"]

    result = await mandate_service.run_patrols(WS, USER, mandate_id)
    sightings = result["sightings"]
    assert len(sightings) == 2, sightings
    for s in sightings:
        assert s["patrol"] == "issues"
        assert s["evidence"]["source"] == "gitlab:list_issues"
    # The deps patrol was NOT enabled on this mandate — no deps sighting leaked.
    assert all(s["patrol"] == "issues" for s in sightings)
