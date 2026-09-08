# tests/ee/terrarium/test_prompt_cache.py — the citizen prompt splits into a
# stable prefix (cacheable at the provider) and a volatile suffix; HttpLlm marks
# that prefix ONLY on a Claude model whose minimum cacheable length it clears,
# the meter records whether it was marked, and cache reads reach the cost meter.
# The last test measures the shipped Dust seed against those minimums.

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import world  # noqa: E402
from pocketpaw_ee.terrarium.physics import load_physics, seed_physics_path  # noqa: E402


def _digest(snap: world.CitizenSnapshot, *, day: int, speech: list[str]) -> world.SenseDigest:
    return world.build_digest(
        day=day,
        tick=day * 3,
        pool=100 + day,
        citizen=snap,
        ledger=[{"name": snap.name, "balance": snap.balance}],
        nearby_speech=speech,
        new_artifacts=[],
        weather=[],
        viewer_messages=[],
        memories=[f"day {day} happened"],
        constitution=["no theft"],
    )


def test_the_prefix_is_byte_identical_across_ticks_and_differs_between_citizens():
    physics = load_physics(seed_physics_path("dust"))
    nim = world.CitizenSnapshot(
        id="c1", name="Nim", balance=100, charter="keep faith", ocean={"o": 0.6}, values=("care",)
    )
    ash = world.CitizenSnapshot(id="c2", name="Ash", balance=100, charter="keep faith")

    p1, s1 = citizen_llm.build_prompt_parts(physics, nim, _digest(nim, day=1, speech=[]))
    p2, s2 = citizen_llm.build_prompt_parts(physics, nim, _digest(nim, day=2, speech=["Ash: hi"]))
    assert p1 == p2
    assert s1 != s2 and "Ash: hi" in s2 and "Ash: hi" not in s1
    assert "day 1" not in p1 and "no theft" not in p1  # nothing from the digest leaks in
    assert "care" in p1 and '"o": 0.6' in p1

    p3, _ = citizen_llm.build_prompt_parts(physics, ash, _digest(ash, day=1, speech=[]))
    assert p3 != p1
    # The joined prompt is what the CLI and the mock still see.
    assert citizen_llm.build_prompt(physics, nim, _digest(nim, day=1, speech=[])) == p1 + s1


def test_a_drift_line_lives_in_the_prefix():
    physics = load_physics(seed_physics_path("dust"))
    nim = world.CitizenSnapshot(id="c1", name="Nim", balance=10)
    p, _ = citizen_llm.build_prompt_parts(
        physics, nim, _digest(nim, day=1, speech=[]), drift_line="slightly more open"
    )
    assert "slightly more open" in p


# Long enough to clear every model's minimum cacheable prefix, so a test about
# the marker is not accidentally a test about the length gate.
_LONG = "STABLE " * 3000


async def _capture(model: str, prefix: str = _LONG) -> dict:
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"thought": "ok", "acts": []}'}],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 10,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = citizen_llm.MeteredLlm(citizen_llm.HttpLlm("k", model, client=client), model)
    out = await llm.decide_parts(prefix, "VOLATILE", physics=None, citizen=None, digest=None)  # type: ignore[arg-type]
    assert '"thought"' in out
    seen["meter"] = llm.meter.summary()
    return seen


