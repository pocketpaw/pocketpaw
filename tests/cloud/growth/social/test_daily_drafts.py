# tests/cloud/growth/social/test_daily_drafts.py — the daily X drafting sweep
# (``run_daily_drafts``), its on-demand route (``POST /growth/social/ideas/daily``)
# and its cron. Same harness as test_router.py: the social router over
# mongomock-backed Beanie, per-workspace clients, and fake ``IdeasFn`` /
# ``ChangesFn`` that record every call. No network, no model.
#
# Covers: X only, stored ``new`` / ``daily`` with a ``suggested_at`` slot; the
# two-a-day and weekly quotas (manual ideas, old drafts and another workspace's
# rows never count); inactive and not-onboarded profiles skipped by the sweep and
# refused by the route; dash and invented-number drafts dropped with exactly one
# retry carrying the reasons, a release version number kept; a failing profile or
# a failing changes read never stops the sweep; cross-tenant isolation; 503 with
# no ideas fn; the ``source`` / ``suggested_at`` mappers and list filter; the
# retry prompt; the worker's cron entry and its fn installation.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pocketpaw_ee.cloud.growth.social import changes as social_changes
from pocketpaw_ee.cloud.growth.social import ideas as social_ideas
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.changes import Change, set_production_changes_fn
from pocketpaw_ee.cloud.growth.social.domain import GeneratedIdea, SocialProfile
from pocketpaw_ee.cloud.growth.social.ideas import (
    build_ideas_prompt,
    check_draft,
    set_production_ideas_fn,
)
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc

from tests.cloud.growth.social.test_router import _COMPLETE, BASE, IDEAS, PROFILE, _client

DAILY = f"{IDEAS}/daily"
CHANGES = [
    Change("release", "v0.7.0", "https://github.com/acme/app/releases/tag/v0.7.0", "Faster sync"),
    Change("pr", "Cut cold start to 40%", "https://github.com/acme/app/pull/123", ""),
]


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with await _client("w2") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_seams():
    try:
        yield
    finally:
        set_production_ideas_fn(None)
        set_production_changes_fn(None)


def _today(hour: int, minute: int = 0) -> datetime:
    return datetime.now(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _x(hook: str, **kw: Any) -> GeneratedIdea:
    return GeneratedIdea(format="x_post", hook=hook, caption=kw.pop("caption", "Out now."), **kw)


class _FakeIdeas:
    """Returns ``script[i]`` on the i-th call (clean ideas once the script runs out)."""

    def __init__(self, *script: tuple[GeneratedIdea, ...] | Exception, fail_for: str = "") -> None:
        self.script = list(script)
        self.fail_for = fail_for
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        profile: SocialProfile,
        count: int,
        recent_hooks: list[str],
        *,
        platform: str | None = None,
        pulse: list | None = None,
        changes: list[Change] | None = None,
        retry_note: str | None = None,
    ) -> tuple[GeneratedIdea, ...]:
        self.calls.append(
            {
                "profile": profile,
                "count": count,
                "platform": platform,
                "changes": changes,
                "retry_note": retry_note,
            }
        )
        if self.fail_for and profile.company_name == self.fail_for:
            raise RuntimeError("model down")
        if len(self.calls) <= len(self.script):
            result = self.script[len(self.calls) - 1]
            if isinstance(result, Exception):
                raise result
            return result
        # No digits, so these pass ``check_draft`` whatever the changes say.
        return tuple(
            _x(f"Shipped for {profile.company_name}, take {'abcdefgh'[i]}") for i in range(count)
        )


class _FakeChanges:
    def __init__(self, result: list[Change] | Exception = CHANGES) -> None:
        self.result = result
        self.calls: list[tuple[list[str], datetime]] = []

    async def __call__(self, repos: list[str], *, since: datetime) -> list[Change]:
        self.calls.append((repos, since))
        if isinstance(self.result, Exception):
            raise self.result
        return list(self.result)


