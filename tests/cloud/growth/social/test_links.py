# tests/cloud/growth/social/test_links.py — Growth › Social tracked links: the
# public ``GET /api/v1/go/{code}`` redirect (exact 302 Location, no query
# passthrough, bare 404 for unknown and malformed codes, malformed ones refused
# before any DB read, no session needed), click counting (total and per UTC day,
# preview bots and empty UAs not counted, nothing about the requester stored),
# ``link_clicks`` tenancy, and ``create_post`` moving an idea post's first URL
# into a tracked reply part while posts without a URL or idea stay unchanged.
# Posting reuses the test_accounts fake Postiz and the test_router client.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pocketpaw_ee.cloud.growth.social import links_router
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.links import (
    CODE_RE,
    extract_urls,
    is_preview_bot,
    new_code,
)
from pocketpaw_ee.cloud.growth.social.postiz import set_production_postiz
from pocketpaw_ee.cloud.models.social_idea import SocialIdea
from pocketpaw_ee.cloud.models.social_link import SocialLink

from tests.cloud.growth.social.test_accounts import POSTS, _connected, _FakePostiz, _settings
from tests.cloud.growth.social.test_router import BASE, _client

TARGET = "https://paw.test/blog/links?ref=x"
HUMAN = {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15"}
GO_BASE = "https://go.paw.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "https://app.paw.test")
    try:
        yield
    finally:
        set_production_postiz(None)


@pytest.fixture
def postiz() -> _FakePostiz:
    fake = _FakePostiz()
    set_production_postiz(fake)  # type: ignore[arg-type]
    return fake


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def anon(mongo_db: Any):
    """Only the public router, on a bare app: no auth, license or context override."""
    app = FastAPI()
    app.include_router(links_router.router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.fixture
def link_base(monkeypatch: pytest.MonkeyPatch) -> str:
    settings = SimpleNamespace(**vars(_settings()), growth_link_base_url=GO_BASE + "/")
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)
    return GO_BASE


async def _link(workspace: str = "w1", idea: str = "i1", url: str = TARGET) -> SocialLink:
    return await social_service.create_tracked_link(workspace, "p1", idea, url)


# --- helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "ua",
    [
        "",
        None,
        "   ",
        "Twitterbot/1.0",
        "facebookexternalhit/1.1",
        "LinkedInBot/1.0",
        "Slackbot-LinkExpanding 1.0",
        "Discordbot/2.0",
        "TelegramBot (like TwitterBot)",
        "WhatsApp/2.23.20.0",
        "Mozilla/5.0 (compatible; Googlebot/2.1)",
        "Mozilla/5.0 (compatible; bingbot/2.0)",
        "Applebot/0.1",
        "SomeCrawler/1.0",
        "my-spider",
        "LinkPreview/2",
    ],
)
def test_preview_bots_and_empty_uas_are_bots(ua):
    assert is_preview_bot(ua)


def test_a_browser_is_not_a_bot():
    assert not is_preview_bot(HUMAN["User-Agent"])


def test_extract_urls():
    text = "See https://a.test/x. And (http://b.test/y?q=1), not ftp://c.test or https://."
    assert extract_urls(text) == ["https://a.test/x", "http://b.test/y?q=1"]
    assert extract_urls("no links here") == []


