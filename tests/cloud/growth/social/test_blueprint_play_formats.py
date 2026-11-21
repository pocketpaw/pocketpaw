# tests/cloud/growth/social/test_blueprint_play_formats.py — which play a new idea
# is made for (``SocialIdea.play_id``, stamped on every new idea) and the
# per-format numbers on each play of ``GET /blueprint/live``. Same harness as
# test_blueprint_live.py: approved units over mongomock-backed Beanie and fake
# ``IdeasFn`` / ``MemeFn``. No network, no model.
#
# Covers: one acting play stamps every idea on its platform (an idea with no
# platform reads as X); two on one platform go to the one with the most posts left
# this ISO week, each pick counting, last week's ideas not, and a tie going to the
# core play; no acting play on the platform (or no Blueprint) is None; the daily
# drafts, memes and pulse picks stamp too. Live: a stamped idea counts toward
# exactly its play, an old one keeps the single-play fallback; formats carry posts,
# avg and a suggestion at the 1.3 / 0.7 edges, none under 5 posts, with the reason
# text; ``play_id`` crosses model, domain and the idea response.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.growth.social import live
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.domain import GeneratedIdea
from pocketpaw_ee.cloud.growth.social.dto import (
    GenerateIdeasRequest,
    MakeMemeRequest,
    UsePulseSuggestionRequest,
)
from pocketpaw_ee.cloud.growth.social.ideas import set_production_ideas_fn
from pocketpaw_ee.cloud.growth.social.memes import MadeMeme, set_production_meme_fn
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric as MetricDoc
from pocketpaw_ee.cloud.models.social_pulse import SocialPulse as PulseDoc

from tests.cloud.growth.social.test_blueprint import _profile
from tests.cloud.growth.social.test_blueprint_live import NOW as LIVE_NOW
from tests.cloud.growth.social.test_blueprint_live import (
    NULL,
    _blueprint,
    _ctx,
    _live,
    _page_row,
    _page_snap,
    _snap,
)
from tests.cloud.growth.social.test_blueprint_live import _play as _live_play
from tests.cloud.growth.social.test_router import MEME_SVG, _client


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_ideas_fn():
    set_production_ideas_fn(None)
    set_production_meme_fn(None)
    try:
        yield
    finally:
        set_production_ideas_fn(None)
        set_production_meme_fn(None)


def _play(status: str = "core", per_week: int = 3) -> dict:
    return {**_live_play(status), "posts_per_week": per_week}


class _Ideas:
    """Returns ``count`` X ideas on ``platform`` (no digits, so daily drafts keep them)."""

    def __init__(self, platform: str = "x") -> None:
        self.platform = platform

    async def __call__(self, profile, count, recent_hooks, **_: Any) -> tuple[GeneratedIdea, ...]:
        return tuple(
            GeneratedIdea(format="x_post", hook=f"Take {'abcdef'[i]}", platform=self.platform)
            for i in range(count)
        )


async def _brand(w1, plays: dict[str, dict] | None) -> str:
    pid = await _profile(onboarding_completed_at=datetime.now(UTC), active=True)
    if plays is not None:
        await _blueprint(w1, pid, plays)
    return pid


async def _generate(pid: str, count: int, platform: str = "x") -> list[str | None]:
    set_production_ideas_fn(_Ideas(platform))
    out = await social_service.generate_ideas(_ctx(), GenerateIdeasRequest(count=count), pid)
    return [i.play_id for i in out.items]


async def _stamped(pid: str, play_id: str, n: int, created: datetime | None = None) -> None:
    for i in range(n):
        if created is None:
            await IdeaDoc(
                workspace="w1", profile=pid, format="x_post", hook=f"h{i}", play_id=play_id
            ).insert()
        else:  # insert() would overwrite createdAt
            await IdeaDoc.get_pymongo_collection().insert_one(
                {"workspace": "w1", "profile": pid, "format": "x_post", "hook": f"old{i}",
                 "platform": "x", "status": "new", "play_id": play_id,
                 "createdAt": created, "updatedAt": created}
            )  # fmt: skip


# --- stamping ------------------------------------------------------------------------------


async def test_one_acting_play_stamps_every_idea_on_its_platform(w1):
    pid = await _brand(w1, {"play:x-organic": _play(), "play:x-authority": _play("parked")})
    assert await _generate(pid, 2) == ["play:x-organic", "play:x-organic"]
    # An idea with no platform reads as X, as live does.
    assert await _generate(pid, 1, platform="") == ["play:x-organic"]
    stored = await IdeaDoc.find({"workspace": "w1", "profile": pid}).to_list()
    assert {d.play_id for d in stored} == {"play:x-organic"}


async def test_two_plays_pick_the_most_left_then_the_core_play(w1):
    pid = await _brand(
        w1, {"play:x-organic": _play("core", 3), "play:x-authority": _play("testing", 3)}
    )
    await _stamped(pid, "play:x-organic", 2)  # organic: 1 left, authority: 3 left
    last_week = datetime.now(UTC) - timedelta(days=8)
    await _stamped(pid, "play:x-authority", 2, created=last_week)  # never counts
    # authority (3 v 1), authority (2 v 1), then a 1 v 1 tie goes to the core play.
    assert await _generate(pid, 3) == ["play:x-authority", "play:x-authority", "play:x-organic"]


