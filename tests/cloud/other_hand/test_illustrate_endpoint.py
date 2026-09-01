# tests/cloud/other_hand/test_illustrate_endpoint.py — the whole round trip.
#
# Created 2026-08-28. The unit tests cover the geometry and the spend gate
# separately; this one proves the pieces are actually joined — request in,
# validated `path` ops out — WITHOUT calling the paid generator. Everything
# except fal is real: the route, the body validation, the SVG conversion, the
# box fitting.

from __future__ import annotations

import sys
import types

import pytest
from pocketpaw_ee.cloud.other_hand import illustrate as ill
from pocketpaw_ee.cloud.other_hand.svg_to_ink import Box

# A hand-written stand-in for what Recraft returns: a viewBox, a curve, and a
# shape element, which between them exercise the three code paths that matter.
FAKE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">'
    '<path d="M20 100 C 20 40, 180 40, 180 100"/>'
    '<circle cx="100" cy="140" r="30"/>'
    "</svg>"
)


@pytest.fixture
def fake_generator(monkeypatch):
    """A fal client that costs nothing and returns our SVG."""

    class _Client:
        def __init__(self, *a, **k): ...

        async def run(self, endpoint, arguments=None, **k):
            _Client.last_endpoint = endpoint
            _Client.last_arguments = arguments or {}
            return {"images": [{"url": "https://example.test/x.svg",
                                "content_type": "image/svg+xml"}]}

    mod = types.ModuleType("fal_client")
    mod.AsyncClient = _Client
    monkeypatch.setitem(sys.modules, "fal_client", mod)

    async def _fetch(_url: str) -> str:
        return FAKE_SVG

    monkeypatch.setattr(ill, "_fetch_svg", _fetch)
    return _Client


class TestTheRoundTrip:
    @pytest.mark.asyncio
    async def test_a_prompt_comes_back_as_drawable_path_ops(self, fake_generator):
        ops = await ill.illustrate_as_ops(
            "a honeybee", Box(x=100, y=400, w=600, h=600), api_key="k", allowed=True
        )
        assert ops, "the round trip produced nothing to draw"
        assert {op["t"] for op in ops} == {"path"}
        for op in ops:
            assert len(op["pts"]) >= 2
            for x, y in op["pts"]:
                # Inside the box it was given, which is what makes it safe to
                # append to a page without landing on the user's handwriting.
                assert 100 <= x <= 700
                assert 400 <= y <= 1000

    @pytest.mark.asyncio
    async def test_the_curve_survives_as_a_curve(self, fake_generator):
        ops = await ill.illustrate_as_ops(
            "a honeybee", Box(x=0, y=0, w=400, h=400), api_key="k", allowed=True
        )
        # The cubic in FAKE_SVG must arrive as many points, not as a chord.
        assert max(len(op["pts"]) for op in ops) > 6

    @pytest.mark.asyncio
    async def test_the_generator_is_asked_for_line_art_not_just_the_prompt(
        self, fake_generator
    ):
        await ill.illustrate_as_ops(
            "a honeybee", Box(x=0, y=0, w=400, h=400), api_key="k", allowed=True
        )
        sent = fake_generator.last_arguments["prompt"]
        assert sent.startswith("a honeybee")
        assert "line art" in sent, "the pen cannot draw fills; the ask must say so"

    @pytest.mark.asyncio
    async def test_it_calls_the_vector_endpoint_not_a_raster_one(self, fake_generator):
        await ill.illustrate_as_ops(
            "a honeybee", Box(x=0, y=0, w=400, h=400), api_key="k", allowed=True
        )
        # A raster model here would silently break the whole premise: the
        # result would be a picture, and svg_to_ink would find no geometry.
        assert "text-to-vector" in fake_generator.last_endpoint


# ---------------------------------------------------------------------------
# Added 2026-09-01, from the pre-PR review of integration/session-2026-08-29.
#
# The tests above call `ill.illustrate_as_ops` DIRECTLY, which is why they all
# passed while the route itself spent money with no ceiling: the REST handler
# hard-wired `allowed=True` and never claimed the daily budget, so
# `illustration_budget.try_spend` had exactly one caller in the whole repo —
# the MCP tool. A signed-up user (guest accounts included) could script the
# wand button into an unmetered bill at roughly $0.08 a call.
#
# These two tests exercise the HANDLER, not the generator, because that is
# where the hole was. Mutation-checked: delete the `try_spend` claim in
# router.py and `test_the_route_refuses_once_the_day_is_spent` fails.
# ---------------------------------------------------------------------------


class TestTheRouteHonoursTheDailyBudget:
    """The paid route must claim the budget, and must not pay when refused."""

    @pytest.mark.asyncio
    async def test_the_route_refuses_once_the_day_is_spent(self, monkeypatch):
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.cloud.other_hand import illustration_budget
        from pocketpaw_ee.cloud.other_hand import router as oh_router

        async def _refuse(_workspace_id=None):
            return False, 20, 20

        monkeypatch.setattr(illustration_budget, "try_spend", _refuse)

        # If the handler reaches the generator at all, that is the bug.
        async def _must_not_run(*_a, **_k):
            raise AssertionError("the route paid the generator while over cap")

        monkeypatch.setattr(ill, "illustrate_as_ops", _must_not_run)

        body = oh_router.IllustrateRequest(prompt="a honeybee", x=0, y=0, w=600, h=600)
        with pytest.raises(CloudError) as caught:
            await oh_router.illustrate(body=body, workspace_id="ws-1")

        assert caught.value.status_code == 429
        assert caught.value.code == "other_hand.illustration_limit"
        assert "20/20" in caught.value.message

    @pytest.mark.asyncio
    async def test_the_route_claims_one_unit_before_it_draws(self, monkeypatch):
        from pocketpaw_ee.cloud.other_hand import illustration_budget
        from pocketpaw_ee.cloud.other_hand import router as oh_router

        claimed: list[str | None] = []

        async def _allow(workspace_id=None):
            claimed.append(workspace_id)
            return True, 1, 20

        monkeypatch.setattr(illustration_budget, "try_spend", _allow)

        async def _draw(*_a, **_k):
            assert claimed, "the generator ran before the budget was claimed"
            return [{"t": "path", "d": "M0 0 L1 1"}]

        monkeypatch.setattr(ill, "illustrate_as_ops", _draw)

        body = oh_router.IllustrateRequest(prompt="a honeybee", x=0, y=0, w=600, h=600)
        out = await oh_router.illustrate(body=body, workspace_id="ws-1")

        assert out["ops"], "an allowed call should still draw"
        assert claimed == ["ws-1"], "the claim must be scoped to the caller's workspace"
