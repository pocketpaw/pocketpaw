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


# ---------------------------------------------------------------------------
# Added 2026-09-01. The daily budget is a COST CEILING, not an entitlement: it
# caps what one workspace spends, and a guest can mint a fresh workspace to get
# a fresh ceiling. So guests are refused outright, on BOTH paths -- the REST
# route and the MCP tool the agent calls when asked to draw. Gating only the
# route would leave the feature reachable by simply asking.
#
# Mutation-checked: drop either guest check and the matching test goes red.
# ---------------------------------------------------------------------------


class TestGuestsCannotSpendPlatformMoneyOnPictures:
    @pytest.mark.asyncio
    async def test_the_route_refuses_a_guest_before_claiming_budget(self, monkeypatch):
        from pocketpaw_ee.cloud._core.errors import GuestIllustrateForbidden
        from pocketpaw_ee.cloud.auth import guest_budget
        from pocketpaw_ee.cloud.other_hand import illustration_budget
        from pocketpaw_ee.cloud.other_hand import router as oh_router

        async def _is_a_guest(_user_id):
            return {"id": "guest-1"}

        monkeypatch.setattr(guest_budget, "load_guest", _is_a_guest)

        async def _budget_must_not_be_touched(_workspace_id=None):
            raise AssertionError("a refused guest still consumed the daily budget")

        monkeypatch.setattr(illustration_budget, "try_spend", _budget_must_not_be_touched)

        async def _must_not_run(*_a, **_k):
            raise AssertionError("the route paid the generator for a guest")

        monkeypatch.setattr(ill, "illustrate_as_ops", _must_not_run)

        body = oh_router.IllustrateRequest(prompt="a honeybee", x=0, y=0, w=600, h=600)
        with pytest.raises(GuestIllustrateForbidden) as caught:
            await oh_router.illustrate(body=body, workspace_id="ws-1", user_id="u-guest")

        assert caught.value.status_code == 403
        assert caught.value.code == "guest_illustrate_forbidden"
        # The frontend keys the signup prompt off a TOP-LEVEL code.
        assert caught.value.to_dict()["code"] == "guest_illustrate_forbidden"

    @pytest.mark.asyncio
    async def test_a_real_account_still_draws(self, monkeypatch):
        from pocketpaw_ee.cloud.auth import guest_budget
        from pocketpaw_ee.cloud.other_hand import illustration_budget
        from pocketpaw_ee.cloud.other_hand import router as oh_router

        async def _not_a_guest(_user_id):
            return None

        monkeypatch.setattr(guest_budget, "load_guest", _not_a_guest)

        async def _allow(_workspace_id=None):
            return True, 1, 20

        monkeypatch.setattr(illustration_budget, "try_spend", _allow)

        async def _draw(*_a, **_k):
            return [{"t": "path", "d": "M0 0 L1 1"}]

        monkeypatch.setattr(ill, "illustrate_as_ops", _draw)

        body = oh_router.IllustrateRequest(prompt="a honeybee", x=0, y=0, w=600, h=600)
        out = await oh_router.illustrate(body=body, workspace_id="ws-1", user_id="u-real")
        assert out["ops"], "a signed-up account should still get its drawing"

    @pytest.mark.asyncio
    async def test_the_mcp_tool_refuses_a_guest_too(self, monkeypatch):
        """The path a guest reaches by ASKING the agent, rather than pressing."""
        from pocketpaw_ee.agent.mcp_servers import other_hand as tool_mod
        from pocketpaw_ee.cloud.auth import guest_budget
        from pocketpaw_ee.cloud.chat import agent_service
        from pocketpaw_ee.cloud.other_hand import illustrate as ill_mod
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget
        from pocketpaw_ee.cloud.studio import fal_edit

        monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "test-key")
        monkeypatch.setattr(agent_service, "current_user_id", lambda: "u-guest")

        async def _is_a_guest(_user_id):
            return {"id": "guest-1"}

        monkeypatch.setattr(guest_budget, "load_guest", _is_a_guest)

        async def _budget_must_not_be_touched(*_a, **_k):
            raise AssertionError("a refused guest still consumed the daily budget")

        monkeypatch.setattr(budget, "try_spend", _budget_must_not_be_touched)

        async def _must_not_run(*_a, **_k):
            raise AssertionError("the tool paid the generator for a guest")

        monkeypatch.setattr(ill_mod, "illustrate_as_ops", _must_not_run)

        res = await tool_mod._illustrate_handler({"subject": "a honeybee"})
        assert "account" in res["content"][0]["text"].lower(), res

    @pytest.mark.asyncio
    async def test_the_mcp_tool_refuses_when_it_cannot_tell_who_is_asking(self, monkeypatch):
        """No identity resolves to a refusal, not to a free drawing."""
        from pocketpaw_ee.agent.mcp_servers import other_hand as tool_mod
        from pocketpaw_ee.cloud.chat import agent_service
        from pocketpaw_ee.cloud.other_hand import illustrate as ill_mod
        from pocketpaw_ee.cloud.other_hand import illustration_budget as budget
        from pocketpaw_ee.cloud.studio import fal_edit

        monkeypatch.setattr(fal_edit, "fal_api_key", lambda: "test-key")
        monkeypatch.setattr(agent_service, "current_user_id", lambda: None)

        async def _budget_must_not_be_touched(*_a, **_k):
            raise AssertionError("an unidentified caller still consumed budget")

        monkeypatch.setattr(budget, "try_spend", _budget_must_not_be_touched)

        async def _must_not_run(*_a, **_k):
            raise AssertionError("the tool paid the generator for an unknown caller")

        monkeypatch.setattr(ill_mod, "illustrate_as_ops", _must_not_run)

        res = await tool_mod._illustrate_handler({"subject": "a honeybee"})
        assert "account" in res["content"][0]["text"].lower(), res