async def test_a_tie_between_two_testing_plays_takes_the_lower_id(w1):
    pid = await _brand(
        w1, {"play:x-organic": _play("testing"), "play:x-authority": _play("testing")}
    )
    assert await _generate(pid, 1) == ["play:x-authority"]


async def test_no_acting_play_on_the_platform_is_none(w1):
    pid = await _brand(w1, {"play:reddit-community": _play()})
    assert await _generate(pid, 1) == [None]
    assert await _generate(pid, 1, platform="reddit") == ["play:reddit-community"]
    bare = await _brand(w1, None)  # no approved Blueprint at all
    assert await _generate(bare, 1) == [None]


async def test_daily_drafts_stamp_their_play(w1):
    pid = await _brand(w1, {"play:x-organic": _play(per_week=5)})
    set_production_ideas_fn(_Ideas())
    made = await social_service.draft_daily_for_profile("w1", pid, now=datetime.now(UTC))
    assert made and {i.play_id for i in made} == {"play:x-organic"}


async def test_a_meme_is_stamped_with_its_play(w1):
    async def make(*_: Any) -> MadeMeme:
        return MadeMeme(hook="Nobody: / Me:", caption="Post", why="Relatable", svg=MEME_SVG)

    set_production_meme_fn(make)
    pid = await _brand(w1, {"play:x-organic": _play(), "play:reddit-community": _play()})
    made = await social_service.make_meme(_ctx(), MakeMemeRequest(platform="reddit"), pid)
    assert made.play_id == "play:reddit-community"


async def test_a_pulse_pick_is_stamped_with_its_play(w1):
    pid = await _brand(w1, {"play:x-organic": _play()})
    suggestions = [{"platform": "x", "hook": "A"}, {"platform": "reddit", "hook": "B"}]
    await PulseDoc(
        workspace="w1", profile=pid, themes=[{"title": "T", "suggestions": suggestions}]
    ).insert()
    pick = UsePulseSuggestionRequest
    x = await social_service.use_pulse_suggestion(_ctx(), pick(theme=0, suggestion=0), pid)
    reddit = await social_service.use_pulse_suggestion(_ctx(), pick(theme=0, suggestion=1), pid)
    assert (x.play_id, reddit.play_id) == ("play:x-organic", None)  # no Reddit play


async def test_play_id_crosses_model_domain_and_response(w1):
    doc = IdeaDoc(workspace="w1", profile="p", format="x_post", hook="h", play_id="play:x-organic")
    await doc.insert()
    domain = social_service._idea_to_domain(doc)
    assert domain.play_id == "play:x-organic"
    assert social_service._idea_to_response(domain).play_id == "play:x-organic"
    old = IdeaDoc(workspace="w1", profile="p", format="x_post", hook="h")
    assert social_service._idea_to_domain(old).play_id is None
    got = await social_service.get_idea(_ctx(), str(doc.id))
    assert got.model_dump()["play_id"] == "play:x-organic"


# --- live attribution and formats ----------------------------------------------------------


async def _posted(pid: str, fmt: str, likes: int, play_id: str | None = None, platform="x"):
    doc = IdeaDoc(
        workspace="w1",
        profile=pid,
        format=fmt,
        hook="h",
        platform=platform,
        published_at=LIVE_NOW - timedelta(days=8),
        play_id=play_id,
    )
    await doc.insert()
    await MetricDoc(
        workspace="w1",
        idea=str(doc.id),
        x_post_id="1",
        age_bucket=7,
        captured_at=LIVE_NOW - timedelta(days=1),
        likes=likes,
    ).insert()


def _eng(status: str = "core") -> dict:
    return _live_play(status, key="engagement_per_post")


async def test_a_stamped_idea_counts_toward_exactly_its_play(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _eng(), "play:x-authority": _eng("testing")})
    await _posted(pid, "x_post", 10, "play:x-organic")
    await _posted(pid, "x_post", 20, "play:x-authority")
    await _posted(pid, "x_post", 99)  # old idea: two acting plays, so neither gets it
    await _posted(pid, "reddit_post", 50, "play:x-organic", platform="reddit")  # wrong platform
    plays = (await _live(pid))["plays"]
    assert plays["play:x-organic"]["value"] == 10.0
    assert plays["play:x-organic"]["posts_counted"] == 1
    assert plays["play:x-authority"]["value"] == 20.0


async def test_an_old_idea_still_counts_toward_the_single_play(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _eng(), "play:x-authority": _eng("parked")})
    await _posted(pid, "x_post", 10)
    await _posted(pid, "x_post", 30, "play:x-authority")  # stamped while it was acting
    plays = (await _live(pid))["plays"]
    assert plays["play:x-organic"]["value"] == 10.0  # the fallback; the stamped one is not
    assert plays["play:x-authority"]["value"] == 30.0