async def test_an_anthropic_model_marks_the_prefix_only():
    seen = await _capture("claude-sonnet-4-6")
    blocks = seen["body"]["messages"][0]["content"]
    assert [b["text"] for b in blocks] == [_LONG, "VOLATILE"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


async def test_a_non_anthropic_model_carries_no_marker():
    seen = await _capture("gpt-5-mini")
    blocks = seen["body"]["messages"][0]["content"]
    assert [b["text"] for b in blocks] == [_LONG, "VOLATILE"]
    assert not any("cache_control" in b for b in blocks)


async def test_cache_reads_reach_the_meter_at_the_cached_rate():
    seen = await _capture("claude-sonnet-4-6")
    meter = seen["meter"]
    assert meter["calls"] == 1
    assert meter["input_tokens"] == 1000 and meter["cache_read_tokens"] == 900
    assert meter["output_tokens"] == 10
    rate_in, rate_out = citizen_llm.PRICING["claude-sonnet-4-6"]
    expected = ((100 + 900 * 0.1) * rate_in + 10 * rate_out) / 1_000_000
    assert meter["cost_usd"] == pytest.approx(expected, abs=1e-9)
    uncached = citizen_llm.CostMeter("claude-sonnet-4-6")
    uncached.record("", "", {"input_tokens": 1000, "output_tokens": 10})
    assert uncached.cost_usd > meter["cost_usd"]


# --------------------------------------------------------------------------
# The length gate. A marker on a prefix under the model's minimum is not an
# error and not a saving — it is a no-op the provider never mentions.
# --------------------------------------------------------------------------


def test_the_minimums_are_the_published_ones_and_are_not_monotonic():
    assert citizen_llm.MIN_CACHEABLE_TOKENS == {
        "claude-opus-5": 512,
        "claude-sonnet-5": 1024,
        "claude-sonnet-4-6": 1024,
        "claude-haiku-4-5": 4096,
    }
    # The cheap model has the LONGEST minimum. Anything that assumes cheaper
    # means laxer gets this backwards.
    mins = citizen_llm.MIN_CACHEABLE_TOKENS
    assert mins["claude-haiku-4-5"] > mins["claude-sonnet-5"] > mins["claude-opus-5"]


def test_a_prefix_under_the_minimum_is_not_marked_and_one_over_it_is():
    # ~1200 approximate tokens: over opus and the sonnets, well under haiku.
    prefix = "x" * (1200 * 4)
    assert citizen_llm.cache_marker_fits("claude-opus-5", prefix)
    assert citizen_llm.cache_marker_fits("claude-sonnet-5", prefix)
    assert not citizen_llm.cache_marker_fits("claude-haiku-4-5", prefix)
    # Right at the boundary, and one token under it.
    assert citizen_llm.cache_marker_fits("claude-opus-5", "x" * (512 * 4))
    assert not citizen_llm.cache_marker_fits("claude-opus-5", "x" * (511 * 4))


async def test_the_cheap_tier_sends_the_same_two_blocks_without_the_marker():
    """Behaviour under the minimum is identical minus a marker that buys
    nothing — not a dropped block, not a joined prompt."""
    short = "x" * (1200 * 4)
    seen = await _capture("claude-haiku-4-5", short)
    blocks = seen["body"]["messages"][0]["content"]
    assert [b["text"] for b in blocks] == [short, "VOLATILE"]
    assert not any("cache_control" in b for b in blocks)
    assert seen["meter"]["cache_marked_calls"] == 0

    marked = await _capture("claude-opus-5", short)
    assert marked["body"]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked["meter"]["cache_marked_calls"] == 1


def test_the_tiers_run_on_the_current_generation():
    assert citizen_llm.model_for_tier("premium") == "claude-opus-5"
    assert citizen_llm.model_for_tier("mid") == "claude-sonnet-5"
    assert citizen_llm.model_for_tier("tail") == "claude-haiku-4-5"
    assert citizen_llm.model_for_tier("nonsense") == citizen_llm.model_for_tier(None)


def test_sonnet_5_is_cheaper_than_the_model_it_replaces():
    assert citizen_llm.PRICING["claude-sonnet-5"] == (2.00, 10.00)
    assert citizen_llm.PRICING["claude-opus-5"] == (5.00, 25.00)
    # The previous generation stays priced so an older universe still costs out.
    assert citizen_llm.PRICING["claude-sonnet-4-6"] == (3.00, 15.00)
    assert citizen_llm.PRICING["claude-haiku-4-5"] == (1.00, 5.00)
    assert citizen_llm.PRICING["claude-sonnet-5"] < citizen_llm.PRICING["claude-sonnet-4-6"]
    # An unpriced model still prices instead of raising (see test_cost.py).
    assert citizen_llm.CostMeter("no-such-model").model in citizen_llm.PRICING


def test_the_dust_prefix_is_too_short_to_cache_on_any_tier():
    """THE NUMBER. This citizen's stable prefix measures 1822 chars = 455
    approximate tokens — under opus-5's 512, under half the sonnets' 1024, an
    ninth of haiku-4-5's 4096. Caching engages on NOTHING at Dust size, which is
    why the marker is gated rather than trusted. The charter is the growable
    part: it is one sentence here, and ~230 more chars would clear opus-5."""
    physics = load_physics(seed_physics_path("dust"))
    nim = world.CitizenSnapshot(
        id="c1",
        name="Nim",
        role="wellkeeper",
        balance=100,
        charter="I am Nim, wellkeeper. I will keep what I say and pay what I owe.",
        ocean={
            "openness": 0.62,
            "conscientiousness": 0.51,
            "extraversion": 0.44,
            "agreeableness": 0.58,
            "neuroticism": 0.37,
        },
        values=("care", "fairness", "curiosity"),
    )
    prefix, _ = citizen_llm.build_prompt_parts(
        physics, nim, _digest(nim, day=3, speech=[]), drift_line="slightly more open"
    )
    approx_tokens = len(prefix) // 4
    assert 400 < approx_tokens < 512, approx_tokens
    for model in citizen_llm.MIN_CACHEABLE_TOKENS:
        assert not citizen_llm.cache_marker_fits(model, prefix), model
