# tests/cloud/other_hand/test_illustrate_gate.py — the gate with a bill behind it.
#
# Created 2026-08-28. Every one of these is about NOT spending money. Recraft
# v4 pro is $0.30 an image and BYOK does not cover it — a user's own Anthropic
# key pays for their tokens while every illustration bills the platform — so
# the checks that stop a call are worth more tests than the call itself.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.other_hand import illustrate as ill
from pocketpaw_ee.cloud.other_hand.svg_to_ink import Box

BOX = Box(x=100, y=200, w=600, h=400)


class _Boom:
    """Any use of this is a call we were not supposed to be able to make."""

    def __init__(self, *a, **k):
        raise AssertionError("a fal client was constructed on a path that must not spend money")


@pytest.fixture
def no_spending(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("fal_client")
    mod.AsyncClient = _Boom
    monkeypatch.setitem(sys.modules, "fal_client", mod)
    return mod


class TestNothingSpendsWithoutPermission:
    @pytest.mark.asyncio
    async def test_a_turn_that_did_not_opt_in_makes_no_call(self, no_spending):
        # A valid key present and unusable: the only thing standing between
        # this deployment and a $0.30 charge is the opt-in, so this is the test
        # that matters most in the file.
        assert await ill.illustrate_as_ops("a bee", BOX, api_key="real-key", allowed=False) == []

    @pytest.mark.asyncio
    async def test_no_key_configured_means_no_call_and_no_error(self, no_spending):
        # A deployment without fal answers without a picture; it does not fail
        # the user's question.
        assert await ill.illustrate_as_ops("a bee", BOX, api_key=None, allowed=True) == []

    @pytest.mark.asyncio
    async def test_an_empty_prompt_never_reaches_the_generator(self, no_spending):
        assert await ill.illustrate_as_ops("   ", BOX, api_key="real-key", allowed=True) == []

    def test_availability_is_exactly_whether_a_key_exists(self):
        assert ill.is_available("k") is True
        assert ill.is_available(None) is False
        assert ill.is_available("") is False


class TestTheGeneratorIsAskedForSomethingAPenCanDraw:
    def test_the_style_suffix_rules_out_what_a_single_pen_cannot_render(self):
        # A filled illustration converts to bare outlines and loses whatever the
        # fills carried. Asking for line art up front beats converting badly.
        for needed in ("line art", "no fills", "no shading", "no background"):
            assert needed in ill.STYLE_SUFFIX

    def test_the_generator_is_asked_for_no_lettering(self):
        # Generated text becomes unreadable scribble once flattened to strokes,
        # and the agent writes its own labels with text ops.
        assert "no text" in ill.STYLE_SUFFIX


class TestFailuresDegradeToNoPicture:
    @pytest.mark.asyncio
    async def test_a_generator_outage_raises_a_named_error_not_a_raw_exception(self, monkeypatch):
        import sys
        import types

        class _Failing:
            def __init__(self, *a, **k): ...
            async def run(self, *a, **k):
                raise RuntimeError("upstream 503")

        mod = types.ModuleType("fal_client")
        mod.AsyncClient = _Failing
        monkeypatch.setitem(sys.modules, "fal_client", mod)

        with pytest.raises(ill.IllustrateError, match="did not respond"):
            await ill.illustrate_as_ops("a bee", BOX, api_key="k", allowed=True)

    @pytest.mark.asyncio
    async def test_a_response_with_no_image_is_a_named_error(self, monkeypatch):
        import sys
        import types

        class _Empty:
            def __init__(self, *a, **k): ...
            async def run(self, *a, **k):
                return {"images": []}

        mod = types.ModuleType("fal_client")
        mod.AsyncClient = _Empty
        monkeypatch.setitem(sys.modules, "fal_client", mod)

        with pytest.raises(ill.IllustrateError, match="no image"):
            await ill.illustrate_as_ops("a bee", BOX, api_key="k", allowed=True)

    def test_the_url_reader_tolerates_the_shapes_fal_endpoints_actually_return(self):
        assert ill._extract_svg_url({"images": [{"url": "https://x/a.svg"}]}) == "https://x/a.svg"
        assert ill._extract_svg_url({"image": {"url": "https://x/b.svg"}}) == "https://x/b.svg"
        assert ill._extract_svg_url({"images": "not-a-list"}) is None
        assert ill._extract_svg_url({}) is None
