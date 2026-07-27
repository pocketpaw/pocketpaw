# tests/cloud/growth/test_followups.py — the daily follow-up sweep (G-7), the
# slice that closes the /growth outbound cycle.
#
# What it proves:
#   1. A send that went quiet past the delay produces a ``follow_up`` draft AND
#      a real proposal in the Instinct tray — and the draft stops at
#      ``proposed``. Nothing is approved, nothing is sent.
#   2. The stop conditions: a replied prospect, a send below the delay
#      threshold, a follow-up already open, and a prospect already ``dead``.
#   3. Idempotency — a second pass over the same data creates nothing.
#   4. The cap: after ``GROWTH_FOLLOWUP_MAX`` unanswered follow-ups the
#      prospect is retired to ``dead`` and nothing further is created.
#   5. Both env knobs (``GROWTH_FOLLOWUP_DELAY_DAYS`` /
#      ``GROWTH_FOLLOWUP_MAX``) actually move the behaviour.
#   6. The cron is registered on the ``growth`` queue.
#
# Time is FROZEN by injection — every test passes an explicit ``now`` into the
# sweep instead of backdating docs or sleeping. Setup writes rows at the real
# clock and the sweep is then run from a point N days in the future, which is
# the same relative geometry with none of the wall-clock cost.
#
# Harness mirrors test_gate.py: mongomock Beanie via ``mongo_db`` plus ONE tmp
# InstinctStore monkeypatched behind ``pocketpaw.stores.get_instinct_store``,
# so the propose path, the tray assertions and the sweep's proposer lookup all
# hit the same store.
#
# Created 2026-07-27 (feat/growth-g7): new module.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.followups import (
    GROWTH_FOLLOWUP_SWEEP_JOB_NAME,
    followup_sweep,
    render_followup,
)
from pocketpaw_ee.cloud.growth.propose import GROWTH_SEND_PARAM_KEY

