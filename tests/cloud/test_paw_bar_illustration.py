# tests/cloud/test_paw_bar_illustration.py: Ripple's ``illustration`` widget on the
# ripple concierge profile.
#
# ``illustration_svg.svg_violation`` is the server's half of the illustration contract
# (paw-workspace docs/design/drafts/2026-10-09-ripple-illustration-svg.md): anything in
# the hostile set or over a cap is refused, a harmless unknown (a filter element, a
# class attribute) passes. ``_HOSTILE`` pins one case per hostile rule and cap, each
# with the reason it must be refused for, so a case refused by accident (a parse
# error standing in for the real rule) fails. A use may not point at a subtree
# holding a use (at most 40 uses); text nodes are never held to the value rules.
# On the card side, an illustration passes the
# whole ripple validator end to end, is refused with a clear reason when its svg is
# hostile or its props are wrong, is unknown on the pawbar profile, and streams to a
# ``card.final`` even though its svg (an xmlns URL, ``url(#g)``) would break the plain
# text rules: no prefix of it is flagged mid-stream. The prompt lists the widget and
# its rule, and the rule's own sample markup passes the policy.


from __future__ import annotations

import json

import pytest

from tests.cloud.test_paw_bar_card_streaming import _events, _fence

GOOD_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink' "
    "viewBox='0 0 200 120'>"
    "<title>A sun</title>"
    "<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'>"
    "<stop offset='0' stop-color='#fde68a'/><stop offset='1' stop-color='#f59e0b'/>"
    "</linearGradient><circle id='a' r='8' fill='url(#g)'/></defs>"
    "<rect width='200' height='120' fill='#e0f2fe'/>"
    "<g transform='translate(100 60)'>"
    "<circle r='30' fill='url( #g )'>"
    "<animate attributeName='r' values='28;32;28' dur='2s' repeatCount='indefinite'/>"
    "</circle>"
    "<use href='#a' x='50'><animateTransform attributeName='transform' type='rotate' "
    "from='0' to='360' dur='00:04' repeatCount='3'/></use>"
    "<use xlink:href='#a' x='-50'/>"
    "</g>"
    # Text is only held to the script schemes: "data:" in a caption is not a link.
    "<text x='100' y='112' text-anchor='middle' font-size='10'>Metadata: 5 &amp; more</text>"
    "</svg>"
)


def _svg(inner: str, root: str = "") -> str:
    return f"<svg viewBox='0 0 100 100'{root}>{inner}</svg>"


def _violation(markup: str) -> str | None:
    from pocketpaw_ee.paw_bar.illustration_svg import svg_violation

    return svg_violation(markup)


