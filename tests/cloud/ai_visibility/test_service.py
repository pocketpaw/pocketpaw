# tests/cloud/ai_visibility/test_service.py — ``run_check`` end to end.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). Fake engines and a fake
# decision model against a mongomock Beanie DB: the "named X of N" frequency per
# engine (N = answers received, failures counted apart), near misses through the
# confirm hook, judgements only on named answers, the picked fix, cost accounting
# (engine + judge, failed calls free), the stored doc, the concurrency limit,
# input validation, and the deterministic question templates.
from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.ai_visibility.domain import Business, Competitor, EngineAnswer, Location
from pocketpaw_ee.cloud.ai_visibility.engines import EngineError
from pocketpaw_ee.cloud.ai_visibility.service import generate_questions, run_check
from pocketpaw_ee.cloud.models.ai_visibility_check import AiVisibilityCheck

pytestmark = pytest.mark.usefixtures("mongo_db")

JOES = Business(
    name="Joe's Pizza",
    business_type="pizza restaurant",
    domain="joespizzaatx.com",
    competitors=(Competitor("Home Slice Pizza"), Competitor("Via 313")),
)
AUSTIN = Location(city="Austin", country="US", region="Texas")


class FakeEngine:
    """Returns ``texts`` in order (cycling); an Exception item is raised."""

    def __init__(self, name: str, texts: list, consulted: tuple[str, ...] = (), cost=0.01):
        self.name = name
        self.texts = texts
        self.consulted = consulted
        self.cost = cost
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def ask(self, question: str, location: Location) -> EngineAnswer:
        i = self.calls
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        item = self.texts[i % len(self.texts)]
        if isinstance(item, Exception):
            raise item
        return EngineAnswer(
            engine=self.name,
            model=f"{self.name}-model",
            text=item,
            consulted_urls=self.consulted,
            cost_usd=self.cost,
        )


class FakeDecision:
    name = "fake-clef"

    def __init__(self, position: str = "recommended", sentiment: int = 3) -> None:
        self.position = position
        self.sentiment = sentiment
        self.calls = 0

    async def decide(self, state: str, questions: dict) -> tuple[dict, float]:
        self.calls += 1
        return (
            {
                "position": {"choice": self.position, "confidence": 0.9},
                "sentiment": {"score": self.sentiment, "confidence": 0.9},
            },
            0.0001,
        )


NAMED = "Joe's Pizza and Home Slice Pizza are both great."
ABSENT = "Home Slice Pizza and Via 313 are the picks."
NEAR = "Locals like Joe Pizza near Zilker."


async def test_named_x_of_n_per_engine_and_storage() -> None:
    openai = FakeEngine(
        "openai",
        [NAMED, ABSENT, NAMED],
        consulted=("https://joespizzaatx.com/", "https://www.yelp.com/biz/joes-pizza-austin"),
    )
    pplx = FakeEngine("perplexity", [ABSENT, EngineError("api.perplexity.ai: HTTP 500")])
    decision = FakeDecision()

    check = await run_check(
        JOES,
        AUSTIN,
        ["best pizza in Austin"],
        [openai, pplx],
        runs=3,
        workspace_id="ws1",
        site_id="site1",
        decision_model=decision,
    )

    assert check.summary["openai"] == {
        "named": 2,
        "of": 3,
        "failed": 0,
        "near_miss": 0,
        "competitors": {"Home Slice Pizza": 3, "Via 313": 1},
    }
    # 3 calls: ABSENT, error, ABSENT -> N counts answers received only
    assert check.summary["perplexity"]["named"] == 0
    assert check.summary["perplexity"]["of"] == 2
    assert check.summary["perplexity"]["failed"] == 1
    assert decision.calls == 2  # only the two named answers are judged

    # cost: 5 successful engine calls at 0.01 + 2 judgements at 0.0001; failures free
    assert check.total_cost_usd == pytest.approx(5 * 0.01 + 2 * 0.0001)
    failed = [r for r in check.runs if not r["ok"]]
    assert failed[0]["error"] == "api.perplexity.ai: HTTP 500" and failed[0]["cost_usd"] == 0

    named_row = next(r for r in check.runs if r["ok"] and r["mentioned"])
    assert named_row["judgement"]["position"] == "recommended"
    assert {"url": "https://joespizzaatx.com/", "type": "own_site"} in named_row["sources"]

    stored = await AiVisibilityCheck.get(check.id)
    assert stored is not None and stored.workspace == "ws1" and stored.site_id == "site1"
    assert len(stored.runs) == 6 and stored.fix == check.fix
    # own site consulted, Yelp listing is the business's, recommended -> nothing to fix
    assert check.fix["id"] == "none"