def test_new_codes_are_short_url_safe_and_random():
    codes = {new_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(CODE_RE.fullmatch(c) for c in codes)


# --- the redirect ---------------------------------------------------------------


async def test_redirect_is_exact_and_counts_by_day(anon, mongo_db):
    link = await _link()
    resp = await anon.get(f"/api/v1/go/{link.code}", headers=HUMAN)
    assert resp.status_code == 302
    assert resp.headers["location"] == TARGET
    assert resp.headers["cache-control"] == "no-store"

    day1 = datetime(2026, 11, 14, 23, 59, tzinfo=UTC)
    day2 = datetime(2026, 11, 15, 0, 1, tzinfo=UTC)
    assert await social_service.record_click(link.code, "Mozilla/5.0", day1) == TARGET
    assert await social_service.record_click(link.code, "Mozilla/5.0", day2) == TARGET
    assert await social_service.record_click(link.code, "Mozilla/5.0", day2) == TARGET
    doc = await SocialLink.get(link.id)
    assert doc.clicks == 4
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    expected = {"2026-11-14": 1, "2026-11-15": 2}
    expected[today] = expected.get(today, 0) + 1
    assert doc.clicks_by_day == expected


async def test_query_string_is_not_passed_on(anon, mongo_db):
    link = await _link()
    resp = await anon.get(
        f"/api/v1/go/{link.code}", params={"next": "https://evil.test", "u": "x"}, headers=HUMAN
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == TARGET


async def test_unknown_code_is_a_bare_404(anon, mongo_db):
    await _link()
    resp = await anon.get("/api/v1/go/AAAAAAAA", headers=HUMAN)
    assert resp.status_code == 404
    assert resp.content == b""
    assert "location" not in resp.headers


@pytest.mark.parametrize("code", ["abc", "abcdefghi", "abc.efgh", "abc%20efg", "abc~efgh"])
async def test_malformed_code_is_404_without_a_db_read(anon, mongo_db, monkeypatch, code):
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("malformed code reached the database")

    monkeypatch.setattr(SocialLink, "find_one", _boom)
    resp = await anon.get(f"/api/v1/go/{code}", headers=HUMAN)
    assert resp.status_code == 404
    assert resp.content == b""
    assert await social_service.record_click(code, "Mozilla/5.0") is None


@pytest.mark.parametrize("ua", ["Twitterbot/1.0", ""])
async def test_bots_follow_but_are_not_counted(anon, mongo_db, ua):
    link = await _link()
    resp = await anon.get(f"/api/v1/go/{link.code}", headers={"User-Agent": ua})
    assert resp.status_code == 302
    assert resp.headers["location"] == TARGET
    doc = await SocialLink.get(link.id)
    assert doc.clicks == 0
    assert doc.clicks_by_day == {}


async def test_a_click_stores_nothing_about_the_requester(anon, mongo_db):
    link = await _link()
    headers = {**HUMAN, "Referer": "https://x.com/acme", "X-Forwarded-For": "203.0.113.9"}
    assert (await anon.get(f"/api/v1/go/{link.code}", headers=headers)).status_code == 302
    raw = await mongo_db["growth_social_links"].find_one({"code": link.code})
    assert set(raw) == {
        "_id",
        "code",
        "workspace",
        "profile",
        "idea",
        "target_url",
        "clicks",
        "clicks_by_day",
        "createdAt",
        "updatedAt",
    }
    assert raw["clicks"] == 1
    flat = repr(raw)
    for leak in ("203.0.113.9", "Mozilla", "x.com/acme"):
        assert leak not in flat


async def test_the_route_needs_no_session(anon, mongo_db):
    [route] = links_router.router.routes
    assert route.dependant.dependencies == []
    assert links_router.router.dependencies == []
    link = await _link()
    resp = await anon.get(f"/api/v1/go/{link.code}")  # no cookie, no Authorization
    assert resp.status_code == 302


async def test_link_clicks_is_workspace_scoped(mongo_db):
    mine = await _link("w1", "idea-1")
    other = await _link("w1", "idea-1", "https://paw.test/other")
    foreign = await _link("w2", "idea-1")
    await _link("w1", "idea-2")
    for link, n in ((mine, 2), (other, 1), (foreign, 5)):
        for _ in range(n):
            await social_service.record_click(link.code, "Mozilla/5.0")
    assert await social_service.link_clicks("w1", ["idea-1", "idea-2"]) == {
        "idea-1": 3,
        "idea-2": 0,
    }
    assert await social_service.link_clicks("w2", ["idea-1"]) == {"idea-1": 5}
    assert await social_service.link_clicks("w3", ["idea-1"]) == {}


async def test_only_web_urls_are_tracked(mongo_db):
    with pytest.raises(Exception) as exc:
        await _link(url="javascript:alert(1)")
    assert getattr(exc.value, "code", "") == "social.link_url_invalid"
    assert await SocialLink.find_all().count() == 0


# --- posting ---------------------------------------------------------------------


async def _idea(client: httpx.AsyncClient) -> str:
    pid = (await client.get(f"{BASE}/profile")).json()["id"]
    idea = await SocialIdea(workspace="w1", profile=pid, format="x_post", hook="h").insert()
    return str(idea.id)


def _values(postiz: _FakePostiz) -> list[list[dict[str, Any]]]:
    return [[p["value"] for p in payload["posts"]][0] for _, payload in postiz.posted]


X = [{"account_id": "x-1"}]


async def test_post_moves_the_link_into_a_tracked_reply(w1, postiz, link_base, anon):
    await _connected(w1, postiz)
    idea_id = await _idea(w1)
    body = {
        "content": f"Shipped tracked links  {TARGET}\nTry it today",
        "parts": ["Why we built it"],
        "targets": X,
        "idea_id": idea_id,
    }
    resp = await w1.post(POSTS, json=body)
    assert resp.status_code == 200, resp.text
    [link] = await SocialLink.find_all().to_list()
    assert (link.workspace, link.idea, link.target_url) == ("w1", idea_id, TARGET)
    assert link.profile == (await w1.get(f"{BASE}/profile")).json()["id"]
    assert _values(postiz) == [
        [
            {"content": "Shipped tracked links\nTry it today", "image": []},
            {"content": "Why we built it", "image": []},
            {"content": f"Link: {GO_BASE}/{link.code}", "image": []},
        ]
    ]
    assert "paw.test/blog" not in str(postiz.posted)
    # The posted link resolves to the original URL.
    go = await anon.get(f"/api/v1/go/{link.code}", headers=HUMAN)
    assert go.headers["location"] == TARGET

    # Posting the same idea and URL again reuses the link.
    assert (await w1.post(POSTS, json=body)).status_code == 200
    assert await SocialLink.find_all().count() == 1
    assert _values(postiz)[1][-1]["content"] == f"Link: {GO_BASE}/{link.code}"


async def test_a_link_only_post_keeps_the_tracked_link_in_the_body(w1, postiz, link_base):
    await _connected(w1, postiz)
    idea_id = await _idea(w1)
    body = {"content": TARGET, "targets": X, "idea_id": idea_id}
    assert (await w1.post(POSTS, json=body)).status_code == 200, postiz.posted
    [link] = await SocialLink.find_all().to_list()
    assert _values(postiz) == [[{"content": f"{GO_BASE}/{link.code}", "image": []}]]


async def test_base_url_falls_back_to_the_public_api_route(w1, postiz, monkeypatch):
    settings = SimpleNamespace(**vars(_settings()), growth_link_base_url=None)
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", "https://api.paw.test/")
    await _connected(w1, postiz)
    idea_id = await _idea(w1)
    body = {"content": f"New {TARGET}", "targets": X, "idea_id": idea_id}
    assert (await w1.post(POSTS, json=body)).status_code == 200
    [link] = await SocialLink.find_all().to_list()
    assert _values(postiz)[0][-1]["content"] == f"Link: https://api.paw.test/api/v1/go/{link.code}"


@pytest.mark.parametrize("with_idea", [False, True])
async def test_posts_without_a_url_or_idea_are_unchanged(w1, postiz, link_base, with_idea):
    await _connected(w1, postiz)
    idea_id = await _idea(w1)
    content = f"Ship day  {TARGET}" if not with_idea else "Ship day, no link"
    body: dict[str, Any] = {"content": content, "parts": ["more"], "targets": X}
    if with_idea:
        body["idea_id"] = idea_id
    assert (await w1.post(POSTS, json=body)).status_code == 200
    assert _values(postiz) == [
        [{"content": content, "image": []}, {"content": "more", "image": []}]
    ]
    assert await SocialLink.find_all().count() == 0


async def test_a_refused_post_mints_no_link(w1, postiz, link_base):
    await _connected(w1, postiz)
    idea_id = await _idea(w1)
    body = {"content": f"Hi {TARGET}", "targets": [{"account_id": "li-1"}], "idea_id": idea_id}
    assert (await w1.post(POSTS, json=body)).status_code == 422
    assert await SocialLink.find_all().count() == 0
    assert postiz.posted == []
