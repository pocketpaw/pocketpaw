# tests/cloud/test_paw_bar_illustration_hardening.py: the edges of the illustration
# SVG policy (``illustration_svg.svg_violation``) where a looser server would accept
# what the browser-side rebuild treats as dangerous.
#
# Each case pins the reason it must be refused for, so a case refused by accident (a
# parse error standing in for the real rule) fails. Covered: attributeName matched
# exactly against the closed list and no attributeType; href only on use/mpath and
# only as an exact ``#id`` that exists; ``url(`` only as an exact ``url(#id)`` that
# exists; rules read parsed values (numeric character references decoded); begin/end
# refs and the id charset; the shared caps (numbers, list lengths, mask/clipPath
# cycles, ``d`` length); odd markup (prefixed svg root, CDATA, PIs, duplicate
# attributes, a BOM, comments); any backslash in a value; a blank title. The last
# section proves the streaming skip for ``svg`` never reaches the final check: a card
# whose svg is malformed or truncated is refused at the close, repaired body or not.

from __future__ import annotations

import json

import pytest

from tests.cloud.test_paw_bar_card_streaming import _events, _fence
from tests.cloud.test_paw_bar_illustration import GOOD_SVG, _card, _svg, _violation

XLINK = " xmlns:xlink='http://www.w3.org/1999/xlink'"
SVG_PREFIX = "<svg:svg xmlns:svg='http://www.w3.org/2000/svg' viewBox='0 0 1 1'>{}</svg:svg>"


def _list(n: int, entry: str = "1") -> str:
    return ";".join([entry] * n)


