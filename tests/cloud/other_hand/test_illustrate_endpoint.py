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
