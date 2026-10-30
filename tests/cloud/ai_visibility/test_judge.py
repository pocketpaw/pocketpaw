# tests/cloud/ai_visibility/test_judge.py — presence, competitors, source types and
# the decision-model judgement.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). Pins: an absent business is
# False with NO model call; exact name / alias / domain is True; a fuzzy near miss
# is None and only then reaches ``make_confirm``; competitor matching; the URL ->
# source_type table; Clef answers parsed from the Jev shape (and the Workers AI
# envelope); the fallback judges below the 0.7 floor and when the primary fails.
from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.cloud.ai_visibility.domain import Business, Competitor
from pocketpaw_ee.cloud.ai_visibility.judge import (
    ClefFlash,
    HaikuFallback,
    competitors_mentioned,
    judge_mention,
    make_confirm,
    mentioned,
    normalize_name,
    passage_for,
    source_type,
    url_names_business,
)

JOES = Business(
    name="Joe's Pizza LLC",
    business_type="pizza restaurant",
    aliases=("Joes Pizza ATX",),
    domain="https://www.joespizzaatx.com",
    competitors=(
        Competitor("Home Slice Pizza", domain="homeslicepizza.com"),
        Competitor("Via 313", aliases=("Via313",)),
    ),
)


class FakeModel:
    def __init__(self, name: str, answers: dict | Exception, cost: float = 0.001) -> None:
        self.name = name
        self.answers = answers
        self.cost = cost
        self.calls: list[str] = []

    async def decide(self, state: str, questions: dict) -> tuple[dict, float]:
        self.calls.append(state)
        if isinstance(self.answers, Exception):
            raise self.answers
        return self.answers, self.cost


def _answers(position: str, conf: float, score: float = 3.2) -> dict:
    return {
        "position": {"type": "choice", "choice": position, "confidence": conf},
        "sentiment": {"type": "score", "score": score, "confidence": 0.9},
    }


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("Joe's Pizza LLC", "joes pizza"),
        ("The Café & Bar, Inc.", "cafe and bar"),
        ("JOE’S  PIZZA", "joes pizza"),
        ("The", "the"),
    ],
)
def test_normalize_name(raw: str, norm: str) -> None:
    assert normalize_name(raw) == norm


def test_present_exact_alias_and_domain() -> None:
    assert mentioned("Try Joe's Pizza on 5th.", JOES) is True
    assert mentioned("JOES PIZZA ATX has great slices", JOES) is True
    assert mentioned("See joespizzaatx.com for hours", JOES) is True
    assert mentioned("A good spot [1]", JOES, ["https://joespizzaatx.com/menu"]) is True


def test_absent_is_false_not_a_model_call() -> None:
    # The contract is synchronous: mentioned() cannot reach a model at all.
    assert mentioned("Home Slice Pizza and Via 313 are the best in Austin.", JOES) is False
    assert mentioned("", JOES) is False


def test_near_miss_is_none() -> None:
    assert mentioned("Locals like Joe Pizza near Zilker.", JOES) is None
    assert mentioned("Locals like Joes Piza near Zilker.", JOES) is None


def test_short_names_are_not_fuzzed() -> None:
    biz = Business(name="Ace", business_type="hardware store")
    assert mentioned("Acme Hardware is open late.", biz) is False


async def test_confirm_hook_uses_noul() -> None:
    yes = FakeModel("m", {"names_business": {"type": "noul", "noul": 0.8}})
    no = FakeModel("m", {"names_business": {"type": "noul", "noul": 0.1}})
    assert await make_confirm(yes)("Joe Pizza", JOES) is True
    assert await make_confirm(no)("Joe Pizza", JOES) is False


def test_competitors_mentioned() -> None:
    text = "Home Slice Pizza and Via313 lead; homeslicepizza.com has the menu."
    assert competitors_mentioned(text, JOES.competitors) == ["Home Slice Pizza", "Via 313"]
    assert competitors_mentioned("Only Joe's Pizza here.", JOES.competitors) == []


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://joespizzaatx.com/menu", "own_site"),
        ("https://order.joespizzaatx.com/", "own_site"),
        ("https://www.google.com/maps/place/Joe's+Pizza/@30.2", "gbp"),
        ("https://maps.google.com/?cid=123", "gbp"),
        ("https://maps.app.goo.gl/abc", "gbp"),
        ("https://g.page/joes-pizza", "gbp"),
        ("https://www.google.com/search?q=pizza", "other"),
        ("https://www.yelp.com/biz/joes-pizza-austin", "yelp"),
        ("https://m.yelp.co.uk/biz/x", "yelp"),
        ("https://www.tripadvisor.com/Restaurant_Review-g1", "tripadvisor"),
        ("https://www.reddit.com/r/Austin/comments/1", "reddit"),
        ("https://redd.it/abc", "reddit"),
        ("https://www.yellowpages.com/austin-tx/pizza", "directory"),
        ("https://www.opentable.com/r/joes", "directory"),
        ("https://www.eater.com/austin/best-pizza", "other"),
        ("https://notjoespizzaatx.com/", "other"),
    ],
)
def test_source_type(url: str, kind: str) -> None:
    assert source_type(url, JOES.domain) == kind