_REFUSED: list[tuple[str, str, str]] = [
    # 1. attributeName: exact, case-sensitive, closed list. No attributeType.
    ("1", _svg("<set attributeName='HREF' to='x' dur='1s'/>"), "animation of 'HREF'"),
    ("1", _svg("<set attributeName='Href' to='x' dur='1s'/>"), "animation of 'Href'"),
    (
        "1",
        _svg("<set attributeName='xlink:href' to='#a' dur='1s'/><g id='a'/>", XLINK),
        "animation of 'xlink:href'",
    ),
    ("1", _svg("<set attributeName='svg:fill' to='red' dur='1s'/>"), "animation of 'svg:fill'"),
    ("1", _svg("<set attributeName='Fill' to='red' dur='1s'/>"), "animation of 'Fill'"),
    (
        "1",
        _svg("<set attributeName='fill' attributeType='CSS' to='red' dur='1s'/>"),
        "an attributeType attribute",
    ),
    (
        "1",
        _svg("<animate attributeName='r' attributeType='XML' values='1;2' dur='1s'/>"),
        "an attributeType attribute",
    ),
    # 2. href: never on an animation; on use/mpath exactly #<existing id>, one form.
    (
        "2",
        _svg("<circle id='a'/><animate href='#a' attributeName='r' values='1;2' dur='1s'/>"),
        "an href on animate",
    ),
    (
        "2",
        _svg("<circle id='a'/><animateTransform href='#a' attributeName='transform' dur='1s'/>"),
        "an href on animateTransform",
    ),
    ("2", _svg("<path id='a'/><animateMotion href='#a' dur='1s'/>"), "an href on animateMotion"),
    (
        "2",
        _svg("<circle id='a'/><set xlink:href='#a' attributeName='r' to='1'/>", XLINK),
        "an href on set",
    ),
    ("2", _svg("<use href='#nope'/>"), "a reference to a missing id (#nope)"),
    (
        "2",
        _svg("<path id='p'/><animateMotion dur='1s'><mpath href='#nope'/></animateMotion>"),
        "a reference to a missing id (#nope)",
    ),
    ("2", _svg("<circle id='a'/><use href=' #a'/>"), "an href that is not #id"),
    ("2", _svg("<circle id='a'/><use href='&#9;#a'/>"), "an href that is not #id"),
    ("2", _svg("<circle id='a'/><use href='&#1;#a'/>"), "markup that does not parse"),
    ("2", _svg("<circle id='a'/><use href='&#x7f;#a'/>"), "an href that is not #id"),
    ("2", _svg("<circle id='a'/><use href='#a '/>"), "an href that is not #id"),
    ("2", _svg("<circle id='a'/><use href='#%61'/>"), "an href that is not #id"),
    ("2", _svg("<circle id='a'/><use href='%23a'/>"), "an href that is not #id"),
    (
        "2",
        _svg("<circle id='a'/><use href='#a' xlink:href='#a'/>", XLINK),
        "both href and xlink:href on one element",
    ),
    # 3. url(: exactly url(#<existing id>).
    ("3", _svg("<radialGradient id='g'/><rect fill='url( #g )'/>"), "url() that is not url(#id)"),
    ("3", _svg("<radialGradient id='g'/><rect fill='url(#g )'/>"), "url() that is not url(#id)"),
    ("3", _svg("<radialGradient id='g'/><rect fill=' url(#g)'/>"), "url() that is not url(#id)"),
    (
        "3",
        _svg("<radialGradient id='g'/><rect fill='url(/**/#g)'/>"),
        "url() that is not url(#id)",
    ),
    (
        "3",
        _svg("<radialGradient id='g'/><rect fill='url(#g&#10;)'/>"),
        "url() that is not url(#id)",
    ),
    ("3", _svg("<radialGradient id='g'/><rect fill='URL(#g)'/>"), "url() that is not url(#id)"),
    ("3", _svg("<radialGradient id='g'/><rect fill='Url(#g)'/>"), "url() that is not url(#id)"),
    (
        "3",
        _svg("<radialGradient id='g'/><rect fill='url(#g)url(#g)'/>"),
        "url() that is not url(#id)",
    ),
    (
        "3",
        _svg("<radialGradient id='a'/><rect fill='url(#a) url(http://x/y)'/>"),
        "url() that is not url(#id)",
    ),
    ("3", _svg("<rect fill='url(#nope)'/>"), "a reference to a missing id (#nope)"),
    # 4. Rules read parsed values: ElementTree decodes numeric character references.
    ("4", _svg("<rect fill='&#x75;rl(http://x)'/>"), "url() that is not url(#id)"),
    ("4", _svg("<rect fill='&#117;rl(http://x)'/>"), "url() that is not url(#id)"),
    (
        "4",
        _svg("<set attributeName='fill' to='&#x6a;avascript:x' dur='1s'/>"),
        "a script or data link",
    ),
    # 5. begin/end name existing ids only; ids are [A-Za-z0-9_-]+.
    (
        "5",
        _svg("<set attributeName='fill' to='red' begin='nope.click' dur='1s'/>"),
        "a reference to a missing id (#nope)",
    ),
    (
        "5",
        _svg("<g id='a'/><set attributeName='fill' to='red' end='0s; nope.end+1s' dur='1s'/>"),
        "a reference to a missing id (#nope)",
    ),
    (
        "5",
        _svg("<set attributeName='fill' to='red' begin='nope.repeat(2)' dur='1s'/>"),
        "a reference to a missing id (#nope)",
    ),
    ("5", _svg("<rect id='a.b'/>"), "an id outside [A-Za-z0-9_-]"),
    ("5", _svg("<rect id='a b'/>"), "an id outside [A-Za-z0-9_-]"),
    ("5", _svg("<rect id='café'/>"), "an id outside [A-Za-z0-9_-]"),
    ("5", _svg("<rect id=''/>"), "an id outside [A-Za-z0-9_-]"),
    # 6. Shared caps.
    ("6", _svg("<rect width='1000001'/>"), "a number over 1e+06"),
    ("6", _svg("<rect x='-1e7'/>"), "a number over 1e+06"),
    ("6", _svg("<path d='M0 0L1e7 0'/>"), "a number over 1e+06"),
    ("6", _svg("<g transform='translate(0 9e999)'/>"), "a number over 1e+06"),
    (
        "6",
        _svg(f"<animate attributeName='r' dur='1s' values='{_list(201)}'/>"),
        "a values list over 200 entries",
    ),
    (
        "6",
        _svg(f"<animate attributeName='r' dur='1s' keyTimes='{_list(201, '0')}'/>"),
        "a keyTimes list over 200 entries",
    ),
    (
        "6",
        _svg(f"<animate attributeName='r' dur='1s' keySplines='{_list(201, '0 0 1 1')}'/>"),
        "a keySplines list over 200 entries",
    ),
    ("6", _svg("<mask id='m'><rect mask='url(#m)'/></mask>"), "references itself"),
    ("6", _svg("<clipPath id='c' clip-path='url(#c)'/>"), "references itself"),
    (
        "6",
        _svg(
            "<clipPath id='a'><rect clip-path='url(#b)'/></clipPath>"
            "<clipPath id='b'><rect clip-path='url(#a)'/></clipPath>"
        ),
        "references itself",
    ),
    (
        "6",
        _svg(
            "<mask id='a'><g mask='url(#b)'/></mask><clipPath id='b'><g mask='url(#a)'/></clipPath>"
        ),
        "references itself",
    ),
    ("6", _svg("<path d='M0 0" + " L1 1" * 1600 + "'/>"), "a d over 8000 characters"),
    # 7. Odd markup.
    ("7", SVG_PREFIX.format("<svg:script>x</svg:script>"), "a script element"),
    ("7", SVG_PREFIX.format("<svg:rect onclick='x'/>"), "event handler attribute (onclick)"),
    (
        "7",
        SVG_PREFIX.format("<svg:use href='http://x/#a'/>"),
        "an href that is not #id",
    ),
    ("7", _svg("<text><![CDATA[hi]]></text>"), "a CDATA section"),
    ("7", _svg("<text><![cdata[hi]]></text>"), "a CDATA section"),
    ("7", _svg("<?foo bar?><rect/>"), "a processing instruction"),
    ("7", "<?foo?>" + _svg(""), "a processing instruction"),
    ("7", "<?xml version='1.0'?><?foo?>" + _svg(""), "a processing instruction"),
    ("7", _svg("<rect fill='red' fill='blue'/>"), "markup that does not parse"),
    (
        "7",
        _svg(
            "<circle id='a'/><use xlink:href='#a' x:href='#a'/>",
            XLINK + " xmlns:x='http://www.w3.org/1999/xlink'",
        ),
        "markup that does not parse",
    ),
    # Backslashes are refused outright (the landing and the widget do the same).
    ("bs", _svg("<rect fill='\\72 ed'/>"), "a backslash in a value"),
    ("bs", _svg("<rect fill='red\\'/>"), "a backslash in a value"),
    ("bs", _svg("<text font-family='a\\b'>x</text>"), "a backslash in a value"),
]


