# tests/cloud/growth/social/test_replies.py — Growth › Social reply cards: the
# profile settings, the reply sweep (``run_reply_drafts``), its routes
# (``/growth/social/replies``) and its cron. Same harness as test_router.py: the
# social router over mongomock-backed Beanie, per-workspace clients, and fake X
# reader / ``ReplyDraftFn`` / ``ChangesFn`` seams that record every call. No
# network, no model.
#
# Covers the never-posts guarantee (replies.py imports no publishing client and
# makes no HTTP write; x_api exposes only a GET reader), a refused X read giving
# zero cards, the per-day cap, dedupe and the unique index, our own posts skipped,
# dash / invented-number drafts dropped, intent-link encoding and the 280 cap, the
# edit re-check (422) and intent rebuild, status updates, tenancy, inactive and
# source-less profiles skipped, the settings round trip and the worker's cron.

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pocketpaw_ee.cloud.growth.social import changes as social_changes
from pocketpaw_ee.cloud.growth.social import replies as social_replies
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social import x_api
from pocketpaw_ee.cloud.growth.social.changes import Change, set_production_changes_fn
from pocketpaw_ee.cloud.growth.social.domain import GeneratedReply, ReplyCandidate
from pocketpaw_ee.cloud.growth.social.replies import (
    handle_queries,
    intent_url,
    set_production_reply_draft_fn,
)
from pocketpaw_ee.cloud.growth.social.x_api import set_x_read_fn
from pocketpaw_ee.cloud.models.reply_draft import ReplyDraft as ReplyDoc
from pymongo.errors import DuplicateKeyError

from tests.cloud.growth.social.test_router import _COMPLETE, BASE, PROFILE, _client

REPLIES = f"{BASE}/replies"


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
    # Clear before too: anything that ran ``mount_cloud`` on this worker installed real fns.
    for reset in (set_production_reply_draft_fn, set_production_changes_fn, set_x_read_fn):
        reset(None)
    try:
        yield
    finally:
        for reset in (set_production_reply_draft_fn, set_production_changes_fn, set_x_read_fn):
            reset(None)


POSTS = [
    ("101", "alice", "How do people export CSV from their dashboards?"),
    ("102", "bob", "We hit 40 users this week"),
    ("103", "carol", "Any tips for onboarding emails?"),
]


class _FakeX:
    """Answers every search with ``posts``; None (a refused read) when ``refuse``."""

    def __init__(self, posts: list[tuple[str, str, str]] = POSTS, refuse: bool = False) -> None:
        self.posts = posts
        self.refuse = refuse
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, workspace_id: str, path: str, params: dict | None = None):
        self.calls.append((workspace_id, path, dict(params or {})))
        if self.refuse:
            return None
        return {
            "data": [{"id": i, "author_id": f"u-{a}", "text": t} for i, a, t in self.posts],
            "includes": {"users": [{"id": f"u-{a}", "username": a} for _, a, _ in self.posts]},
        }


class _FakeDrafts:
    def __init__(self, texts: dict[str, str] | None = None) -> None:
        self.texts = texts or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, profile, candidates: list[ReplyCandidate], *, changes: list[Change]):
        self.calls.append({"profile": profile, "candidates": candidates, "changes": changes})
        return tuple(
            GeneratedReply(c.post_id, self.texts.get(c.post_id, f"Nice one @{c.author}, try it"))
            for c in candidates
        )


def _install(x: _FakeX | None = None, drafts: _FakeDrafts | None = None):
    x, drafts = x or _FakeX(), drafts or _FakeDrafts()
    set_x_read_fn(x)
    set_production_reply_draft_fn(drafts)
    return x, drafts


async def _product(
    client: AsyncClient,
    *,
    active: bool = True,
    complete: bool = True,
    handles: tuple[str, ...] = ("alice", "bob", "carol"),
    queries: tuple[str, ...] = ("csv export",),
    per_day: int = 15,
) -> str:
    body = {
        **_COMPLETE,
        "active": active,
        "reply_watch_handles": list(handles),
        "reply_queries": list(queries),
        "reply_cards_per_day": per_day,
    }
    resp = await client.put(PROFILE, json=body)
    assert resp.status_code == 200, resp.text
    if complete:
        assert (await client.post(f"{PROFILE}/complete")).status_code == 200
    return resp.json()["id"]


