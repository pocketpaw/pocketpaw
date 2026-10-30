# tests/ee/agent/test_ripple_beats.py — Ripple explainer diagrams in HyperFrames
# compositions: the pre-dispatch check, the 15-20 s reel window, and the brief
# reaching the Studio tool. clean_reel defaults to the film route
# (test_anatomy_film.py covers the routing), so the reel paths here include one.
from __future__ import annotations

import json

import pytest
from pocketpaw_ee.agent.mcp_servers.anatomy_film import ANATOMY_FILM_EXAMPLES
from pocketpaw_ee.agent.mcp_servers.ripple_beats import FIGURE_ANCHORS, validate_ripple_beats
from pocketpaw_ee.agent.mcp_servers.timeline import (
    ADD_MOTION_GRAPHIC_DESCRIPTION,
    validate_motion_graphic,
)
from pocketpaw_ee.cloud.growth.social.ideas import clean_reel


def comp(body: str = "", duration: float = 18) -> str:
    return (
        "<!doctype html><html><body>"
        f'<div data-composition-id="main" data-start="0" data-duration="{duration}" '
        f'data-width="1080" data-height="1920">{body}</div>'
        "<script>const tl=gsap.timeline({paused:true});"
        'window.__timelines=window.__timelines||{};window.__timelines["main"]=tl;</script>'
        "</body></html>"
    )


FIGURE = '<div data-ripple="figure" data-figure="heart" data-labels=\'["aorta"]\'></div>'
ISO = {"parts": [{"id": "ring", "kind": "gear", "at": [0, 0, 30], "r": 22}], "motions": []}
SCENE = (
    f'<div data-ripple="iso-scene"><script type="application/json">{json.dumps(ISO)}</script></div>'
)
FILM = (
    '<div data-anatomy-film data-start="2" data-duration="12"><script type="application/json">'
    + json.dumps(ANATOMY_FILM_EXAMPLES[0])
    + "</script></div>"
)


def test_known_hosts_pass_and_reach_the_render():
    html = comp(FIGURE + SCENE)
    shape, error = validate_motion_graphic(html, 30)
    assert error is None and shape["durationS"] == 18
    reel = comp(FIGURE + SCENE + FILM)
    assert clean_reel(reel) == reel


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ('<div data-ripple="figure" data-figure="liver"></div>', "not a Ripple figure"),
        ('<div data-ripple="figure"></div>', "not a Ripple figure"),
        ('<div data-ripple="iso-scene"><script type="application/json">{x</script></div>', "JSON"),
        ('<div data-ripple="iso-scene"></div>', "JSON"),
        ('<div data-ripple="chart"></div>', "unknown"),
    ],
)
def test_bad_hosts_are_refused_with_a_fix(body, needle):
    assert needle in validate_ripple_beats(comp(body))
    assert validate_motion_graphic(comp(body), 30)[1] is not None


@pytest.mark.parametrize(("duration", "kept"), [(10, False), (15, True), (20, True), (21, False)])
def test_reels_hold_to_fifteen_to_twenty_seconds(duration, kept):
    assert bool(clean_reel(comp(FILM, duration=duration))) is kept


def test_the_brief_names_every_figure_in_the_studio_tool():
    prompt = ADD_MOTION_GRAPHIC_DESCRIPTION
    assert 'data-ripple="figure"' in prompt and 'data-ripple="iso-scene"' in prompt
    assert all(fid in prompt for fid in FIGURE_ANCHORS)