from pocketpaw.instinct.store import InstinctStore

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def tray(tmp_path: Path, monkeypatch) -> InstinctStore:
    """One shared InstinctStore behind every seam that resolves a store."""
    store = InstinctStore(tmp_path / "growth_followups.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


@pytest_asyncio.fixture
async def db(mongo_db: Any) -> Any:
    """Alias so tests read as ``(db, tray)`` rather than the raw fixture name."""
    return mongo_db


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


async def _prospect(workspace_id: str, **overrides: Any) -> Any:
    from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest

    payload: dict[str, Any] = {
        "name": "Sam Founder",
        "company": "Acme Dental",
        "domain": "acme-dental.com",
        "source": "manual",
    }
    payload.update(overrides)
    return await growth_service.create(
        _ctx(workspace_id), CreateProspectRequest.model_validate(payload)
    )


async def _draft(
    workspace_id: str,
    prospect_id: str,
    *,
    user_id: str = "u1",
    channel: str = "email",
    subject: str | None = "Quick idea for Acme Dental",
    body: str = "Saw your booking flow — here's a live demo.",
    variant: str = "first_touch",
) -> Any:
    from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest

    return await growth_service.create_draft(
        _ctx(workspace_id, user_id),
        prospect_id,
        CreateDraftRequest(
            channel=channel,  # type: ignore[arg-type]
            subject=subject if channel == "email" else None,
            body=body,
            variant=variant,  # type: ignore[arg-type]
        ),
    )


async def _send(
    workspace_id: str, draft_id: str, *, user_id: str = "u1", gated: bool = True
) -> None:
    """Walk a draft all the way to ``sent`` the way production does.

    ``gated=True`` files the real ``_growth_send`` proposal first, which is
    what leaves the proposer record the sweep later inherits. ``gated=False``
    skips it, simulating a draft that reached ``sent`` without a tray record.
    """
    if gated:
        await growth_service.propose_send(_ctx(workspace_id, user_id), draft_id)
    else:
        await growth_service.gate_transition(workspace_id, draft_id, "proposed")
    await growth_service.gate_transition(workspace_id, draft_id, "approved")
    await growth_service.gate_transition(workspace_id, draft_id, "sent")


async def _sent_thread(
    workspace_id: str = "w1", *, user_id: str = "u1", channel: str = "email", **prospect_kw: Any
) -> tuple[Any, Any]:
    """A prospect with one first-touch draft already sent through the gate."""
    prospect = await _prospect(workspace_id, **prospect_kw)
    draft = await _draft(workspace_id, prospect.id, user_id=user_id, channel=channel)
    await _send(workspace_id, draft.id, user_id=user_id)
    return prospect, draft


async def _drafts(workspace_id: str) -> list[Any]:
    return await growth_service.list_drafts(_ctx(workspace_id))


async def _followups(workspace_id: str) -> list[Any]:
    return [d for d in await _drafts(workspace_id) if d.variant == "follow_up"]


async def _tray_action_for(store: InstinctStore, workspace_id: str, draft_id: str) -> Any:
    """The pending ``_growth_send`` Action filed for a specific draft, if any."""
    for action in await store.list_actions(
        pocket_id=workspace_id, workspace_id=workspace_id, limit=100
    ):
        blob = (getattr(action, "parameters", None) or {}).get(GROWTH_SEND_PARAM_KEY)
        if isinstance(blob, dict) and str(blob.get("draft_id")) == str(draft_id):
            return action
    return None


def _in_days(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


# ---------------------------------------------------------------------------
# The happy path — a quiet send becomes a proposed follow-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_send_proposes_a_followup_into_the_tray(db, tray):
    prospect, first = await _sent_thread()

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 1
    followups = await _followups("w1")
    assert len(followups) == 1
    followup = followups[0]

    # The draft stops at the gate — proposed, never approved, never sent.
    assert followup.status == "proposed"
    assert followup.channel == "email"
    assert followup.prospect_id == prospect.id

    # …and a REAL proposal is waiting in the tray for it.
    action = await _tray_action_for(tray, "w1", followup.id)
    assert action is not None
    assert str(getattr(action.status, "value", action.status)) == "pending"
    blob = action.parameters[GROWTH_SEND_PARAM_KEY]
    assert blob["workspace_id"] == "w1"
    assert blob["draft_id"] == followup.id
    assert blob["channel"] == "email"
    assert blob["preview"]["body"] == followup.body

    # The original send is untouched.
    original = next(d for d in await _drafts("w1") if d.id == first.id)
    assert original.status == "sent"


@pytest.mark.asyncio
async def test_followup_inherits_the_original_proposer(db, tray):
    """A cron has no user, so the follow-up rides the human who sent the first
    touch — otherwise the gate's execute-time RBAC re-check has nobody to
    check and the approved send would fail closed."""
    await _sent_thread(user_id="carol")

    await followup_sweep({}, now=_in_days(5))

    followup = (await _followups("w1"))[0]
    action = await _tray_action_for(tray, "w1", followup.id)
    blob = action.parameters[GROWTH_SEND_PARAM_KEY]
    assert blob["requested_by"] == "carol"
    # The executor resolves the proposer off the trigger, not the blob — they
    # must agree or it refuses to dispatch.
    assert action.trigger.source == "carol"
    assert action.assignee == "carol"


@pytest.mark.asyncio
async def test_followup_copy_threads_under_the_original(db, tray):
    await _sent_thread()

    await followup_sweep({}, now=_in_days(5))

    followup = (await _followups("w1"))[0]
    assert followup.subject == "Re: Quick idea for Acme Dental"
    assert "Sam Founder" in followup.body
    assert "Saw your booking flow" in followup.body  # quotes the first touch


def test_render_followup_does_not_double_prefix_a_reply_subject():
    subject, _ = render_followup(
        channel="email",
        prospect_name="Sam",
        prospect_company="Acme",
        first_touch={"subject": "Re: already a reply", "body": "hi"},
    )
    assert subject == "Re: already a reply"


def test_render_followup_omits_subject_off_email():
    """The draft DTO refuses a subject on non-email channels — the renderer
    must not hand one over."""
    for channel in ("linkedin", "whatsapp"):
        subject, body = render_followup(
            channel=channel,
            prospect_name="Sam",
            prospect_company="Acme",
            first_touch={"subject": None, "body": "hi"},
        )
        assert subject is None
        assert "Acme" in body


# ---------------------------------------------------------------------------
# The stop conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replied_prospect_gets_nothing(db, tray):
    from pocketpaw_ee.cloud.growth.dto import UpdateProspectRequest

    prospect, _ = await _sent_thread()
    await growth_service.update(_ctx("w1"), prospect.id, UpdateProspectRequest(status="replied"))

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 0
    assert await _followups("w1") == []


@pytest.mark.asyncio
async def test_send_below_the_delay_threshold_gets_nothing(db, tray):
    await _sent_thread()

    counters = await followup_sweep({}, now=_in_days(2))

    assert counters["created"] == 0
    assert await _followups("w1") == []


@pytest.mark.asyncio
async def test_existing_open_followup_is_not_duplicated(db, tray):
    from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest

    prospect, _ = await _sent_thread()
    await growth_service.create_followup_draft(
        "w1",
        prospect.id,
        CreateDraftRequest(channel="email", subject="Re: x", body="nudge", variant="follow_up"),
    )

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 0
    assert len(await _followups("w1")) == 1


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db, tray):
    await _sent_thread()

    first = await followup_sweep({}, now=_in_days(5))
    second = await followup_sweep({}, now=_in_days(5))

    assert first["created"] == 1
    assert second["created"] == 0
    assert len(await _followups("w1")) == 1


@pytest.mark.asyncio
async def test_send_with_no_tray_record_is_skipped(db, tray):
    """No resolvable proposer → no follow-up. A proposal nobody can execute
    (the gate re-checks the proposer's role at approve time) is worse than
    none, so the sweep declines rather than filing a dud."""
    prospect = await _prospect("w1")
    draft = await _draft("w1", prospect.id)
    await _send("w1", draft.id, gated=False)

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 0
    assert counters["skipped"] == 1
    assert await _followups("w1") == []


@pytest.mark.asyncio
async def test_a_failed_propose_retracts_its_draft(db, tray, monkeypatch):
    """A draft created but never proposed would sit in ``draft`` forever,
    counting as an open follow-up and stalling the thread on every future
    pass. It gets retracted instead, and the next pass tries again."""
    await _sent_thread()

    real_propose = growth_service.propose_send
    calls = {"n": 0}

    async def _flaky(ctx, draft_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tray unavailable")
        return await real_propose(ctx, draft_id)

    monkeypatch.setattr(growth_service, "propose_send", _flaky)

    failed = await followup_sweep({}, now=_in_days(5))
    assert failed["created"] == 0
    assert [f.status for f in await _followups("w1")] == ["rejected"]

    # Nothing is stuck: the next pass files a real one.
    recovered = await followup_sweep({}, now=_in_days(5))
    assert recovered["created"] == 1
    assert sorted(f.status for f in await _followups("w1")) == ["proposed", "rejected"]


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


async def _thread_with_sent_followups(count: int, *, workspace_id: str = "w1") -> Any:
    """A first touch plus ``count`` follow-ups, all already sent + unanswered."""
    prospect, _ = await _sent_thread(workspace_id)
    for _ in range(count):
        followup = await _draft(
            workspace_id, prospect.id, subject="Re: Quick idea", variant="follow_up"
        )
        await _send(workspace_id, followup.id)
    return prospect


@pytest.mark.asyncio
async def test_third_pass_after_two_followups_retires_the_prospect(db, tray):
    prospect = await _thread_with_sent_followups(2)

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 0
    assert counters["retired"] == 1
    refreshed = await growth_service.get_prospect_system("w1", prospect.id)
    assert refreshed.status == "dead"
    # Still exactly the two follow-ups that were already there.
    assert len(await _followups("w1")) == 2


@pytest.mark.asyncio
async def test_the_whole_cycle_over_three_sweeps(db, tray):
    """The slice end to end, on the real sequence rather than a pre-baked
    state: sweep 1 proposes nudge #1, sweep 2 (after it went out and stayed
    quiet) proposes nudge #2, sweep 3 finds the cap reached and retires the
    prospect. Every draft passes through the human gate on the way."""
    prospect, _ = await _sent_thread()

    assert (await followup_sweep({}, now=_in_days(5)))["created"] == 1
    nudge_1 = (await _followups("w1"))[0]
    assert nudge_1.status == "proposed"
    # The human approves it in the tray and it goes out.
    await growth_service.gate_transition("w1", nudge_1.id, "approved")
    await growth_service.gate_transition("w1", nudge_1.id, "sent")

    assert (await followup_sweep({}, now=_in_days(10)))["created"] == 1
    nudge_2 = next(f for f in await _followups("w1") if f.id != nudge_1.id)
    assert nudge_2.status == "proposed"
    await growth_service.gate_transition("w1", nudge_2.id, "approved")
    await growth_service.gate_transition("w1", nudge_2.id, "sent")

    third = await followup_sweep({}, now=_in_days(15))
    assert third["created"] == 0
    assert third["retired"] == 1
    assert (await growth_service.get_prospect_system("w1", prospect.id)).status == "dead"
    assert len(await _followups("w1")) == 2


@pytest.mark.asyncio
async def test_a_dead_prospect_is_never_touched_again(db, tray):
    await _thread_with_sent_followups(2)

    await followup_sweep({}, now=_in_days(5))
    counters = await followup_sweep({}, now=_in_days(10))

    assert counters["created"] == 0
    assert counters["retired"] == 0  # already dead — not re-retired
    assert len(await _followups("w1")) == 2


@pytest.mark.asyncio
async def test_one_sent_followup_still_earns_a_second(db, tray):
    """The cap is 2, so a thread with one unanswered follow-up gets one more —
    the dedupe guard is about OPEN follow-ups, not any follow-up ever."""
    await _thread_with_sent_followups(1)

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 1
    followups = await _followups("w1")
    assert len(followups) == 2
    assert sum(1 for f in followups if f.status == "proposed") == 1


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delay_days_env_moves_the_threshold(db, tray, monkeypatch):
    await _sent_thread()
    monkeypatch.setenv("GROWTH_FOLLOWUP_DELAY_DAYS", "10")

    assert (await followup_sweep({}, now=_in_days(5)))["created"] == 0
    assert (await followup_sweep({}, now=_in_days(11)))["created"] == 1


@pytest.mark.asyncio
async def test_followup_max_env_moves_the_cap(db, tray, monkeypatch):
    prospect = await _thread_with_sent_followups(1)
    monkeypatch.setenv("GROWTH_FOLLOWUP_MAX", "1")

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 0
    assert counters["retired"] == 1
    assert (await growth_service.get_prospect_system("w1", prospect.id)).status == "dead"


@pytest.mark.asyncio
async def test_a_junk_env_value_falls_back_to_the_default(db, tray, monkeypatch):
    await _sent_thread()
    monkeypatch.setenv("GROWTH_FOLLOWUP_DELAY_DAYS", "not-a-number")

    assert (await followup_sweep({}, now=_in_days(5)))["created"] == 1


# ---------------------------------------------------------------------------
# Tenancy + registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_crosses_tenants_without_mixing_them(db, tray):
    """The cron has no request workspace, so it scans globally — every row it
    writes must still land in the workspace it came from."""
    await _sent_thread("w1", user_id="alice")
    await _sent_thread("w2", user_id="bob", domain="other-co.com")

    counters = await followup_sweep({}, now=_in_days(5))

    assert counters["created"] == 2
    w1_followup = (await _followups("w1"))[0]
    w2_followup = (await _followups("w2"))[0]
    assert w1_followup.workspace_id == "w1"
    assert w2_followup.workspace_id == "w2"
    w1_blob = (await _tray_action_for(tray, "w1", w1_followup.id)).parameters[GROWTH_SEND_PARAM_KEY]
    w2_blob = (await _tray_action_for(tray, "w2", w2_followup.id)).parameters[GROWTH_SEND_PARAM_KEY]
    assert w1_blob["requested_by"] == "alice"
    assert w2_blob["requested_by"] == "bob"


def test_cron_is_registered_on_the_growth_queue():
    from pocketpaw_ee.cloud.growth.worker import WorkerSettings

    assert WorkerSettings.queue_name == "growth"
    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    sweep = jobs[GROWTH_FOLLOWUP_SWEEP_JOB_NAME]
    # Daily, and unique so a scaled-out worker fleet runs one tick, not N.
    assert sweep.hour == 13
    assert sweep.minute == 0
    assert sweep.unique is True