async def _cards(client: AsyncClient, **params: str) -> list[dict[str, Any]]:
    resp = await client.get(REPLIES, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# --- the never-posts guarantee -------------------------------------------------------------


def _ast_names(path: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(Path(path).read_text())
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return modules, names


_WRITE_NAMES = {"post", "put", "patch", "delete", "create_post", "PostizClient", "resolve_postiz"}


def test_replies_module_imports_no_posting_client():
    modules, names = _ast_names(social_replies.__file__)
    assert not [m for m in modules if "postiz" in m or "httpx" in m or m.endswith("service")]
    assert not names & _WRITE_NAMES
    assert "postiz" not in Path(social_replies.__file__).read_text().lower()


def test_x_api_exposes_no_write_function():
    _, names = _ast_names(x_api.__file__)
    assert not names & _WRITE_NAMES
    public = {n for n in dir(x_api) if callable(getattr(x_api, n)) and not n.startswith("_")}
    assert public - {"XReadFn", "x_read", "set_x_read_fn", "resolve_x_read_fn"} <= {
        "Awaitable",
        "Callable",
    } | {n for n in public if getattr(getattr(x_api, n), "__module__", "") != x_api.__name__}


# --- the sweep ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_drafts_cards_from_one_batched_handle_read_plus_each_query(w1):
    x, drafts = _install()
    pid = await _product(w1)

    counts = await social_service.run_reply_drafts()

    assert counts == {"profiles": 1, "drafted": 3, "dropped": 0, "failed": 0}
    queries = [c[2]["query"] for c in x.calls]
    assert queries == [
        "(from:alice OR from:bob OR from:carol) -is:retweet",
        "csv export -is:retweet",
    ]
    assert all(c[0] == "w1" and c[1] == "/2/tweets/search/recent" for c in x.calls)
    assert x.calls[0][2]["start_time"].endswith("Z")
    cards = await _cards(w1, profile_id=pid)
    assert len(cards) == 3 and {c["status"] for c in cards} == {"new"}
    card = next(c for c in cards if c["target_post_id"] == "101")
    assert card["author"] == "alice"
    assert card["target_url"] == "https://x.com/alice/status/101"
    assert card["context"] == POSTS[0][2]
    assert card["platform"] == "x" and card["profile_id"] == pid
    assert card["intent_url"] == intent_url("101", card["draft_text"])
    assert len(drafts.calls) == 1


@pytest.mark.asyncio
async def test_a_refused_x_read_makes_zero_cards_and_never_raises(w1):
    _, drafts = _install(x=_FakeX(refuse=True))
    await _product(w1)

    counts = await social_service.run_reply_drafts()

    assert counts == {"profiles": 1, "drafted": 0, "dropped": 0, "failed": 0}
    assert drafts.calls == []
    assert await _cards(w1) == []


@pytest.mark.asyncio
async def test_per_day_cap_holds_and_a_full_day_reads_nothing(w1):
    x, _ = _install()
    await _product(w1, per_day=2)

    assert (await social_service.run_reply_drafts())["drafted"] == 2
    reads = len(x.calls)
    assert (await social_service.run_reply_drafts())["drafted"] == 0
    assert len(x.calls) == reads
    assert len(await _cards(w1)) == 2


@pytest.mark.asyncio
async def test_a_post_never_gets_two_cards(w1):
    _install()
    pid = await _product(w1)

    await social_service.run_reply_drafts()
    await social_service.run_reply_drafts()

    assert sorted(c["target_post_id"] for c in await _cards(w1)) == ["101", "102", "103"]
    with pytest.raises(DuplicateKeyError):
        await ReplyDoc(workspace="w1", profile=pid, target_post_id="101").insert()


@pytest.mark.asyncio
async def test_our_own_posts_get_no_card(w1, monkeypatch):
    _install()
    await _product(w1)

    async def own(workspace_id: str, profile: str) -> set[str]:
        return {"bob"}

    monkeypatch.setattr(social_service, "_own_x_handles", own)
    await social_service.run_reply_drafts()

    assert sorted(c["author"] for c in await _cards(w1)) == ["alice", "carol"]


@pytest.mark.asyncio
async def test_dash_and_invented_number_drafts_are_dropped(w1):
    texts = {
        "101": "Use the export button — easy",
        "102": "Congrats on 40",
        "103": "Try 300 emails",
    }
    _install(drafts=_FakeDrafts(texts))
    await _product(w1)

    counts = await social_service.run_reply_drafts()

    assert counts["drafted"] == 1 and counts["dropped"] == 2
    assert [c["target_post_id"] for c in await _cards(w1)] == ["102"]


@pytest.mark.asyncio
async def test_numbers_from_recent_changes_pass(w1):
    _install(drafts=_FakeDrafts({"103": "Our v0.7.0 release adds this"}))
    set_production_changes_fn(_changes)
    body = {**_COMPLETE, "github_repos": ["acme/app"]}
    assert (await w1.put(PROFILE, json=body)).status_code == 200
    await _product(w1)

    await social_service.run_reply_drafts()

    assert "103" in {c["target_post_id"] for c in await _cards(w1)}


async def _changes(repos: list[str], *, since: datetime) -> list[Change]:
    return [Change("release", "v0.7.0", "https://github.com/acme/app/releases/tag/v0.7.0", "")]


@pytest.mark.asyncio
async def test_inactive_unonboarded_and_sourceless_profiles_are_skipped(w1, w2):
    _install()
    await _product(w1, active=False)
    await _product(w2, handles=(), queries=())

    assert (await social_service.run_reply_drafts())["profiles"] == 0
    assert (
        await social_service.draft_replies_for_profile("w1", (await w1.get(PROFILE)).json()["id"])
        == []
    )


@pytest.mark.asyncio
async def test_no_drafter_sweeps_nothing(w1):
    set_x_read_fn(_FakeX())
    await _product(w1)
    assert (await social_service.run_reply_drafts())["profiles"] == 0


# --- intent links -------------------------------------------------------------------------


def test_intent_url_encodes_everything_and_caps_at_280():
    text = "Try A & B #tips \U0001f680 50%/x?y=z"
    url = intent_url("123", text)
    assert url.startswith("https://x.com/intent/tweet?in_reply_to=123&text=")
    raw = url.split("&text=", 1)[1]
    assert " " not in raw and "&" not in raw and "#" not in raw and "?" not in raw
    assert "%20" in raw and "%26" in raw and "%23" in raw and "%F0%9F%9A%80" in raw
    assert parse_qs(urlsplit(url).query)["text"] == [text]
    long = parse_qs(urlsplit(intent_url("1", "a" * 300)).query)["text"][0]
    assert len(long) == 280
    with pytest.raises(ValueError):
        intent_url("12&text=x", "hi")


def test_handle_queries_stay_under_the_search_limit_and_cover_every_handle():
    handles = [f"handle_{i:02d}abcdef" for i in range(30)]
    queries = handle_queries(handles)
    assert len(queries) > 1
    assert all(len(q) <= social_replies.SEARCH_QUERY_CHARS for q in queries)
    assert sum(q.count("from:") for q in queries) == 30


# --- routes -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_recomputes_the_intent_link_and_rejects_rule_breaks(w1):
    _install()
    await _product(w1)
    await social_service.run_reply_drafts()
    card = next(c for c in await _cards(w1) if c["target_post_id"] == "102")

    ok = await w1.patch(
        f"{REPLIES}/{card['id']}", json={"draft_text": "Big week at 40, nice & well done"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["intent_url"] == intent_url("102", "Big week at 40, nice & well done")

    dash = await w1.patch(f"{REPLIES}/{card['id']}", json={"draft_text": "Nice — well done"})
    assert dash.status_code == 422 and "dash" in dash.text
    number = await w1.patch(f"{REPLIES}/{card['id']}", json={"draft_text": "Now 500 users"})
    assert number.status_code == 422 and "500" in number.text
    too_long = await w1.patch(f"{REPLIES}/{card['id']}", json={"draft_text": "a" * 281})
    assert too_long.status_code == 422
    assert (await _cards(w1))[0]["draft_text"] != "Nice — well done"


@pytest.mark.asyncio
async def test_status_moves_to_posted_or_skipped_and_filters(w1):
    _install()
    await _product(w1)
    await social_service.run_reply_drafts()
    a, b, _ = await _cards(w1)

    assert (await w1.patch(f"{REPLIES}/{a['id']}", json={"status": "posted"})).status_code == 200
    assert (await w1.patch(f"{REPLIES}/{b['id']}", json={"status": "skipped"})).status_code == 200
    assert (await w1.patch(f"{REPLIES}/{b['id']}", json={"status": "sent"})).status_code == 422
    assert (await w1.patch(f"{REPLIES}/{b['id']}", json={})).status_code == 422
    assert [c["id"] for c in await _cards(w1, status="posted")] == [a["id"]]
    assert [c["id"] for c in await _cards(w1, status="skipped")] == [b["id"]]


@pytest.mark.asyncio
async def test_another_workspace_never_sees_or_edits_the_cards(w1, w2):
    _install()
    await _product(w1)
    await _product(w2, handles=("dave",), queries=())
    await social_service.draft_replies_for_profile("w1", (await w1.get(PROFILE)).json()["id"])
    card = (await _cards(w1))[0]

    assert await _cards(w2) == []
    edit = await w2.patch(f"{REPLIES}/{card['id']}", json={"status": "skipped"})
    assert edit.status_code == 404
    assert (await w2.patch(f"{REPLIES}/not-an-id", json={"status": "skipped"})).status_code == 404


@pytest.mark.asyncio
async def test_manual_route_drafts_now_and_refuses_when_it_cannot(w1):
    resp = await w1.post(f"{REPLIES}/draft")
    assert resp.status_code == 503
    _install()
    await _product(w1, active=False)
    assert (await w1.post(f"{REPLIES}/draft")).status_code == 409
    assert (
        await w1.put(
            PROFILE, json={"active": True, "reply_watch_handles": None, "reply_queries": None}
        )
    ).status_code == 200
    resp = await w1.post(f"{REPLIES}/draft")
    assert resp.status_code == 409 and "social.no_reply_sources" in resp.text
    assert (await w1.put(PROFILE, json={"reply_queries": ["csv"]})).status_code == 200
    resp = await w1.post(f"{REPLIES}/draft")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 3


# --- profile settings ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_settings_round_trip_and_reset(w1):
    fresh = (await w1.put(PROFILE, json={"company_name": "Acme"})).json()
    assert (fresh["reply_watch_handles"], fresh["reply_queries"], fresh["reply_cards_per_day"]) == (
        [],
        [],
        15,
    )
    body = {
        "reply_watch_handles": ["@Alice", " bob ", "alice", "x_1"],
        "reply_queries": [" csv export ", "", "csv export"],
        "reply_cards_per_day": 7,
    }
    saved = (await w1.put(PROFILE, json=body)).json()
    assert saved["reply_watch_handles"] == ["Alice", "bob", "x_1"]
    assert saved["reply_queries"] == ["csv export"]
    assert saved["reply_cards_per_day"] == 7
    kept = (await w1.put(PROFILE, json={"company_name": "Acme 2"})).json()
    assert (
        kept["reply_watch_handles"] == ["Alice", "bob", "x_1"] and kept["reply_cards_per_day"] == 7
    )
    reset = (
        await w1.put(
            PROFILE,
            json={"reply_watch_handles": None, "reply_queries": None, "reply_cards_per_day": None},
        )
    ).json()
    assert (reset["reply_watch_handles"], reset["reply_queries"], reset["reply_cards_per_day"]) == (
        [],
        [],
        15,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"reply_watch_handles": ["has space"]},
        {"reply_watch_handles": ["a" * 16]},
        {"reply_watch_handles": [f"h{i}" for i in range(31)]},
        {"reply_queries": ["q" * 201]},
        {"reply_queries": [f"q{i}" for i in range(6)]},
        {"reply_cards_per_day": 0},
        {"reply_cards_per_day": 26},
    ],
)
async def test_bad_reply_settings_are_422(w1, body):
    assert (await w1.put(PROFILE, json=body)).status_code == 422


# --- the cron -----------------------------------------------------------------------------


def test_worker_registers_the_reply_cron_at_05_45():
    from pocketpaw_ee.cloud.growth.domain import GROWTH_SOCIAL_REPLY_DRAFTS_JOB_NAME
    from pocketpaw_ee.cloud.growth.worker import WorkerSettings, social_reply_drafts

    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    job = jobs[GROWTH_SOCIAL_REPLY_DRAFTS_JOB_NAME]
    assert GROWTH_SOCIAL_REPLY_DRAFTS_JOB_NAME == "growth.social_reply_drafts"
    assert job.coroutine is social_reply_drafts
    assert (job.hour, job.minute) == (5, 45)
    assert job.unique is True and job.run_at_startup is False


@pytest.mark.asyncio
async def test_worker_job_installs_the_production_fns_when_missing(monkeypatch):
    from pocketpaw_ee.cloud.growth import worker

    seen: dict[str, Any] = {}

    async def fake_run(now: datetime | None = None) -> dict[str, int]:
        seen["drafts"] = social_replies.resolve_reply_draft_fn()
        seen["changes"] = social_changes.resolve_changes_fn()
        return {"profiles": 0}

    monkeypatch.setattr(social_service, "run_reply_drafts", fake_run)
    assert await worker.social_reply_drafts({}) == {"profiles": 0}
    assert seen["drafts"] is social_replies.agent_draft_replies
    assert seen["changes"] is social_changes.github_changes
    assert datetime.now(UTC)  # keeps UTC import meaningful for fake_run's signature
