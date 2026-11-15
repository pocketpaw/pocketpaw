# tests/cloud/growth/social/test_brand_archive.py — renaming and archiving a
# Growth › Social brand (``PATCH /growth/social/profile`` and
# ``POST /growth/social/profile/archive``). Same harness as test_router.py:
# the social router over mongomock-backed Beanie with a fixed RequestContext
# per workspace, and a stateful fake Postiz (posts queued through ``POST /posts``
# land in its list as ``QUEUE``; a delete removes the post's group).
#
# Covers: rename and its 422s / 404; archive hides the brand from the list and
# the default pick while a read by id still works; archive deletes the brand's
# QUEUE posts in Postiz (never published or errored ones, never another brand's),
# unschedules its calendar ideas (event gone, status kept), carries on past a
# failed delete or unschedule, and a repeat archive retries a failed delete;
# the client's DELETE route; outreach drafts untouched; the daily
# drafts, reply cards, repo metrics and Pulse sweeps skip archived brands;
# tenancy; RBAC (both writes need growth.manage); the mapper round-trip.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.crypto import encrypt
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.ideas import set_production_ideas_fn
from pocketpaw_ee.cloud.growth.social.postiz import PostizError, set_production_postiz
from pocketpaw_ee.cloud.growth.social.pulse import set_production_pulse_fn
from pocketpaw_ee.cloud.growth.social.replies import set_production_reply_draft_fn
from pocketpaw_ee.cloud.growth.social.repo_metrics import set_production_repo_metrics_fn
from pocketpaw_ee.cloud.growth.social.router import router as social_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.draft import Draft as DraftDoc
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg as PostizOrgDoc
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