_HOSTILE: list[tuple[str, str]] = [
    (_svg("<set attributeName='href' to='javascript:alert(1)'/>"), "animation of 'href'"),
    (
        _svg("<animate attributeName='xlink:href' values='javascript:alert(1)'/>"),
        "animation of 'xlink:href'",
    ),
    (_svg("<set attributeName='onload' to='x'/>"), "animation of 'onload'"),
    (_svg("<a href='javascript:alert(1)'><text>x</text></a>"), "a a element"),
    (_svg("<image href='http://x/y.png'/>"), "a image element"),
    (_svg("<rect fill='url(http://x/#a)'/>"), "url() that is not url(#id)"),
    (_svg("<rect fill='url(#a) red'/>"), "url() that is not url(#id)"),
    (_svg("<style>@import url(http://x)</style>"), "a style element"),
    (_svg("<rect style='fill:red'/>"), "a style attribute"),
    (
        "<?xml version='1.0'?><!DOCTYPE lolz [<!ENTITY lol 'lol'>"
        "<!ENTITY lol2 '&lol;&lol;&lol;&lol;'>]>" + _svg("<text>&lol2;</text>"),
        "a DOCTYPE",
    ),
    (_svg("<!ENTITY x 'y'>"), "an ENTITY"),
    (_svg("<text>&nbsp;</text>"), "the entity &nbsp;"),
    ("<?xml-stylesheet href='http://x/s.css'?>" + _svg(""), "a stylesheet"),
    (_svg("<text><![CDATA[<script>x</script>]]></text>"), "CDATA holding markup"),
    (_svg("<foreignObject><iframe src='http://x'/></foreignObject>"), "a foreignObject element"),
    (_svg("", root=" onload='alert(1)'"), "event handler attribute (onload)"),
    (_svg("<rect OnClick='alert(1)'/>"), "event handler attribute (OnClick)"),
    (_svg("<g>" * 1000 + "</g>" * 1000), "nesting deeper than 24"),
    (_svg("<g>" * 24 + "</g>" * 24), "nesting deeper than 24"),
    (_svg("<rect/>" * 400), "more than 400 elements"),
    (_svg(" " * 24_001), "more than 24000 characters"),
    (_svg("<animate attributeName='r' values='1;2' dur='0.01s'/>"), "a dur under 0.5s"),
    (_svg("<animate attributeName='r' values='1;2' dur='400ms'/>"), "a dur under 0.5s"),
    (_svg("<animate attributeName='r' values='1;2' dur='fast'/>"), "a dur under 0.5s"),
    (
        _svg("<animate attributeName='r' values='1;2' dur='1s' repeatCount='1001'/>"),
        "a repeatCount over 1000",
    ),
    (
        _svg("<animate attributeName='r' values='1;2' dur='1s'/>" * 41),
        "more than 40 animation elements",
    ),
    (_svg("<use href='http://x#a'/>"), "an href that is not #id"),
    (_svg("<rect href='#a'/>"), "an href on rect"),
    (_svg("<rect fill='JaVa\tScRiPt:alert(1)'/>"), "a script or data link"),
    (_svg("<rect fill='JaVa&#9;ScRiPt:alert(1)'/>"), "a script or data link"),
    (_svg("<rect fill='data:x'/>"), "a script or data link"),
    (_svg("<rect fill='expression(alert(1))'/>"), "a script or data link"),
    # A CSS escape still spells url( for the browser.
    (_svg("<rect fill='\\75 rl(http://x/)'/>"), "url() that is not url(#id)"),
    (
        _svg("<circle id='a' r='2'/><use id='b' href='#a'/><use href='#b'/>"),
        "a use pointing at a use",
    ),
    (
        _svg("<g id='a'><circle r='2'/><use href='#c'/></g><circle id='c'/><use href='#a'/>"),
        "a use pointing at a use",
    ),
    (_svg("<g id='a'><use href='#a'/></g>"), "a use pointing at a use"),  # its own ancestor
    (_svg("<circle id='a' r='2'/>" + "<use href='#a'/>" * 41), "more than 40 use elements"),
    (
        _svg("<h:div xmlns:h='http://www.w3.org/1999/xhtml'>x</h:div>"),
        "an element outside the SVG namespace",
    ),
    ("<g viewBox='0 0 1 1'/>", "a root that is not svg"),
    (_svg("<rect>"), "markup that does not parse"),
    (_svg("<use xlink:href='#a'/>"), "markup that does not parse"),  # unbound prefix
]


@pytest.mark.parametrize(("markup", "reason"), _HOSTILE)
def test_hostile_or_over_cap_svg_is_refused_for_its_rule(markup, reason):
    found = _violation(markup)
    assert found is not None and reason in found, found


@pytest.mark.parametrize(
    "markup",
    [
        GOOD_SVG,
        # A harmless unknown element and attribute pass; the widget drops them.
        _svg(
            "<filter id='f'><feGaussianBlur stdDeviation='2'/></filter>"
            "<rect class='x' filter='url(#f)' width='10' height='10'/>"
        ),
        "<?xml version='1.0' encoding='UTF-8'?>" + _svg("<circle r='4'/>"),
        _svg("<set attributeName='visibility' to='hidden' begin='1s'/>"),  # no dur: fine
        _svg("<rect/>" * 398),
        _svg("<g>" * 23 + "</g>" * 23),
        _svg("<animate attributeName='r' values='1;2' dur='0.5s'/>" * 40),
        _svg("<text>&lt;&#65;&#x42;&quot;&apos;&gt;</text>"),
        # Value rules read attribute values only, never text.
        _svg("<text>data: javascript:alert(1) url(http://x) expression(1)</text>"),
        _svg("<circle id='a' r='2'/>" + "<use href='#a'/>" * 40),
        _svg("<g id='a'><circle r='2'/><circle r='4'/></g><use href='#a'/><use href='#zz'/>"),
    ],
)
def test_good_svg_passes(markup):
    assert _violation(markup) is None


def test_plain_model_svg_with_no_xmlns_is_svg():
    assert _violation("<svg viewBox='0 0 10 10'><rect width='4' height='4'/></svg>") is None
    xhtml = "<svg xmlns='http://www.w3.org/1999/xhtml' viewBox='0 0 10 10'/>"
    assert "outside the SVG namespace" in (_violation(xhtml) or "")


def test_url_is_matched_in_any_case():
    assert "url() that is not url(#id)" in (_violation(_svg("<rect fill='URL(http://x)'/>")) or "")
    assert _violation(_svg("<rect fill='URL(#g)'/>")) is None


def test_deep_nesting_never_raises_recursion():
    deep = "<svg>" + "<g>" * 3000 + "</g>" * 3000 + "</svg>"
    assert len(deep) <= 24_000
    assert "nesting deeper" in (_violation(deep) or "")


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def _card(svg: str = GOOD_SVG, **props) -> dict:
    return {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "text", "props": {"text": "How the sun shines"}},
                {
                    "type": "illustration",
                    "props": {"svg": svg, "title": "A sun", **props},
                },
            ],
        },
        "state": {},
    }