async def test_a_postiz_post_follows_its_ideas_play_id(w1):
    """A Page post of an idea stamped for the LinkedIn play counts toward it, even
    with the play parked (so the no-play_id fallback would not) and with the idea
    never published on X; it brings the idea's format."""
    pid = await _profile()
    await _blueprint(w1, pid, {"play:linkedin-content": _eng("parked")})
    idea = IdeaDoc(
        workspace="w1", profile=pid, format="wall_of_text", hook="h", platform="linkedin",
        play_id="play:linkedin-content",
    )  # fmt: skip
    await idea.insert()
    await _page_row(str(idea.id), pid, "p-1", 7, 8, impressions=40, likes=6)
    play = (await _live(pid))["plays"]["play:linkedin-content"]
    assert (play["value"], play["posts_counted"]) == (6.0, 1)
    assert [(f["name"], f["posts"], f["avg"]) for f in play["formats"]] == [
        ("wall_of_text", 1, 6.0)
    ]


async def test_a_stamped_x_idea_cross_posted_to_linkedin_counts_on_each(w1):
    pid = await _profile()
    await _blueprint(
        w1, pid, {"play:x-organic": _live_play(), "play:linkedin-content": _live_play()}
    )
    idea = IdeaDoc(
        workspace="w1", profile=pid, format="x_post", hook="h", platform="x",
        published_at=LIVE_NOW - timedelta(days=2), play_id="play:x-organic",
    )  # fmt: skip
    await idea.insert()
    await _snap(str(idea.id), 1, impressions=300)
    await _page_snap(str(idea.id), pid, "p-lp", 3, 4)
    plays = (await _live(pid))["plays"]
    assert plays["play:x-organic"]["value"] == 300.0
    # Its play_id is an X play, so on LinkedIn it counts as unstamped: the one play.
    assert plays["play:linkedin-content"]["value"] == 1520.0


async def test_formats_carry_numbers_and_suggestions_at_the_edges(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _live_play()})  # an impressions KPI
    for _ in range(5):
        await _posted(pid, "x_thread", 13)
        await _posted(pid, "wall_of_text", 7)
    for _ in range(3):
        await _posted(pid, "meme", 10)
    await IdeaDoc(  # published, no 7-day snapshot yet: listed, not measured
        workspace="w1", profile=pid, format="slideshow", hook="h", platform="x",
        published_at=LIVE_NOW - timedelta(days=1),
    ).insert()  # fmt: skip
    play = (await _live(pid))["plays"]["play:x-organic"]
    # Play average: (65 + 35 + 30) / 13 = 10, so the ratios are exactly 1.3 and 0.7.
    assert play["formats"] == [
        {"name": "wall_of_text", "posts": 5, "avg": 7.0, "suggestion": "drop",
         "reason": "5 posts, 0.7x the play average"},
        {"name": "x_thread", "posts": 5, "avg": 13.0, "suggestion": "double_down",
         "reason": "5 posts, 1.3x the play average"},
        {"name": "meme", "posts": 3, "avg": 10.0, "suggestion": None,
         "reason": "3 posts, needs 5 for a call"},
        {"name": "slideshow", "posts": 0, "avg": None, "suggestion": None,
         "reason": "0 posts, needs 5 for a call"},
    ]  # fmt: skip


async def test_a_play_with_no_counted_ideas_has_no_formats(w1):
    pid = await _profile()
    await _blueprint(w1, pid, {"play:x-organic": _eng(), "play:x-authority": _eng("testing")})
    await _posted(pid, "x_post", 10)  # old idea, two acting plays
    assert (await _live(pid))["plays"]["play:x-organic"] == {**NULL, "formats": []}


@pytest.mark.parametrize(
    ("posts", "avg", "play_avg", "expected"),
    [
        (5, 13.0, 10.0, ("double_down", "5 posts, 1.3x the play average")),
        (5, 7.0, 10.0, ("drop", "5 posts, 0.7x the play average")),
        (5, 12.9, 10.0, (None, "5 posts, 1.29x the play average")),
        (5, 7.1, 10.0, (None, "5 posts, 0.71x the play average")),
        # Ratios that are not exact in floating point still hit the edge.
        (5, 117 / 5, 126 / 7, ("double_down", "5 posts, 1.3x the play average")),
        (5, 21 / 5, 36 / 6, ("drop", "5 posts, 0.7x the play average")),
        (5, 12.96, 10.0, ("double_down", "5 posts, 1.3x the play average")),
        (5, 12.94, 10.0, (None, "5 posts, 1.29x the play average")),
        (6, 21.0, 10.0, ("double_down", "6 posts, 2.1x the play average")),
        (4, 30.0, 10.0, (None, "4 posts, needs 5 for a call")),
        (1, 0.0, 10.0, (None, "1 post, needs 5 for a call")),
        (5, 3.0, 0.0, (None, None)),
        (5, 3.0, None, (None, None)),
    ],
)
def test_suggest_edges(posts, avg, play_avg, expected):
    assert live.suggest(posts, avg, play_avg) == expected
    assert "—" not in str(expected) and "–" not in str(expected)