@pytest.mark.parametrize(("prop", "markup", "reason"), _REFUSED)
def test_the_validator_refuses(prop, markup, reason):
    found = _violation(markup)
    assert found is not None and reason in found, (prop, found)


@pytest.mark.parametrize(
    "markup",
    [
        GOOD_SVG,
        # The prefixed root is walked as SVG: its benign children pass.
        SVG_PREFIX.format("<svg:rect width='1' height='1' fill='#1e78f2'/>"),
        # A forward reference resolves after the walk.
        _svg("<rect fill='url(#g)'/><linearGradient id='g'/>"),
        _svg("<use href='#a'/><circle id='a' r='2'/>"),
        _svg("<path id='p' d='M0 0'/><animateMotion dur='1s'><mpath href='#p'/></animateMotion>"),
        # Clock values are not id refs; event refs name existing ids.
        _svg(
            "<g id='a'/><set attributeName='fill' to='red' "
            "begin='1.5s; a.click; a.end+0.5s; a.repeat(2); click; -0.5s' dur='1s'/>"
        ),
        # Hex colours and #ids are not numbers; the caps' own edges pass.
        _svg("<rect id='a1e9' fill='#1e9999' stroke='#1E7' width='1000000' x='-1e6'/>"),
        _svg(f"<animate attributeName='r' dur='1s' values='{_list(200)};'/>"),
        _svg("<path d='M0 0" + " L1 1" * 1599 + "'/>"),
        _svg("<clipPath id='a'><rect clip-path='url(#b)'/></clipPath><clipPath id='b'/>"),
        _svg("<!-- a comment, <b>with</b> tags --><rect/>"),
        "﻿" + _svg("<rect/>"),
        "﻿<?xml version='1.0' encoding='UTF-8'?>" + _svg("<rect/>"),
    ],
)
def test_the_validator_accepts(markup):
    assert _violation(markup) is None