async def test_near_miss_confirm_and_anonymous_check() -> None:
    engine = FakeEngine("claude", [NEAR])
    seen: list[str] = []

    async def confirm(passage: str, business: Business) -> bool:
        seen.append(passage)
        return True

    check = await run_check(JOES, AUSTIN, ["q"], [engine], runs=2, confirm=confirm)
    assert check.workspace_id is None and check.site_id is None
    assert check.summary["claude"]["named"] == 2 and check.summary["claude"]["near_miss"] == 2
    assert seen == [NEAR, NEAR]

    unconfirmed = await run_check(JOES, AUSTIN, ["q"], [FakeEngine("claude", [NEAR])], runs=1)
    assert unconfirmed.summary["claude"]["named"] == 0  # no hook -> not counted


async def test_fix_when_never_named_and_site_unread() -> None:
    engine = FakeEngine("openai", [ABSENT], consulted=("https://www.yelp.com/biz/home-slice",))
    check = await run_check(JOES, AUSTIN, ["q"], [engine], runs=2)
    assert check.fix["id"] == "site_content" and check.fix["we_can_apply"] is True


async def test_fix_negative_and_ai_access() -> None:
    engine = FakeEngine("openai", [NAMED], consulted=("https://joespizzaatx.com/",))
    neg = await run_check(
        JOES, AUSTIN, ["q"], [engine], runs=1, decision_model=FakeDecision("negative", 1)
    )
    assert neg.fix["id"] == "negative_mentions"
    blocked = await run_check(JOES, AUSTIN, ["q"], [engine], runs=1, site_blocks_ai_bots=True)
    assert blocked.fix["id"] == "ai_access"


async def test_fallback_alone_judges_when_no_primary() -> None:
    fallback = FakeDecision("listed", 2)
    check = await run_check(
        JOES, AUSTIN, ["q"], [FakeEngine("openai", [NAMED])], runs=1, fallback_model=fallback
    )
    assert fallback.calls == 1 and check.runs[0]["judgement"]["position"] == "listed"


async def test_concurrency_limit() -> None:
    engine = FakeEngine("openai", [ABSENT])
    await run_check(JOES, AUSTIN, ["a", "b", "c"], [engine], runs=3, concurrency=2)
    assert engine.calls == 9 and engine.max_in_flight <= 2


async def test_judge_failure_is_not_fatal() -> None:
    class Boom:
        name = "boom"

        async def decide(self, state, questions):
            raise RuntimeError("down")

    check = await run_check(
        JOES, AUSTIN, ["q"], [FakeEngine("openai", [NAMED])], runs=1, decision_model=Boom()
    )
    assert check.runs[0]["mentioned"] is True and check.runs[0]["judgement"] is None


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"questions": []}, "ai_visibility.questions"),
        ({"engines": []}, "ai_visibility.no_engines"),
        ({"runs": 0}, "ai_visibility.runs"),
        ({"runs": 11}, "ai_visibility.runs"),
    ],
)
async def test_validation(kwargs: dict, code: str) -> None:
    args = {"questions": ["q"], "engines": [FakeEngine("x", [ABSENT])], "runs": 1, **kwargs}
    with pytest.raises(ValidationError) as exc:
        await run_check(JOES, AUSTIN, **args)
    assert exc.value.code == code


def test_generate_questions_is_deterministic() -> None:
    qs = generate_questions("dentist", "Austin", 3, area="Zilker")
    assert qs == [
        "best dentist in Austin",
        "dentist near Zilker recommendations",
        "which dentist in Austin do locals recommend?",
    ]
    assert generate_questions("dentist", "Austin", 2) == generate_questions("dentist", "Austin", 2)
    assert generate_questions("dentist", "Austin", 2)[1] == "dentist near Austin recommendations"
