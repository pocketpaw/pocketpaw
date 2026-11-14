# tests/ee/agent/test_anatomy_film.py — the full-frame anatomy film in Growth
# reels: the pre-dispatch check on its placeholder element, clean_reel requiring
# it, the stricter spec lint, the reel routing between film and Ripple diagram by
# business model, the profile facts in the reel prompt, and the one repair pass.
from __future__ import annotations

import json

import pytest
from pocketpaw_ee.agent.mcp_servers.anatomy_film import (
    ANATOMY_FILM_EXAMPLES,
    validate_anatomy_film,
)
from pocketpaw_ee.agent.mcp_servers.timeline import (
    ADD_MOTION_GRAPHIC_DESCRIPTION,
    validate_motion_graphic,
)
from pocketpaw_ee.cloud.growth.social import ideas
from pocketpaw_ee.cloud.growth.social.domain import SocialAnalysis, SocialIdea, SocialProfile
from pocketpaw_ee.cloud.growth.social.ideas import (
    build_media_prompt,
    check_reel,
    clean_reel,
    reel_visual,
)


def comp(body: str = "", duration: float = 18) -> str:
    return (
        "<!doctype html><html><body>"
        f'<div data-composition-id="main" data-start="0" data-duration="{duration}" '
        f'data-width="1080" data-height="1920">{body}</div>'
        "<script>const tl=gsap.timeline({paused:true});"
        'window.__timelines=window.__timelines||{};window.__timelines["main"]=tl;</script>'
        "</body></html>"
    )


SPEC = ANATOMY_FILM_EXAMPLES[1]


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
        (film({"parts": [{"id": "x", "kind": "torus"}]}), "box, cylinder, ball, lathe"),
        (film({**SPEC, "parts": SPEC["parts"][:3]}), "usable parts"),
        (film({**SPEC, "shots": [{"part": "speaker"}]}), "do not exist: speaker"),
        (film({**SPEC, "assemblies": SPEC["assemblies"][:1]}), "not assemblies"),
        (
            film(
                {**SPEC, "parts": [*SPEC["parts"], {"id": "cup", "kind": "lathe", "at": [0, 0, 0]}]}
            ),
            "profile",
        ),
        (
            film(
                {
                    **SPEC,
                    "parts": [
                        {**p, "at": [p["at"][0], p["at"][1], p["at"][2] + 50]}
                        for p in SPEC["parts"]
                    ],
                }
            ),
            "stands on the ground",
        ),
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


ANALYSIS = SocialAnalysis(
    product="Cold brew coffee kit",
    audience="Home baristas",
    problem="Bitter iced coffee",
    benefits=("Smooth in 12 hours", "No machine needed"),
    differentiators=("Glass carafe",),
    logo_url="https://acme.test/logo.png",
)


def profile(model: str | None = None) -> SocialProfile:
    return SocialProfile(
        id="p", workspace_id="w", company_name="Acme", business_model=model, analysis=ANALYSIS
    )


IDEA = SocialIdea(id="i", workspace_id="w", format="hook_demo", hook="Inside the box")


@pytest.mark.parametrize(
    ("model", "visual"),
    [("ecommerce", "film"), ("b2b_saas", "diagram"), ("services", "diagram"), (None, "either")],
)
def test_the_reel_visual_follows_the_business_model(model, visual):
    assert reel_visual(profile(model)) == visual


def test_the_reel_prompt_carries_the_profile_and_the_routed_brief():
    film_prompt = build_media_prompt(profile("ecommerce"), IDEA, "reel")
    assert "data-anatomy-film" in film_prompt and "15 to 20 second" in film_prompt
    assert "data-ripple" not in film_prompt and "Pour-over kettle" in film_prompt
    for fact in ("Cold brew coffee kit", "Home baristas", "No machine needed", "Glass carafe"):
        assert fact in film_prompt
    assert "Logo URL: https://acme.test/logo.png" in film_prompt
    diagram_prompt = build_media_prompt(profile("b2b_saas"), IDEA, "reel")
    assert 'data-ripple="iso-scene"' in diagram_prompt and "data-anatomy-film" not in diagram_prompt
    either = build_media_prompt(profile(), IDEA, "reel")
    assert "data-anatomy-film" in either and 'data-ripple="iso-scene"' in either
    assert "which most do" not in either
    typed = SocialProfile(
        id="p", workspace_id="w", description={"product": "Cold brew kit", "audience": None}
    )
    assert "Product: Cold brew kit" in build_media_prompt(typed, IDEA, "reel")
    poster = build_media_prompt(profile("ecommerce"), IDEA, "poster")
    assert "Home baristas" not in poster
    assert 'data-ripple="figure"' in ADD_MOTION_GRAPHIC_DESCRIPTION


def test_check_reel_holds_the_reel_to_its_routed_visual():
    diagram = comp('<div data-ripple="figure" data-figure="heart"></div>')
    assert check_reel(diagram, "diagram")[1] is None
    assert "data-anatomy-film" in check_reel(diagram, "film")[1]
    assert check_reel(comp(film()), "either")[1] is None
    assert "data-ripple" in check_reel(comp(film()), "diagram")[1]


async def test_a_rejected_reel_gets_one_repair_pass_with_the_error(monkeypatch):
    prompts: list[str] = []
    answers = iter([comp(film({**SPEC, "shots": [{"part": "speaker"}]})), comp(film())])

    async def fake_run(workspace_id, definition, prompt, session):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(ideas, "run_pinned_agent", fake_run)
    assert await ideas.agent_make_media(profile("ecommerce"), IDEA, "reel") == comp(film())
    assert len(prompts) == 2 and "do not exist: speaker" in prompts[1]

    prompts.clear()
    answers = iter(["nope", "still nope"])
    assert await ideas.agent_make_media(profile("ecommerce"), IDEA, "reel") == ""
    assert len(prompts) == 2
