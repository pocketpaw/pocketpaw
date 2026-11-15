# tests/cloud/growth/social/test_brand_archive.py — renaming and archiving a
# Growth › Social brand (``PATCH /growth/social/profile`` and
# ``POST /growth/social/profile/archive``). Same harness as test_router.py:
# the social router over mongomock-backed Beanie with a fixed RequestContext
# per workspace, and the fake Postiz client installed so any Postiz call shows.
#
# Covers: rename and its 422s / 404; archive hides the brand from the list and
# the default pick while a read by id still works; archive unschedules the
# brand's scheduled ideas through the unschedule path (calendar event gone,
# status kept) and never calls Postiz; outreach drafts untouched; the daily
# drafts, reply cards, repo metrics and Pulse sweeps skip archived brands;
# tenancy; RBAC (both writes need growth.manage); the mapper round-trip.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.ideas import set_production_ideas_fn
from pocketpaw_ee.cloud.growth.social.postiz import set_production_postiz
from pocketpaw_ee.cloud.growth.social.pulse import set_production_pulse_fn
from pocketpaw_ee.cloud.growth.social.replies import set_production_reply_draft_fn
from pocketpaw_ee.cloud.growth.social.repo_metrics import set_production_repo_metrics_fn
from pocketpaw_ee.cloud.growth.social.router import router as social_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.draft import Draft as DraftDoc
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as ProfileDoc
from pocketpaw_ee.cloud.models.social_pulse import SocialPulse as PulseDoc

BASE = "/api/v1/growth/social"
PROFILE = f"{BASE}/profile"
ARCHIVE = f"{PROFILE}/archive"


class _FakeMembership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, workspace_id: str, role: str) -> None:
        self.id = "u1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace_id, role)]


