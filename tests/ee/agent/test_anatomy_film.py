# tests/ee/agent/test_anatomy_film.py — the full-frame anatomy film in Growth
# reels: the pre-dispatch check on its placeholder element, clean_reel requiring
# it, and the reel prompt carrying its brief while Studio keeps the Ripple one.
from __future__ import annotations

import json

import pytest
from pocketpaw_ee.agent.mcp_servers.anatomy_film import validate_anatomy_film
from pocketpaw_ee.agent.mcp_servers.timeline import (
    ADD_MOTION_GRAPHIC_DESCRIPTION,
    validate_motion_graphic,
)
from pocketpaw_ee.cloud.growth.social.domain import SocialIdea, SocialProfile
from pocketpaw_ee.cloud.growth.social.ideas import build_media_prompt, clean_reel


def comp(body: str = "", duration: float = 18) -> str:
    return (
        "<!doctype html><html><body>"
        f'<div data-composition-id="main" data-start="0" data-duration="{duration}" '
        f'data-width="1080" data-height="1920">{body}</div>'
        "<script>const tl=gsap.timeline({paused:true});"
        'window.__timelines=window.__timelines||{};window.__timelines["main"]=tl;</script>'
        "</body></html>"
    )


SPEC = {
    "title": "Phone",
    "subtitle": "147 mm tall",
    "parts": [
        {"id": "shell", "kind": "box", "at": [0, 0, 0], "w": 70, "d": 8, "h": 147, "round": 9},
        {"id": "battery", "kind": "box", "at": [0, 0, 20], "w": 50, "d": 4, "h": 80, "group": "in"},
        {"id": "lens", "kind": "cylinder", "at": [20, 4, 130], "r": 6, "h": 3, "axis": "y"},
    ],
    "assemblies": [{"id": "in", "dir": "front", "travel": 40, "delay": 0.2}],
    "shots": [{"part": "battery", "hint": "Battery", "readout": "Two cells"}],
}


def film(spec: object = SPEC, start: str | None = "2", duration: str | None = "14") -> str:
    attrs = "data-anatomy-film"
    if start is not None:
        attrs += f' data-start="{start}"'
    if duration is not None:
        attrs += f' data-duration="{duration}"'
    body = spec if isinstance(spec, str) else json.dumps(spec)
    return (
        f'<div {attrs} style="position:absolute;inset:0">'
        f'<script type="application/json">{body}</script></div>'
    )


def test_a_reel_with_the_film_passes_clean_reel():
    html = comp(film())
    assert validate_anatomy_film(html, 18) is None
    assert clean_reel(html) == html


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        (film("{not json"), "valid JSON"),
        (film({"parts": [{"id": "x", "kind": "torus"}]}), "box, cylinder, ball"),
        (film(start=None), "data-start"),
        (film(start="-1"), "data-start"),
        (film(duration="3"), "data-duration"),
        (film(start="6", duration="14"), "ends at 18s"),
    ],
)
def test_bad_films_are_refused_with_a_fix(body, needle):
    html = comp(body)
    assert needle in validate_anatomy_film(html, 18)
    assert needle in validate_motion_graphic(html, 30)[1]
    assert clean_reel(html) == ""


def test_no_film_element_is_fine_for_studio_but_not_for_a_reel():
    html = comp("<h1>Hook</h1>")
    assert validate_anatomy_film(html, 18) is None
    assert validate_motion_graphic(html, 30)[1] is None
    assert clean_reel(html) == ""


def test_the_reel_prompt_asks_for_the_film_and_studio_keeps_ripple():
    prompt = build_media_prompt(
        SocialProfile(id="p", workspace_id="w", company_name="Acme"),
        SocialIdea(id="i", workspace_id="w", format="reel", hook="Inside the box"),
        "reel",
    )
    assert "data-anatomy-film" in prompt and "15 to 20 second" in prompt
    assert "data-ripple" not in prompt and "ripple" not in prompt.lower()
    assert 'data-ripple="figure"' in ADD_MOTION_GRAPHIC_DESCRIPTION
    assert 'data-ripple="iso-scene"' in ADD_MOTION_GRAPHIC_DESCRIPTION