async def _product(
    client: AsyncClient,
    *,
    active: bool = True,
    quota: int = 5,
    complete: bool = True,
    company: str = "Acme",
    new: bool = False,
) -> str:
    params: dict[str, str] = {}
    if new:
        params["profile_id"] = (await client.post(f"{BASE}/profiles")).json()["id"]
    body = {
        **_COMPLETE,
        "company_name": company,
        "active": active,
        "weekly_post_quota": quota,
        "github_repos": ["acme/app"],
    }
    resp = await client.put(PROFILE, params=params, json=body)
    assert resp.status_code == 200, resp.text
    if complete:
        assert (await client.post(f"{PROFILE}/complete", params=params)).status_code == 200
    return resp.json()["id"]


async def _seed_daily(workspace: str, profile: str, created: datetime, source: str = "daily"):
    """Insert a row with a chosen ``createdAt`` (``insert()`` would overwrite it)."""
    await IdeaDoc.get_pymongo_collection().insert_one(
        {
            "workspace": workspace,
            "profile": profile,
            "format": "x_post",
            "hook": f"old {created.isoformat()}",
            "platform": "x",
            "status": "new",
            "source": source,
            "createdAt": created,
            "updatedAt": created,
        }
    )


async def _listed(client: AsyncClient, profile_id: str, **params: str) -> list[dict[str, Any]]:
    resp = await client.get(IDEAS, params={"profile_id": profile_id, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# --- the sweep ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_drafts_x_only_as_new_daily_ideas_with_slots(w1):
    ideas, changes = _FakeIdeas(), _FakeChanges()
    set_production_ideas_fn(ideas)
    set_production_changes_fn(changes)
    pid = await _product(w1)
    now = _today(5, 30)

    counts = await social_service.run_daily_drafts(now=now)

    assert counts == {"profiles": 1, "drafted": 2, "dropped": 0, "failed": 0}
    assert len(ideas.calls) == 1
    assert ideas.calls[0]["platform"] == "x"
    assert ideas.calls[0]["count"] == 2
    assert ideas.calls[0]["changes"] == CHANGES
    assert changes.calls == [(["acme/app"], now - timedelta(hours=24))]
    made = await _listed(w1, pid, status="new", source="daily")
    assert len(made) == 2
    assert {i["platform"] for i in made} == {"x"}
    assert {i["status"] for i in made} == {"new"}
    slots = sorted(datetime.fromisoformat(i["suggested_at"]) for i in made)
    assert slots == [_today(14), _today(17)]


@pytest.mark.asyncio
async def test_slots_roll_to_tomorrow_once_today_has_passed(w1):
    set_production_ideas_fn(_FakeIdeas())
    pid = await _product(w1)
    await social_service.run_daily_drafts(now=_today(15))
    slots = sorted(datetime.fromisoformat(i["suggested_at"]) for i in await _listed(w1, pid))
    assert slots == [_today(17), _today(14) + timedelta(days=1)]


@pytest.mark.asyncio
async def test_two_a_day_cap_holds_across_runs(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    await _product(w1, quota=21)
    first = await social_service.run_daily_drafts(now=_today(5, 30))
    second = await social_service.run_daily_drafts(now=_today(5, 31))
    assert first["drafted"] == 2
    assert second["drafted"] == 0
    assert len(ideas.calls) == 1


@pytest.mark.asyncio
async def test_weekly_quota_counts_only_this_profiles_daily_drafts_this_week(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    pid = await _product(w1, quota=3)
    now = _today(5, 30)
    # Two daily drafts earlier this week count; the rest must not.
    await _seed_daily("w1", pid, now - timedelta(days=3))
    await _seed_daily("w1", pid, now - timedelta(days=4))
    await _seed_daily("w1", pid, now - timedelta(days=8))  # outside the 7 days
    await _seed_daily("w1", pid, now - timedelta(minutes=5), source="manual")
    await _seed_daily("w2", pid, now - timedelta(minutes=5))  # another tenant's row

    counts = await social_service.run_daily_drafts(now=now)

    assert counts["drafted"] == 1
    assert [c["count"] for c in ideas.calls] == [1]


@pytest.mark.asyncio
async def test_zero_quota_drafts_nothing(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    await _product(w1, quota=0)
    assert (await social_service.run_daily_drafts(now=_today(5, 30)))["drafted"] == 0
    assert ideas.calls == []


@pytest.mark.asyncio
async def test_sweep_skips_inactive_and_not_onboarded_profiles(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    live = await _product(w1, company="Live")
    await _product(w1, company="Off", active=False, new=True)
    await _product(w1, company="Draft", complete=False, new=True)

    counts = await social_service.run_daily_drafts(now=_today(5, 30))

    assert counts["profiles"] == 1
    assert [c["profile"].id for c in ideas.calls] == [live]


@pytest.mark.asyncio
async def test_bad_drafts_are_dropped_and_one_retry_carries_the_reasons(w1):
    ideas = _FakeIdeas(
        (
            _x("Shipped v0.7.0 today"),  # version from the release title: kept
            _x("Faster — finally"),  # em dash: dropped
            _x("Now 10x faster"),  # 10 is not in the changes: dropped
        ),
        (_x("Cold start cut to 40%"),),
    )
    set_production_ideas_fn(ideas)
    set_production_changes_fn(_FakeChanges())
    pid = await _product(w1)

    counts = await social_service.run_daily_drafts(now=_today(5, 30))

    assert counts == {"profiles": 1, "drafted": 2, "dropped": 2, "failed": 0}
    assert len(ideas.calls) == 2
    assert ideas.calls[0]["retry_note"] is None
    note = ideas.calls[1]["retry_note"]
    assert "uses an em or en dash" in note
    assert "uses a number not in the source: 10" in note
    assert ideas.calls[1]["count"] == 1
    assert ideas.calls[1]["platform"] == "x"
    hooks = {i["hook"] for i in await _listed(w1, pid)}
    assert hooks == {"Shipped v0.7.0 today", "Cold start cut to 40%"}


@pytest.mark.asyncio
async def test_the_retry_happens_once_and_bad_retries_are_dropped_too(w1):
    bad = (_x("Up 99%"), _x("Now – better"))
    ideas = _FakeIdeas(bad, bad)
    set_production_ideas_fn(ideas)
    pid = await _product(w1)
    counts = await social_service.run_daily_drafts(now=_today(5, 30))
    assert len(ideas.calls) == 2
    assert counts["drafted"] == 0 and counts["dropped"] == 4
    assert await _listed(w1, pid) == []


@pytest.mark.asyncio
async def test_a_failing_profile_does_not_stop_the_sweep(w1, w2):
    set_production_ideas_fn(_FakeIdeas(fail_for="Broken"))
    await _product(w1, company="Broken")
    ok = await _product(w2, company="Fine")
    counts = await social_service.run_daily_drafts(now=_today(5, 30))
    assert counts == {"profiles": 2, "drafted": 2, "dropped": 0, "failed": 1}
    assert len(await _listed(w2, ok)) == 2


@pytest.mark.asyncio
async def test_a_failing_changes_read_still_drafts_without_changes(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    set_production_changes_fn(_FakeChanges(RuntimeError("github down")))
    await _product(w1)
    assert (await social_service.run_daily_drafts(now=_today(5, 30)))["drafted"] == 2
    assert ideas.calls[0]["changes"] == []


@pytest.mark.asyncio
async def test_no_ideas_fn_sweeps_nothing(w1):
    await _product(w1)
    counts = await social_service.run_daily_drafts(now=_today(5, 30))
    assert counts == {"profiles": 0, "drafted": 0, "dropped": 0, "failed": 0}


# --- the route ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_drafts_now_and_returns_the_new_ideas(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    pid = await _product(w1)
    resp = await w1.post(DAILY, params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert ideas.calls[0]["platform"] == "x"
    assert 1 <= len(items) <= 2
    assert all(i["source"] == "daily" and i["status"] == "new" for i in items)
    assert all(i["suggested_at"] for i in items)
    again = await w1.post(DAILY, params={"profile_id": pid})
    # Whatever is left of today's two, never more.
    assert len(items) + len(again.json()["items"]) <= 2


@pytest.mark.asyncio
async def test_route_without_ideas_fn_is_503(w1):
    pid = await _product(w1)
    resp = await w1.post(DAILY, params={"profile_id": pid})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "social.ideas_unavailable"


@pytest.mark.asyncio
async def test_route_refuses_before_onboarding_and_for_inactive_products(w1):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    draft = await _product(w1, complete=False)
    off = await _product(w1, active=False, new=True)
    resp = await w1.post(DAILY, params={"profile_id": draft})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "social.onboarding_incomplete"
    resp = await w1.post(DAILY, params={"profile_id": off})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "social.profile_inactive"
    assert ideas.calls == []


@pytest.mark.asyncio
async def test_route_never_reads_or_writes_another_workspaces_profile(w1, w2):
    ideas = _FakeIdeas()
    set_production_ideas_fn(ideas)
    theirs = await _product(w2)
    resp = await w1.post(DAILY, params={"profile_id": theirs})
    assert resp.status_code == 404
    assert ideas.calls == []
    assert await _listed(w2, theirs) == []
    assert await IdeaDoc.find({}).count() == 0


# --- fields, filter, prompt, check ---------------------------------------------------------


@pytest.mark.asyncio
async def test_mappers_carry_source_and_suggested_at(mongo_db):
    slot = datetime(2026, 11, 14, 14, tzinfo=UTC)
    doc = IdeaDoc(
        workspace="w1", profile="p", format="x_post", hook="h", source="daily", suggested_at=slot
    )
    await doc.insert()
    domain = social_service._idea_to_domain(doc)
    assert (domain.source, domain.suggested_at) == ("daily", slot)
    wire = social_service._idea_to_response(domain)
    assert (wire.source, wire.suggested_at) == ("daily", slot.isoformat())


@pytest.mark.asyncio
async def test_rows_without_source_read_and_filter_as_manual(w1):
    pid = await _product(w1)
    await IdeaDoc.get_pymongo_collection().insert_one(
        {"workspace": "w1", "profile": pid, "format": "x_post", "hook": "legacy", "status": "new"}
    )
    await _seed_daily("w1", pid, datetime.now(UTC))
    manual = await _listed(w1, pid, source="manual")
    assert [(i["hook"], i["source"], i["suggested_at"]) for i in manual] == [
        ("legacy", "manual", None)
    ]
    assert [i["source"] for i in await _listed(w1, pid, source="daily")] == ["daily"]
    assert len(await _listed(w1, pid)) == 2


def test_retry_note_reaches_the_prompt_only_when_set():
    profile = SocialProfile(id="p", workspace_id="w1", company_name="Acme")
    plain = build_ideas_prompt(profile, 2, [], "x")
    retry = build_ideas_prompt(profile, 2, [], "x", retry_note="uses an em or en dash")
    assert "rejected" not in plain
    assert "rejected" in retry and "uses an em or en dash" in retry


def test_check_draft_reads_hashtags_too():
    assert check_draft(_x("Fine", hashtags=("#build–log",)), "") == ["uses an em or en dash"]
    assert check_draft(_x("Fine", hashtags=("#Top10",)), "") == [
        "uses a number not in the source: 10"
    ]


# --- the cron -----------------------------------------------------------------------------


def test_worker_registers_the_daily_drafts_cron_after_the_pulse_refresh():
    from pocketpaw_ee.cloud.growth.domain import GROWTH_SOCIAL_DAILY_DRAFTS_JOB_NAME
    from pocketpaw_ee.cloud.growth.worker import WorkerSettings, social_daily_drafts

    assert GROWTH_SOCIAL_DAILY_DRAFTS_JOB_NAME == "growth.social_daily_drafts"
    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    job = jobs[GROWTH_SOCIAL_DAILY_DRAFTS_JOB_NAME]
    assert (job.hour, job.minute) == (5, 30)
    assert job.unique is True and job.run_at_startup is False
    assert job.coroutine is social_daily_drafts


@pytest.mark.asyncio
async def test_worker_job_installs_the_production_fns_when_missing(monkeypatch):
    from pocketpaw_ee.cloud.growth import worker

    async def _stub_run(now: datetime | None = None) -> dict[str, int]:
        return {"profiles": 0, "drafted": 0, "dropped": 0, "failed": 0}

    monkeypatch.setattr(social_service, "run_daily_drafts", _stub_run)
    assert (await worker.social_daily_drafts({}))["drafted"] == 0
    assert social_ideas.resolve_ideas_fn() is social_ideas.agent_generate_ideas
    assert social_changes.resolve_changes_fn() is social_changes.github_changes

    fake_ideas, fake_changes = _FakeIdeas(), _FakeChanges()
    set_production_ideas_fn(fake_ideas)
    set_production_changes_fn(fake_changes)
    await worker.social_daily_drafts({})
    assert social_ideas.resolve_ideas_fn() is fake_ideas
    assert social_changes.resolve_changes_fn() is fake_changes