class _FakePostiz:
    """Posts made through ``create_posts`` are queued under the org key; a delete
    removes the post's whole group, like Postiz; ids in ``fail`` refuse to delete."""

    def __init__(self) -> None:
        self.integrations: dict[str, list[dict[str, Any]]] = {}
        self.posts: dict[str, list[dict[str, Any]]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    async def list_integrations(self, api_key: str) -> list[dict[str, Any]]:
        return list(self.integrations.get(api_key, []))

    async def create_posts(self, api_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        group = f"g-{sum(len(v) for v in self.posts.values())}"
        made = []
        for post in payload["posts"]:
            integration = post["integration"]["id"]
            row = {"id": f"post-{integration}-{group}", "state": "QUEUE", "group": group}
            self.posts.setdefault(api_key, []).append(
                {**row, "publishDate": payload["date"], "integration": {"id": integration}}
            )
            made.append({"postId": row["id"], "integration": integration})
        return made

    async def list_posts(self, api_key: str, start: str, end: str) -> list[dict[str, Any]]:
        return list(self.posts.get(api_key, []))

    async def delete_post(self, api_key: str, post_id: str) -> None:
        self.deleted.append((api_key, post_id))
        if post_id in self.fail:
            raise PostizError("postiz.http_error", "Postiz answered 500: busy")
        group = next(p["group"] for p in self.posts[api_key] if p["id"] == post_id)
        self.posts[api_key] = [p for p in self.posts[api_key] if p["group"] != group]

    def ids(self, api_key: str) -> set[str]:
        return {p["id"] for p in self.posts.get(api_key, [])}


@pytest.fixture
def postiz(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    fake = _FakePostiz()
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


async def _connected_brand(client: AsyncClient, postiz: _FakePostiz, name: str) -> str:
    pid = await _brand(client, name)
    key = f"key-{name.lower()}"
    await PostizOrgDoc(
        workspace="w1", profile=pid, postiz_org_id=f"org-{key}", api_key_enc=encrypt(key)
    ).insert()
    postiz.integrations[key] = [{"id": f"x-{name.lower()}", "identifier": "x", "name": name}]
    return pid


async def _queue_post(client: AsyncClient, pid: str, account: str) -> None:
    when = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    resp = await client.post(
        f"{BASE}/posts",
        params={"profile_id": pid},
        json={"content": "hi", "targets": [{"account_id": account}], "scheduled_at": when},
    )
    assert resp.status_code == 200, resp.text


async def _calendar_idea(client: AsyncClient, pid: str, when: str) -> tuple[str, str]:
    doc = IdeaDoc(workspace="w1", profile=pid, format="x_post", hook="h", status="approved")
    await doc.insert()
    resp = await client.post(
        f"{BASE}/ideas/schedule", json={"items": [{"idea_id": str(doc.id), "scheduled_at": when}]}
    )
    assert resp.status_code == 200, resp.text
    return str(doc.id), resp.json()["items"][0]["calendar_event_id"]


@pytest.mark.asyncio
async def test_archive_deletes_queued_posts_unschedules_ideas_and_leaves_outreach(
    w1, postiz, caplog
):
    from pocketpaw_ee.calendar.models import _EventDoc as CalendarEventDoc

    pid = await _connected_brand(w1, postiz, "Acme")
    other = await _connected_brand(w1, postiz, "Other")
    await _queue_post(w1, pid, "x-acme")
    await _queue_post(w1, other, "x-other")
    (queued,) = postiz.ids("key-acme")
    other_posts = postiz.ids("key-other")
    postiz.posts["key-acme"] += [
        {"id": "pub-1", "state": "PUBLISHED", "group": "g-pub"},
        {"id": "err-1", "state": "ERROR", "group": "g-err"},
    ]
    idea_id, event_id = await _calendar_idea(w1, pid, "2026-12-01T10:00:00Z")
    other_idea, other_event = await _calendar_idea(w1, other, "2026-12-02T10:00:00Z")
    draft = DraftDoc(workspace="w1", prospect_id="p1", channel="email", body="Hi")
    await draft.insert()
    draft_before = (await DraftDoc.get(draft.id)).model_dump()
    postiz.fail = {queued}

    first = await w1.post(ARCHIVE, params={"profile_id": pid})
    assert first.status_code == 200, first.text
    # The refused delete is logged by code, without the key, and the archive carries on.
    assert postiz.deleted == [("key-acme", queued)]
    assert queued in caplog.text and "postiz.http_error" in caplog.text
    assert "key-acme" not in caplog.text
    cleared = await IdeaDoc.get(idea_id)
    assert cleared.scheduled_at is None and cleared.calendar_event_id == ""
    assert cleared.status == "approved"
    assert await CalendarEventDoc.get(event_id) is None

    # A repeat retries the delete and keeps the first stamp.
    postiz.fail = set()
    again = await w1.post(ARCHIVE, params={"profile_id": pid})
    assert again.json()["archived_at"] == first.json()["archived_at"]
    assert postiz.deleted == [("key-acme", queued), ("key-acme", queued)]
    # Published and errored posts stay; the other brand keeps its posts and schedule.
    assert postiz.ids("key-acme") == {"pub-1", "err-1"}
    assert postiz.ids("key-other") == other_posts
    kept = await IdeaDoc.get(other_idea)
    assert kept.scheduled_at is not None and kept.calendar_event_id == other_event
    assert await CalendarEventDoc.get(other_event) is not None
    assert (await DraftDoc.get(draft.id)).model_dump() == draft_before


@pytest.mark.asyncio
async def test_a_failed_unschedule_does_not_stop_the_rest(w1, monkeypatch):
    pid = await _brand(w1, "Acme")
    first, _ = await _calendar_idea(w1, pid, "2026-12-01T10:00:00Z")
    second, _ = await _calendar_idea(w1, pid, "2026-12-02T10:00:00Z")
    real = social_service.unschedule_idea

    async def _flaky(ctx: Any, idea_id: str) -> Any:
        if idea_id == first:
            raise RuntimeError("calendar down")
        return await real(ctx, idea_id)

    monkeypatch.setattr(social_service, "unschedule_idea", _flaky)
    assert (await w1.post(ARCHIVE, params={"profile_id": pid})).status_code == 200
    assert (await IdeaDoc.get(first)).scheduled_at is not None
    assert (await IdeaDoc.get(second)).scheduled_at is None


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
    assert counts == {"due": 2, "refreshed": 1, "failed": 0, "skipped": 1}


@pytest.mark.asyncio
async def test_client_delete_post_hits_the_public_posts_route():
    import httpx
    from pocketpaw_ee.cloud.growth.social.postiz import PostizClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/gone"):
            return httpx.Response(404, text="no key-9 here")
        return httpx.Response(200, json={"error": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PostizClient("http://postiz.test/", "s3cret", client=http)
        await client.delete_post("key-9", "p/1")
        with pytest.raises(PostizError) as caught:
            await client.delete_post("key-9", "gone")
    assert (seen[0].method, seen[0].url.raw_path) == ("DELETE", b"/api/public/v1/posts/p%2F1")
    assert seen[0].headers["authorization"] == "key-9"
    assert caught.value.code == "postiz.http_error" and "key-9" not in caught.value.message
