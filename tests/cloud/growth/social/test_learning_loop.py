# tests/cloud/growth/social/test_learning_loop.py — the Growth › Social learning
# loop: idea tags (pillar, hook_type, experiment_id), experiments, daily-draft arm
# alternation, the weekly review and founder-approved learnings. Same harness as
# test_router.py / test_daily_drafts.py: the social router over mongomock-backed
# Beanie, per-workspace clients and fake seams. No network, no model.
#
# Covers: tags parsed (capped), stored, edited and round-tripped through both
# mappers; one running experiment per profile (409); experiment fields have no
# route that changes them; close only from running, and a keep/no-difference close
# proposes one learning; arm alternation (fewer first, tie A) and stamping; the
# review's math, its min-posts gate and its workspace filter; only approved
# learnings reach the ideas prompt; learning status transitions; propose stores at
# most three and drops dashed ones; tenancy on every read.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.domain import GeneratedIdea
from pocketpaw_ee.cloud.growth.social.ideas import (
    GROWTH_SOCIAL_IDEAS_PROMPT,
    build_ideas_prompt,
    parse_ideas_response,
    set_production_ideas_fn,
)
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc

from tests.cloud.growth.social.test_daily_drafts import _FakeIdeas, _product
from tests.cloud.growth.social.test_router import BASE, IDEAS, _client


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
    set_production_ideas_fn(None)
    try:
        yield
    finally:
        set_production_ideas_fn(None)


# --- tags ----------------------------------------------------------------------------------


def test_ideas_prompt_asks_for_both_tags_and_the_parser_caps_them():
    assert "pillar" in GROWTH_SOCIAL_IDEAS_PROMPT and "hook_type" in GROWTH_SOCIAL_IDEAS_PROMPT
    text = (
        '{"ideas": [{"format": "x_post", "hook": "Ship it", "pillar": "  build   in public  ",'
        ' "hook_type": "' + "q" * 90 + '"}, {"format": "x_post", "hook": "Two", "pillar": 7}]}'
    )
    one, two = parse_ideas_response(text, 5, "x")
    assert one.pillar == "build in public"
    assert one.hook_type == "q" * 40
    assert two.pillar == "" and two.hook_type == ""


async def test_tags_round_trip_through_both_mappers(mongo_db):
    doc = IdeaDoc(
        workspace="w1",
        profile="p",
        format="x_post",
        hook="h",
        pillar="speed",
        hook_type="question",
        experiment_id="e1",
    )
    await doc.insert()
    domain = social_service._idea_to_domain(await IdeaDoc.get(doc.id))
    assert (domain.pillar, domain.hook_type, domain.experiment_id) == ("speed", "question", "e1")
    out = social_service._idea_to_response(domain)
    assert (out.pillar, out.hook_type, out.experiment_id) == ("speed", "question", "e1")


async def test_generated_tags_are_stored_and_editable_through_the_idea_patch(w1):
    await _product(w1)
    fake = _FakeIdeas(
        (GeneratedIdea(format="x_post", hook="A", pillar="speed", hook_type="story"),)
    )
    set_production_ideas_fn(fake)
    resp = await w1.post(f"{IDEAS}/generate", json={"count": 1, "platform": "x"})
    assert resp.status_code == 200, resp.text
    idea = resp.json()["items"][0]
    assert (idea["pillar"], idea["hook_type"]) == ("speed", "story")

    resp = await w1.patch(
        f"{IDEAS}/{idea['id']}", json={"pillar": "  docs  ", "hook_type": "x" * 60}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pillar"] == "docs"
    assert resp.json()["hook_type"] == "x" * 40