def test_url_names_business() -> None:
    assert url_names_business("https://www.yelp.com/biz/joes-pizza-austin-2", JOES)
    assert url_names_business("https://www.google.com/maps/place/Joe's+Pizza/@30", JOES)
    assert not url_names_business("https://www.yelp.com/biz/home-slice-pizza-austin", JOES)


def test_passage_for_picks_naming_sentences() -> None:
    text = "Home Slice is great. Joe's Pizza is slow. Via 313 too."
    assert passage_for(text, JOES) == "Joe's Pizza is slow."
    assert passage_for("nothing here", JOES) == "nothing here"


async def test_judge_mention_confident_primary() -> None:
    clef = FakeModel("clef-flash", _answers("recommended", 0.92, 3.4))
    haiku = FakeModel("claude-haiku", _answers("listed", 0.99))
    j = await judge_mention("Joe's Pizza is the best.", JOES, clef, haiku)
    assert (j.position, j.sentiment, j.judged_by) == ("recommended", 3, "clef-flash")
    assert j.confidence == 0.9  # min of position and sentiment confidence
    assert haiku.calls == []


async def test_judge_mention_low_confidence_goes_to_fallback() -> None:
    clef = FakeModel("clef-flash", _answers("recommended", 0.55))
    haiku = FakeModel("claude-haiku", _answers("negative", 0.95, 0.6), cost=0.002)
    j = await judge_mention("Joe's Pizza was slow.", JOES, clef, haiku)
    assert (j.position, j.sentiment, j.judged_by) == ("negative", 1, "claude-haiku")
    assert j.cost_usd == pytest.approx(0.003)  # both calls are paid for


async def test_judge_mention_low_confidence_without_fallback_keeps_primary() -> None:
    j = await judge_mention("x", JOES, FakeModel("clef-flash", _answers("listed", 0.5)))
    assert j.position == "listed" and j.confidence == 0.5


async def test_judge_mention_primary_error_uses_fallback() -> None:
    clef = FakeModel("clef-flash", RuntimeError("boom"))
    haiku = FakeModel("claude-haiku", _answers("listed", 0.8))
    assert (await judge_mention("x", JOES, clef, haiku)).judged_by == "claude-haiku"
    with pytest.raises(RuntimeError):
        await judge_mention("x", JOES, clef)


async def test_judge_parses_probabilities_when_no_choice() -> None:
    answers = {
        "position": {"probabilities": {"recommended": 0.1, "listed": 0.8, "negative": 0.1}},
        "sentiment": {"probabilities": {"0": 0.0, "1": 0.1, "2": 0.75, "3": 0.15, "4": 0.0}},
    }
    j = await judge_mention("x", JOES, FakeModel("clef-flash", answers))
    assert (j.position, j.sentiment, j.confidence) == ("listed", 2, 0.75)


async def test_clef_flash_rest_call() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "result": {
                    "model": "clef-flash",
                    "answers": _answers("recommended", 0.9),
                    "usage": {"input_tokens": 1000},
                },
            },
        )

    clef = ClefFlash("acct1", "cf-token", transport=httpx.MockTransport(handler))
    j = await judge_mention("Joe's Pizza is great", JOES, clef)
    assert seen["url"] == (
        "https://api.cloudflare.com/client/v4/accounts/acct1/ai/run/@cf/cloudflare/clef-flash"
    )
    assert seen["auth"] == "Bearer cf-token"
    assert seen["body"]["state"] == "Joe's Pizza is great"
    assert seen["body"]["questions"]["position"]["type"] == "choice"
    assert j.position == "recommended" and j.cost_usd == pytest.approx(0.00009)


async def test_haiku_fallback_parses_json_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": 'Sure: {"position": {"choice": "negative", "confidence": 0.8},'
                        ' "sentiment": {"score": 1, "confidence": 0.8}}',
                    }
                ],
                "usage": {"input_tokens": 400, "output_tokens": 40},
            },
        )

    haiku = HaikuFallback("sk-ant", transport=httpx.MockTransport(handler))
    j = await judge_mention("Joe's Pizza was cold", JOES, haiku)
    assert (j.position, j.sentiment, j.judged_by) == ("negative", 1, "claude-haiku")
    assert j.cost_usd == pytest.approx(0.0004 + 0.0002)