def _build_app(workspace_id: str, role: str) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(social_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    user = _FakeUser(workspace_id, role)
    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


def _client(workspace_id: str, role: str = "admin") -> AsyncClient:
    transport = ASGITransport(app=_build_app(workspace_id, role))
    return AsyncClient(transport=transport, base_url="http://t")


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with _client("w2") as client:
        yield client


class _RecordingPostiz:
    """Records every Postiz call; archive must make none."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        async def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return []

        return _call


@pytest.fixture
def postiz():
    fake = _RecordingPostiz()
    set_production_postiz(fake)  # type: ignore[arg-type]
    try:
        yield fake
    finally:
        set_production_postiz(None)


@pytest.fixture(autouse=True)
def _reset_seams():
    try:
        yield
    finally:
        set_production_ideas_fn(None)
        set_production_reply_draft_fn(None)
        set_production_repo_metrics_fn(None)
        set_production_pulse_fn(None)


async def _brand(client: AsyncClient, name: str) -> str:
    pid = (await client.post(f"{BASE}/profiles")).json()["id"]
    resp = await client.put(PROFILE, params={"profile_id": pid}, json={"company_name": name})
    assert resp.status_code == 200, resp.text
    return pid


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_sets_the_trimmed_name(w1):
    pid = await _brand(w1, "Acme")
    resp = await w1.patch(PROFILE, params={"profile_id": pid}, json={"name": "  Acme Labs  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["company_name"] == "Acme Labs"
    again = await w1.get(PROFILE, params={"profile_id": pid})
    assert again.json()["company_name"] == "Acme Labs"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"name": ""}, {"name": "   "}, {"name": "x" * 121}, {}])
async def test_rename_refuses_an_empty_or_long_name(w1, body):
    pid = await _brand(w1, "Acme")
    resp = await w1.patch(PROFILE, params={"profile_id": pid}, json=body)
    assert resp.status_code == 422
    assert (await w1.get(PROFILE, params={"profile_id": pid})).json()["company_name"] == "Acme"


@pytest.mark.asyncio
async def test_rename_is_404_without_a_profile(w1):
    assert (await w1.patch(PROFILE, json={"name": "Acme"})).status_code == 404


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_hides_the_brand_from_the_list_and_the_default(w1):
    keep = await _brand(w1, "Keep")
    gone = await _brand(w1, "Gone")
    resp = await w1.post(ARCHIVE, params={"profile_id": gone})
    assert resp.status_code == 200, resp.text
    archived_at = resp.json()["archived_at"]
    assert archived_at

    listed = (await w1.get(f"{BASE}/profiles")).json()["items"]
    assert [p["id"] for p in listed] == [keep]
    # Archiving bumped updatedAt, yet the default pick is still the live brand.
    assert (await w1.get(PROFILE)).json()["id"] == keep
    # A read by id still works, so links don't break.
    direct = await w1.get(PROFILE, params={"profile_id": gone})
    assert direct.status_code == 200
    assert direct.json()["archived_at"] == archived_at

    # A repeat keeps the first stamp.
    again = await w1.post(ARCHIVE, params={"profile_id": gone})
    assert again.json()["archived_at"] == archived_at


@pytest.mark.asyncio
async def test_archive_needs_a_profile_id(w1):
    await _brand(w1, "Acme")
    assert (await w1.post(ARCHIVE)).status_code == 422


@pytest.mark.asyncio
async def test_archive_unschedules_scheduled_ideas_and_leaves_outreach_alone(w1, postiz):
    from pocketpaw_ee.calendar.models import _EventDoc as CalendarEventDoc

    pid = await _brand(w1, "Acme")
    other = await _brand(w1, "Other")
    ideas = []
    for profile in (pid, pid, other):
        doc = IdeaDoc(workspace="w1", profile=profile, format="x_post", hook="h", status="approved")
        await doc.insert()
        ideas.append(doc)
    scheduled = await w1.post(
        f"{BASE}/ideas/schedule",
        json={
            "items": [
                {"idea_id": str(ideas[0].id), "scheduled_at": "2026-12-01T10:00:00Z"},
                {"idea_id": str(ideas[2].id), "scheduled_at": "2026-12-02T10:00:00Z"},
            ]
        },
    )
    assert scheduled.status_code == 200, scheduled.text
    event_id, other_event_id = (i["calendar_event_id"] for i in scheduled.json()["items"])
    draft = DraftDoc(workspace="w1", prospect_id="p1", channel="email", body="Hi")
    await draft.insert()
    draft_before = (await DraftDoc.get(draft.id)).model_dump()

    assert (await w1.post(ARCHIVE, params={"profile_id": pid})).status_code == 200

    cleared = await IdeaDoc.get(ideas[0].id)
    assert cleared.scheduled_at is None and cleared.calendar_event_id == ""
    assert cleared.status == "approved"
    assert await CalendarEventDoc.get(event_id) is None
    # The other brand's schedule is untouched.
    kept = await IdeaDoc.get(ideas[2].id)
    assert kept.scheduled_at is not None and kept.calendar_event_id == other_event_id
    assert await CalendarEventDoc.get(other_event_id) is not None
    # Outreach drafts are untouched, and nothing reached Postiz.
    assert (await DraftDoc.get(draft.id)).model_dump() == draft_before
    assert postiz.calls == []


# ---------------------------------------------------------------------------
# Tenancy and RBAC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_workspace_cannot_rename_or_archive(w1, w2):
    pid = await _brand(w1, "Acme")
    assert (
        await w2.patch(PROFILE, params={"profile_id": pid}, json={"name": "Stolen"})
    ).status_code == 404
    assert (await w2.post(ARCHIVE, params={"profile_id": pid})).status_code == 404
    profile = (await w1.get(PROFILE, params={"profile_id": pid})).json()
    assert profile["company_name"] == "Acme" and profile["archived_at"] is None


@pytest.mark.asyncio
async def test_rename_and_archive_need_growth_manage(mongo_db):
    async with _client("w1", role="member") as member:
        pid = await _brand(member, "Acme")
        renamed = await member.patch(PROFILE, params={"profile_id": pid}, json={"name": "B"})
        assert renamed.status_code == 403
        assert (await member.post(ARCHIVE, params={"profile_id": pid})).status_code == 403


def test_profile_mappers_carry_archived_at():
    when = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    doc = ProfileDoc(workspace="w1", archived_at=when)
    domain = social_service._profile_to_domain(doc)
    assert domain.archived_at == when
    out = social_service._profile_to_response(domain)
    assert out.archived_at is not None and out.archived_at.startswith("2026-11-01T09:30:00")
    assert (
        social_service._profile_to_response(
            social_service._profile_to_domain(ProfileDoc(workspace="w1"))
        ).archived_at
        is None
    )


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------


async def _sweep_profiles() -> tuple[str, str]:
    """One live and one archived brand, both active, onboarded and fully wired."""
    now = datetime.now(UTC)
    ids = []
    for archived_at in (None, now):
        doc = ProfileDoc(
            workspace="w1",
            active=True,
            onboarding_completed_at=now,
            github_repos=["acme/live"] if archived_at is None else ["acme/gone"],
            reply_queries=["acme"],
            archived_at=archived_at,
        )
        await doc.insert()
        ids.append(str(doc.id))
    return ids[0], ids[1]


@pytest.mark.asyncio
async def test_daily_and_reply_sweeps_skip_archived_brands(mongo_db, monkeypatch):
    live, _ = await _sweep_profiles()
    seen: dict[str, list[str]] = {"daily": [], "replies": []}

    async def _draft_daily(workspace: str, profile: str, now: datetime):
        seen["daily"].append(profile)
        return [], 0

    async def _draft_replies(workspace: str, profile: str, now: datetime):
        seen["replies"].append(profile)
        return [], 0

    monkeypatch.setattr(social_service, "_draft_daily", _draft_daily)
    monkeypatch.setattr(social_service, "_draft_replies", _draft_replies)
    set_production_ideas_fn(lambda *a, **k: None)  # type: ignore[arg-type]
    set_production_reply_draft_fn(lambda *a, **k: None)  # type: ignore[arg-type]

    assert (await social_service.run_daily_drafts())["profiles"] == 1
    assert (await social_service.run_reply_drafts())["profiles"] == 1
    assert seen == {"daily": [live], "replies": [live]}


@pytest.mark.asyncio
async def test_repo_metrics_sweep_skips_archived_brands(mongo_db, monkeypatch):
    await _sweep_profiles()
    read: list[str] = []

    async def _metrics(repo: str, day: str):
        read.append(repo)
        return object()

    async def _store(*args: Any) -> None:
        return None

    set_production_repo_metrics_fn(_metrics)  # type: ignore[arg-type]
    monkeypatch.setattr(social_service, "_store_metric", _store)
    counts = await social_service.run_repo_metrics()
    assert counts["repos"] == 1
    assert read == ["acme/live"]


@pytest.mark.asyncio
async def test_pulse_sweep_skips_archived_brands(mongo_db, monkeypatch):
    live, gone = await _sweep_profiles()
    now = datetime.now(UTC)
    for profile in (live, gone):
        await PulseDoc(
            workspace="w1",
            profile=profile,
            manual_refreshed_at=now - timedelta(days=1),
            refreshed_at=now - timedelta(days=2),
        ).insert()
    rebuilt: list[str] = []

    async def _rebuild(doc: Any, *args: Any, **kwargs: Any) -> None:
        rebuilt.append(doc.profile)

    set_production_pulse_fn(lambda *a, **k: None)  # type: ignore[arg-type]
    monkeypatch.setattr(social_service, "_rebuild_pulse", _rebuild)
    counts = await social_service.refresh_due_pulses(now)
    assert rebuilt == [live]
    assert counts == {"due": 2, "refreshed": 1, "failed": 0}