def test_an_illustration_card_passes_the_ripple_validator_end_to_end():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card, validate_and_hydrate

    spec = _card(caption="It glows", max_height=240)
    assert validate_and_hydrate(spec, [], profile=RIPPLE_PROFILE) == spec
    fence = render_card(json.dumps(spec), [], profile=RIPPLE_PROFILE)
    assert fence is not None
    assert json.loads(fence.removeprefix("```pawbar-card\n").removesuffix("\n```")) == spec


def test_a_null_optional_prop_reads_as_absent():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, validate_and_hydrate

    spec = _card(caption=None, max_height=None)
    assert validate_and_hydrate(spec, [], profile=RIPPLE_PROFILE) == spec


def test_illustration_is_unknown_on_the_pawbar_profile():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, validate_and_hydrate

    assert validate_and_hydrate(_card(), [], profile=PAWBAR_PROFILE) is None


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (_card(_svg("<rect onclick='alert(1)'/>")), "an illustration with an event handler"),
        (_card(_svg("<image href='/x.png'/>")), "an illustration with a image element"),
        (_card(_svg("<text>{state.secret}</text>")), "an expression in an illustration"),
        (_card("{state.art}"), "an expression in an illustration"),
        (_card(max_height=50), "max_height outside 80..640"),
        (_card(max_height=641), "max_height outside 80..640"),
        (_card(max_height=True), "max_height outside 80..640"),
        (_card(max_height="200"), "max_height outside 80..640"),
        (_card(caption=["x"]), "caption that is not text"),
        (_card(svg=None), "needs svg and title text"),
        (
            {"ui": {"type": "illustration", "props": {"svg": GOOD_SVG}}},
            "needs svg and title text",
        ),
        (
            {
                "ui": {
                    "type": "illustration",
                    "props": {"svg": GOOD_SVG, "title": "x"},
                    "on_click": {"action": "toast", "message": "hi"},
                }
            },
            "a handler or bind on an illustration",
        ),
        (
            {
                "ui": {
                    "type": "illustration",
                    "props": {"svg": GOOD_SVG, "title": "x"},
                    "bind": "{state.x}",
                }
            },
            "a handler or bind on an illustration",
        ),
    ],
)
def test_a_bad_illustration_refuses_the_card_with_a_clear_reason(spec, reason):
    from pocketpaw_ee.paw_bar import card_spec

    profile = card_spec.RIPPLE_PROFILE
    assert card_spec.validate_and_hydrate(spec, [], profile=profile) is None
    with pytest.raises(card_spec._Reject, match=reason):
        card_spec._check_strict(spec, [], False, profile)


def test_a_caption_with_data_text_passes():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, validate_and_hydrate

    spec = _card(caption="Big data: 5 GB a day")
    assert validate_and_hydrate(spec, [], profile=RIPPLE_PROFILE) == spec


def test_the_title_and_caption_still_get_the_text_rules():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, validate_and_hydrate

    assert validate_and_hydrate(_card(caption="javascript:x"), [], profile=RIPPLE_PROFILE) is None


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("size", [1, 3, 7])
def test_an_illustration_card_streams_to_final_and_no_prefix_is_flagged(size):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, PartialScan

    spec = _card()
    body = json.dumps(spec)
    scan = PartialScan(RIPPLE_PROFILE)
    assert not any(scan.feed(body[i : i + size]) for i in range(0, len(body), size))
    text = _fence(body + "\n")
    events = _events([text[i : i + size] for i in range(0, len(text), size)])
    assert not any(name == "card.rejected" for name, _ in events)
    assert events[-1] == ("card.final", {"card_id": "c1", "card": spec})


def test_a_hostile_illustration_streams_then_is_rejected_at_the_close():
    spec = _card(_svg("<image href='/x.png'/>"))
    text = _fence(json.dumps(spec) + "\n")
    events = _events([text[i : i + 5] for i in range(0, len(text), 5)])
    assert any(name == "card.delta" for name, _ in events)
    assert events[-1] == ("card.rejected", {"card_id": "c1", "reason": "invalid"})


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def test_the_ripple_paragraph_teaches_the_illustration():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import _cards_paragraph

    text = _cards_paragraph([], profile=RIPPLE_PROFILE)
    assert "   - illustration {svg, title, caption?, max_height?}: " in text
    rule = next(line for line in text.splitlines() if "add an illustration" in line)
    assert "single quotes" in rule and "viewBox" in rule and "0.5s" in rule
    assert "never xlink:href" in rule and "40 uses" in rule
    sample = rule[rule.index("<svg") : rule.index("</svg>") + len("</svg>")]
    assert _violation(sample) is None