@pytest.mark.parametrize("markup", ["﻿", "﻿﻿<svg/>", "﻿<?xml?>", ""])
def test_a_bom_or_empty_markup_never_raises(markup):
    assert isinstance(_violation(markup), str)


# --------------------------------------------------------------------------- #
# The card: title, and the final check after streaming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
def test_a_blank_title_refuses_the_card(title):
    from pocketpaw_ee.paw_bar import card_spec

    spec = _card()
    spec["ui"]["children"][1]["props"]["title"] = title
    profile = card_spec.RIPPLE_PROFILE
    assert card_spec.validate_and_hydrate(spec, [], profile=profile) is None
    with pytest.raises(card_spec._Reject, match="an illustration with an empty title"):
        card_spec._check_strict(spec, [], False, profile)


def _stream(body: str) -> list[tuple[str, object]]:
    text = _fence(body + "\n")
    return _events([text[i : i + 4] for i in range(0, len(text), 4)])


def _assert_refused_at_close(body: str) -> None:
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, PartialScan, render_card

    # Mid-stream nothing is flagged (the svg skip), so the close alone refuses it.
    assert not PartialScan(RIPPLE_PROFILE).feed(body)
    assert render_card(body, [], profile=RIPPLE_PROFILE) is None
    events = _stream(body)
    assert any(name == "card.delta" for name, _ in events)
    assert not any(name == "card.final" for name, _ in events)
    assert events[-1] == ("card.rejected", {"card_id": "c1", "reason": "invalid"})


@pytest.mark.parametrize(
    "svg",
    [
        "<svg viewBox='0 0 10 10'><rect",  # truncated mid-tag
        "<svg viewBox='0 0 10 10'><rect/>",  # truncated before </svg>
        "<svg viewBox='0 0 10 10'><g><rect/></svg>",  # malformed nesting
    ],
)
def test_a_malformed_or_truncated_svg_is_refused_by_the_final_check(svg):
    _assert_refused_at_close(json.dumps(_card(svg)))


def test_a_repaired_body_still_runs_the_svg_check():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    # Control: dropping the closers of a good card is repaired and passes.
    good = json.dumps(_card()).removesuffix(', "state": {}}').removesuffix("]}")
    assert render_card(good, [], profile=RIPPLE_PROFILE) is not None
    # The same repair with a truncated svg is refused by the svg check.
    bad = json.dumps(_card("<svg viewBox='0 0 1 1'><rect/>"))
    _assert_refused_at_close(bad.removesuffix(', "state": {}}').removesuffix("]}"))


def test_a_body_cut_inside_the_svg_string_is_refused_at_the_close():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    body = json.dumps(_card())
    cut = body[: body.index("<rect width")]
    assert render_card(cut, [], profile=RIPPLE_PROFILE) is None
    events = _stream(cut)
    assert not any(name == "card.final" for name, _ in events)
    assert events[-1][0] == "card.rejected"


def test_the_stream_ending_mid_svg_is_truncated_never_final():
    body = json.dumps(_card())
    text = _fence(body)[: len("```pawbar-card\n") + body.index("<rect width")]
    events = _events([text[i : i + 4] for i in range(0, len(text), 4)])
    assert events[-1] == ("card.rejected", {"card_id": "c1", "reason": "truncated"})
    assert not any(name == "card.final" for name, _ in events)


def test_an_svg_key_outside_an_illustration_is_still_refused_at_the_close():
    # The streaming skip covers every "svg" key; the final text rules do not.
    body = json.dumps({"ui": {"type": "text", "props": {"text": "hi", "svg": "javascript:x"}}})
    _assert_refused_at_close(body)
