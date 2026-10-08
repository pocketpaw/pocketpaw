# tests/cloud/test_paw_bar_ripple_profile.py — the "ripple" concierge card profile.
#
# A site whose ``concierge_ui_profile`` is "ripple" gets full-catalog Ripple cards
# (``card_spec.RIPPLE_PROFILE``: the vendored ripple-manifest.json, 400 nodes,
# depth 16, 64,000 chars, handlers checked wherever a widget keeps them), the
# Ripple cards paragraph with no catalog, actions or lead capture, and an 8,000
# token reply cap. Every other site keeps ``PAWBAR_PROFILE``. Also covered: the
# per-site daily spend cap over the global one, and both settings through the
# settings PATCH and its response. The accepted card is ripple's recorded
# explainer scenario (tests/fixtures/ripple_explainer_card.json, from ripple
# origin/main 09a56a6, packages/svelte/src/routes/live/fixtures/explainer.json,
# its chunks joined and wrapped as {ui, state}).

# ruff: noqa: F811 — pytest fixtures imported by name

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    _site as _settings_site,
)
from tests.cloud.test_paw_bar_concierge_settings import (  # noqa: F401 — fixture
    client,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "ripple_explainer_card.json"

# Refreshing ripple-manifest.json: see ee/pocketpaw_ee/paw_bar/ripple-manifest.json.source
# (download the release asset, drop the widget examples, update both hashes).
_RIPPLE_MANIFEST_SHA256 = "451f17b6ef0afbc8196cb3a3095bcf531704bab2d16e39baae419cab8986d200"


def _card() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _body(spec: dict) -> str:
    return json.dumps(spec)


def _chain(depth: int, kind: str = "flex") -> dict:
    node: dict = {"type": "text", "props": {"text": "x"}}
    for _ in range(depth - 1):
        node = {"type": kind, "props": {}, "children": [node]}
    return {"ui": node}


# --------------------------------------------------------------------------- #
# card_spec
# --------------------------------------------------------------------------- #


def test_the_vendored_ripple_manifest_has_not_drifted():
    from pocketpaw_ee.paw_bar import card_spec

    raw = card_spec.RIPPLE_MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == _RIPPLE_MANIFEST_SHA256
    assert card_spec.RIPPLE_MANIFEST["version"] == "0.8.0"
    assert len(card_spec.RIPPLE_MANIFEST["widgets"]) == 189
    assert len(card_spec.RIPPLE_PROFILE.widget_types) == 189 - len(card_spec.RIPPLE_DEFERRED)


def test_the_pawbar_profile_is_todays_rules():
    from pocketpaw_ee.paw_bar import card_spec

    RIPPLE_STRICT = card_spec.RIPPLE_PROFILE.strict
    p = card_spec.PAWBAR_PROFILE
    assert (p.widget_types, p.actions) == (card_spec.WIDGET_TYPES, card_spec.SPEC_ACTIONS)
    assert (p.max_nodes, p.max_depth, p.max_chars) == (80, 8, 32_000)
    assert p.detailed is None and p.strict is False and RIPPLE_STRICT

    r = card_spec.RIPPLE_PROFILE
    assert r.actions == card_spec.SPEC_ACTIONS | {"flow", "branch", "validate", "toast"}
    assert (r.max_nodes, r.max_depth, r.max_chars) == (400, 16, 64_000)


def test_a_recorded_full_catalog_card_passes_ripple_and_not_pawbar():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    body = _body(_card())
    out = render_card(body, [], profile=RIPPLE_PROFILE)
    assert out is not None
    assert json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0]) == _card()
    # Today's widget set has no card, grid, stat, slider...: dropped whole.
    assert render_card(body, []) is None


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        (
            {"ui": {"type": "flex", "children": [{"type": "text"}] * 400}},
            "401 nodes",
        ),
        (_chain(17), "depth 17"),
        (
            {"ui": {"type": "text", "props": {"text": "x" * 64_001}}},
            "over 64,000 chars",
        ),
        (
            {"ui": {"type": "button", "on_click": {"action": "emit", "target": "pay"}}},
            "emit to a non-host event",
        ),
        (
            {"ui": {"type": "button", "on_click": {"action": "api", "url": "/x"}}},
            "an action outside the profile's set",
        ),
        (
            {"ui": {"type": "ripple-frame", "props": {"spec": {"ui": {"type": "text"}}}}},
            "a deferred widget",
        ),
    ],
)
def test_ripple_refuses_what_breaks_its_rules(spec, why):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    assert render_card(_body(spec), [], profile=RIPPLE_PROFILE) is None, why


def test_ripple_bounds_are_inclusive():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    wide = {"ui": {"type": "flex", "children": [{"type": "text"}] * 399}}
    assert render_card(_body(wide), [], profile=RIPPLE_PROFILE) is not None
    assert render_card(_body(_chain(16)), [], profile=RIPPLE_PROFILE) is not None
    # The same depth is past paw-bar's 8.
    assert render_card(_body(_chain(16)), []) is None


def test_ripple_allows_a_declared_host_event():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    spec = {"ui": {"type": "button", "on_click": {"action": "emit", "target": "checkout"}}}
    assert render_card(_body(spec), [], verbs=["checkout"], profile=RIPPLE_PROFILE)
    assert render_card(_body(spec), [], verbs=[], profile=RIPPLE_PROFILE) is None


@pytest.mark.parametrize(
    "node",
    [
        # A handler prop that is not on_*.
        {
            "type": "form-layout",
            "props": {"submitActions": [{"action": "api", "url": "https://x.test"}]},
        },
        # A handler inside a labelled button list.
        {
            "type": "order-status",
            "props": {"actions": [{"label": "Go", "actions": {"action": "navigate"}}]},
        },
        # A node kept in a prop, with its own on_click.
        {
            "type": "popover",
            "props": {
                "trigger": "Open",
                "content": {
                    "type": "flex",
                    "children": [{"type": "button", "on_click": {"action": "invoke_tool"}}],
                },
            },
        },
        # An emit to an undeclared event, nested.
        {
            "type": "wizard-layout",
            "props": {"finishActions": {"action": "emit", "target": "steal"}},
        },
    ],
)
def test_ripple_checks_handlers_wherever_a_widget_keeps_them(node):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    assert render_card(_body({"ui": node}), [], profile=RIPPLE_PROFILE) is None


def test_a_data_rows_own_action_field_is_not_a_handler():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    spec = {
        "ui": {
            "type": "audit-log",
            "props": {"entries": [{"action": "login", "actor": "Sam", "time": "09:00"}]},
        }
    }
    assert render_card(_body(spec), [], profile=RIPPLE_PROFILE) is not None
    ok = {"ui": {"type": "form-layout", "props": {"submitActions": {"action": "set"}}}}
    assert render_card(_body(ok), [], profile=RIPPLE_PROFILE) is not None


def test_the_ripple_listing_is_bounded_and_covers_every_widget():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, compact_manifest

    text = compact_manifest(RIPPLE_PROFILE)
    lines = text.splitlines()
    assert len(lines) == len(RIPPLE_PROFILE.widget_types)
    assert {line[2:].split(":")[0].split(" {")[0] for line in lines} == (
        RIPPLE_PROFILE.widget_types
    )
    assert len(text) < 20_000
    # The rules' widgets carry their props (each's node fields too).
    assert "- stat {label?, value" in text
    assert "- each {items, item_as?, index_as?}" in text


# --------------------------------------------------------------------------- #
# Security review (C = critical, I = important, M = minor)
# --------------------------------------------------------------------------- #


def _ripple(spec: dict, **kw):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    return render_card(_body(spec), [], profile=RIPPLE_PROFILE, **kw)


def _popover(content) -> dict:
    return {"ui": {"type": "popover", "props": {"trigger": "Open", "content": content}}}


# C1: a node kept in a prop is a node: its type, the bounds and the form rules apply.
@pytest.mark.parametrize(
    "spec",
    [
        _popover({"type": "embed", "props": {"url": "/x"}}),
        _popover({"type": "flex", "children": [{"type": "ripple-frame"}]}),
        {
            "ui": {
                "type": "settings-list",
                "props": {"items": [{"label": "x", "control": {"type": "richtext"}}]},
            }
        },
        _popover({"type": "made-up", "props": {}}),
        _popover({"type": "flex", "children": [{"type": "text"}] * 400}),
        _popover({"type": "product-card", "props": {"ids": ["p1"]}}),
        _popover(
            {
                "type": "form",
                "props": {"verb": "send_to_team", "fields": [{"name": "email", "type": "email"}]},
            }
        ),
    ],
)
def test_c1_nodes_inside_props_are_checked_as_nodes(spec):
    assert _ripple(spec) is None


def test_c1_an_allowed_node_inside_a_prop_passes():
    assert _ripple(_popover({"type": "text", "props": {"text": "hi"}})) is not None


# C2: an action is an action wherever it sits, in ui or in state.
@pytest.mark.parametrize(
    "spec",
    [
        {
            "ui": {
                "type": "comparison-layout",
                "props": {"items": [{"id": "a", "learn_more": {"action": "api", "url": "/x"}}]},
            }
        },
        {"ui": {"type": "text"}, "state": {"go": {"action": "navigate", "url": "/x"}}},
        {"ui": {"type": "text"}, "state": {"h": [{"action": "emit", "target": "steal"}]}},
        {"ui": {"type": "text", "props": {"meta": {"x": {"action": "invoke_tool"}}}}},
    ],
)
def test_c2_actions_are_checked_anywhere_in_ui_and_state(spec):
    assert _ripple(spec) is None


def test_c2_an_audit_logs_own_action_field_is_data():
    spec = {
        "ui": {
            "type": "audit-log",
            "props": {"entries": [{"id": 1, "actor": "Sam", "action": "navigate"}]},
        }
    }
    assert _ripple(spec) is not None


# C3: a ripple form never submits anywhere itself.
@pytest.mark.parametrize(
    "props",
    [
        {"fields": {"email": {"required": True}}, "action": "https://evil.test/collect"},
        {"fields": {"email": {"required": True}}, "action": "/signup", "method": "post"},
        {"fields": {"email": {"required": True}}, "method": "get"},
    ],
)
def test_c3_a_form_with_a_submit_target_is_refused(props):
    assert _ripple({"ui": {"type": "form", "props": props}}) is None


# I1: links and media point at this site or an allowed host only.
@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "image", "props": {"src": "javascript:alert(1)"}}},
        {"ui": {"type": "image", "props": {"src": " JaVa\tScRiPt:alert(1)"}}},
        {"ui": {"type": "image", "props": {"src": "%6A%61vascript:alert(1)"}}},
        {"ui": {"type": "image", "props": {"src": "&#106;avascript:alert(1)"}}},
        {"ui": {"type": "image", "props": {"src": "data:image/png;base64,AAAA"}}},
        {"ui": {"type": "image", "props": {"src": "https://evil.test/x.png"}}},
        {"ui": {"type": "image", "props": {"src": "//evil.test/x.png"}}},
        {"ui": {"type": "image", "props": {"src": "/\\evil.test/x.png"}}},
        {"ui": {"type": "cta", "props": {"label": "Go", "href": "vbscript:x"}}},
        {"ui": {"type": "cta", "props": {"label": "Go", "href": "{state.next}"}}},
        {"ui": {"type": "navbar", "props": {"links": [{"label": "x", "href": "file:///etc"}]}}},
        {"ui": {"type": "markdown", "props": {"content": "[win](javascript:alert(1))"}}},
        {"ui": {"type": "text", "props": {"text": "click <javascript:alert(1)>"}}},
        {"ui": {"type": "map", "props": {"tiles": "custom"}}},
        {"ui": {"type": "map", "props": {"tileUrl": "https://evil.test/{z}/{x}/{y}.png"}}},
        {"ui": {"type": "qr", "props": {"value": "x", "background": "url(javascript:x)"}}},
        {"ui": {"type": "image", "props": {"src": "{state.pic}"}}, "state": {"pic": "blob:x"}},
        {"ui": {"type": "text"}, "state": {"link": {"href": "https://evil.test"}}},
    ],
)
def test_i1_urls_must_be_relative_or_on_an_allowed_host(spec):
    assert _ripple(spec) is None


@pytest.mark.parametrize(
    "props",
    [
        {"src": "/img/a.png"},
        {"src": "#top"},
        {"src": ""},
    ],
)
def test_i1_relative_urls_pass(props):
    assert _ripple({"ui": {"type": "image", "props": props}}) is not None


def test_i1_an_allowed_host_passes_and_presets_and_colours_stay_usable():
    import dataclasses

    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    spec = {"ui": {"type": "image", "props": {"src": "https://cdn.example.com/a.png"}}}
    assert _ripple(spec) is None
    allowed = dataclasses.replace(RIPPLE_PROFILE, url_hosts=frozenset({"cdn.example.com"}))
    assert render_card(_body(spec), [], profile=allowed) is not None
    assert _ripple({"ui": {"type": "map", "props": {"tiles": "carto-light"}}}) is not None
    assert _ripple({"ui": {"type": "qr", "props": {"value": "x", "background": "#fff"}}})


def test_c1_node_props_are_derived_from_the_manifest():
    from pocketpaw_ee.paw_bar.card_spec import _NODE_PROPS

    assert _NODE_PROPS["popover"] == {"trigger", "content"}
    assert _NODE_PROPS["hover-card"] == {"trigger", "content"}
    assert _NODE_PROPS["tooltip"] == {"trigger"}
    assert _NODE_PROPS["context-menu"] == {"trigger"}


# I2: richtext renders trusted HTML.
def test_i2_richtext_is_not_a_ripple_widget():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE

    assert "richtext" not in RIPPLE_PROFILE.widget_types
    assert _ripple({"ui": {"type": "richtext", "props": {"html": "<b>x</b>"}}}) is None


# I3: card_ids reads as many nodes as the profile allows.
def test_i3_card_ids_reads_the_profiles_node_budget():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, card_ids

    kids = [{"type": "text"}] * 90 + [{"type": "product-card", "props": {"ids": ["p1"]}}]
    body = _body({"ui": {"type": "flex", "children": kids}})
    assert card_ids(body) == []
    assert card_ids(body, profile=RIPPLE_PROFILE) == ["p1"]


# M1: a pathologically deep card is dropped, never raised.
@pytest.mark.parametrize("depth", [200, 5_000])
def test_m1_a_very_deep_card_is_dropped_not_raised(depth):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    deep = "[" * depth + "]" * depth
    body = '{"ui": {"type": "text"}, "state": {"x": ' + deep + "}}"
    assert render_card(body, [], profile=RIPPLE_PROFILE) is None


def test_m1_a_body_too_deep_to_parse_is_dropped_not_passed_through():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    deep = "[" * 100_000 + "]" * 100_000
    with pytest.raises(RecursionError):
        json.loads(deep)
    body = '{"ui": {"type": "text"}, "state": {"x": ' + deep + "}}"
    # Before: the parse failure made it a "legacy" card, sent on verbatim.
    assert render_card(body, []) is None
    assert render_card(body, [], profile=RIPPLE_PROFILE) is None


# M2: a ripple form (keyed validation rules) is checked without crashing and is
# never mistaken for the lead card.
def test_m2_a_ripple_form_validates_and_is_not_a_lead_card():
    from pocketpaw_ee.paw_bar.card_spec import has_lead_form

    spec = {
        "ui": {
            "type": "form",
            "props": {"fields": {"email": {"required": True, "pattern": ".+@.+"}}},
            "children": [{"type": "input", "bind": "email"}],
        }
    }
    assert _ripple(spec) is not None
    assert has_lead_form(_body(spec)) is False


# --------------------------------------------------------------------------- #
# Adversarial re-review
# --------------------------------------------------------------------------- #


# R1: a URL is a URL under any key: absolute, protocol-relative and data/blob/file
# values are held to the policy wherever they sit; a URL-named key refuses
# expressions too.
@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "navbar", "props": {"ctaHref": "//evil.com"}}},
        {"ui": {"type": "navbar", "props": {"ctaHref": "https://evil.com"}}},
        {"ui": {"type": "navbar", "props": {"ctaHref": "http://evil.com"}}},
        {"ui": {"type": "navbar", "props": {"ctaHref": "data:text/html,<b>x</b>"}}},
        {"ui": {"type": "navbar", "props": {"ctaHref": "http:evil.com"}}},
        {"ui": {"type": "navbar", "props": {"ctaHref": "https:\\\\evil.com"}}},
        {"ui": {"type": "hero", "props": {"title": "x", "secondaryCtaHref": "https://evil.com"}}},
        {"ui": {"type": "marketing-hero", "props": {"ctaHref": "{state.next}"}}},
        {
            "ui": {
                "type": "model-viewer",
                "props": {"src": "/m.glb", "environmentImage": "https://evil.com/x.hdr"},
            }
        },
        {
            "ui": {
                "type": "comparison-layout",
                "props": {
                    "features": [{"key": "pic", "type": "image"}],
                    "items": [{"id": "a", "pic": "https://evil.com/x.png"}],
                },
            }
        },
        {"ui": {"type": "text"}, "state": {"tail": "ps://evil.com/x"}},
        {"ui": {"type": "markdown", "props": {"content": "see [here](https://evil.com)"}}},
        {"ui": {"type": "image", "props": {"src": "mailto:x@y.test"}}},
    ],
)
def test_r1_urls_are_checked_under_any_key(spec):
    assert _ripple(spec) is None


def test_r1_mail_and_phone_links_only_under_link_keys():
    assert _ripple({"ui": {"type": "cta", "props": {"label": "Mail", "href": "mailto:a@b.test"}}})
    assert _ripple({"ui": {"type": "cta", "props": {"label": "Call", "href": "tel:+15550100"}}})
    assert _ripple({"ui": {"type": "navbar", "props": {"ctaHref": "tel:+15550100"}}})
    assert _ripple({"ui": {"type": "text", "props": {"text": "Call tel:5550100, 10/12"}}})
    assert _ripple({"ui": {"type": "navbar", "props": {"ctaHref": "/contact"}}})


# R5: an expression can build a URL at render time. A URL-ish key refuses
# expressions outright; anywhere else, string literals concatenated inside an
# expression may not spell a script or data link.
@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "cta", "props": {"label": "x", "href": "{'java'+'script:alert(1)'}"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "href": "{state.a}:alert(1)"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "href": "javascript{state.c}"}}},
        {"ui": {"type": "navbar", "props": {"ctaHref": "{state.a}:alert(1)"}}},
        {
            "ui": {
                "type": "model-viewer",
                "props": {"src": "/m.glb", "environmentImage": "{state.e}"},
            }
        },
        {"ui": {"type": "entity-detail", "props": {"title": "x", "icon": "{state.i}"}}},
        {"ui": {"type": "text", "props": {"text": "{'java' + 'script:' + 'alert(1)'}"}}},
        {"ui": {"type": "text"}, "state": {"go": '{"data"+":text/html,x"}'}},
    ],
)
def test_r5_expressions_cannot_build_a_url(spec):
    assert _ripple(spec) is None


def test_r5_concatenated_literals_cannot_spell_an_off_site_link():
    assert _ripple({"ui": {"type": "text"}, "state": {"go": "{'ht'+'tps:/'+'/evil.com'}"}}) is None


def test_r4_a_recorded_scenario_with_flow_steps_passes():
    # ripple origin/main 09a56a6, live/fixtures/bill-splitter.json: two flows of set steps.
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    path = Path(__file__).parents[1] / "fixtures" / "ripple_bill_splitter_card.json"
    card = json.loads(path.read_text(encoding="utf-8"))
    assert '"action": "flow"' in json.dumps(card)
    assert render_card(_body(card), [], profile=RIPPLE_PROFILE) is not None


def test_r5_expressions_stay_usable_where_no_url_is_read():
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "text", "props": {"text": "Each pays {state.total / state.people}"}},
                {
                    "type": "button",
                    "props": {"label": "{state.n} left"},
                    "on_click": {"action": "set", "target": "rows.{index}.done", "value": "{item}"},
                },
            ],
        },
        "state": {"total": 10, "people": 2, "n": 1},
    }
    assert _ripple(spec) is not None


# R2: CSS in a style object (node or props) may not load anything.
@pytest.mark.parametrize(
    "style",
    [
        {"background-image": "url(https://evil.com/b.png)"},
        {"backgroundImage": "URL( '/x.png' )"},
        {"mask-image": "image-set('/a.png' 1x)"},
        {"list-style-image": "url(x)"},
        {"cursor": "url(/c.cur), auto"},
        {"content": "url(/x)"},
        {"border-image": "url(/x) 30"},
        {"background": "\\75 rl(/x)"},
        {"background": "u\\rl(/x)"},
        {"width": "expression(alert(1))"},
        {"font-family": "x;@import 'https://evil.com/a.css'"},
        {"background": "//evil.com/x.png"},
        {"background": "javascript:alert(1)"},
    ],
)
@pytest.mark.parametrize("where", ["node", "props"])
def test_r2_style_may_not_load_anything(style, where):
    node = {"type": "text", "props": {"text": "x"}}
    if where == "node":
        node["style"] = style
    else:
        node["props"]["style"] = style
    assert _ripple({"ui": node}) is None


def test_r2_a_style_string_is_checked_and_plain_styles_pass():
    bad = {"type": "text", "props": {"text": "x", "style": "color: red; background: url(/x)"}}
    assert _ripple({"ui": bad}) is None
    ok = {
        "type": "text",
        "props": {"text": "x", "style": "color: red; padding: 4px"},
        "style": {"color": "#333", "background": "linear-gradient(90deg, #fff, #eee)"},
    }
    assert _ripple({"ui": ok}) is not None


# R3: reference-style markdown links and autolinks get the same scheme check.
@pytest.mark.parametrize(
    "content",
    [
        "[x][1]\n\n[1]: data:text/html,<script>alert(1)</script>",
        "[x][1]\n\n[1]:   file:///etc/passwd",
        "<javascript:alert(1)>",
        "[x][a]\n\n[a]: blob:https://x/y",
    ],
)
def test_r3_reference_links_and_autolinks_are_checked(content):
    assert _ripple({"ui": {"type": "markdown", "props": {"content": content}}}) is None


# R4: flow, branch, validate and toast are allowed; every step inside them is
# held to the same set.
def _button(handler) -> dict:
    return {"ui": {"type": "button", "props": {"label": "Go"}, "on_click": handler}}


def test_r4_a_flow_of_allowed_steps_passes():
    flow = {
        "action": "flow",
        "steps": [
            {"action": "validate", "condition": "state.n > 0", "message": "Pick one"},
            {"action": "set", "target": "n", "value": 0},
            {"action": "toast", "message": "Done", "variant": "success"},
        ],
        "on_error": [{"action": "toast", "message": "Nope"}],
    }
    assert _ripple(_button(flow)) is not None
    branch = {
        "action": "branch",
        "if": "state.n > 5",
        "then": [{"action": "set", "target": "big", "value": True}],
        "else": [{"action": "flow", "steps": [{"action": "toggle", "target": "small"}]}],
    }
    assert _ripple(_button([branch])) is not None


@pytest.mark.parametrize("bad", ["api", "navigate", "invoke", "confirm", "delay", "made_up"])
def test_r4_a_flow_step_outside_the_set_is_refused(bad):
    step = {"action": bad, "url": "/x", "target": "t", "message": "m", "ms": 10}
    assert _ripple(_button({"action": "flow", "steps": [step]})) is None
    assert _ripple(_button({"action": "flow", "steps": [], "on_error": [step]})) is None


def test_r4_a_nested_branch_with_a_bad_step_is_refused():
    inner = {
        "action": "flow",
        "steps": [{"action": "set", "target": "a"}, {"action": "api", "url": "/x"}],
    }
    for key in ("then", "else"):
        branch = {"action": "branch", "if": "state.x", key: [inner]}
        assert _ripple(_button({"action": "flow", "steps": [branch]})) is None
    emit = {"action": "branch", "if": "1", "then": [{"action": "emit", "target": "steal"}]}
    assert _ripple(_button(emit)) is None


def test_r4_a_toast_message_gets_the_text_checks():
    toast = {"action": "toast", "message": "[go](javascript:alert(1))"}
    assert _ripple(_button(toast)) is None


# --------------------------------------------------------------------------- #
# The runtime
# --------------------------------------------------------------------------- #


def _widget(actions=()):
    return SimpleNamespace(id="w1", spec=SimpleNamespace(actions=list(actions)))


def test_the_profile_comes_from_the_site():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import ui_profile

    assert ui_profile(SimpleNamespace(concierge_ui_profile="ripple")) is RIPPLE_PROFILE
    for site in (SimpleNamespace(), SimpleNamespace(concierge_ui_profile="RIPPLE"), None):
        assert ui_profile(site) is PAWBAR_PROFILE


def test_the_ripple_cards_paragraph_needs_no_catalog_or_lead_capture():
    from pocketpaw_ee.paw_bar.concierge_runtime import build_prompt

    site = SimpleNamespace(concierge_ui_profile="ripple", concierge_lead_capture=False)
    prompt = build_prompt([], _widget(), [], "split a bill", site=site)
    assert "<catalog>" in prompt
    assert "- slider {" in prompt
    assert "no exponent operator" in prompt
    assert "flow (steps run in order)" in prompt
    assert "There is no api, navigate" in prompt
    assert "product-card" not in prompt
    assert "Do not offer a send_to_team form" in prompt

    pawbar = SimpleNamespace(concierge_lead_capture=False)
    assert "<catalog>" not in build_prompt([], _widget(), [], "hi", site=pawbar)


def test_ripple_raises_the_reply_cap_and_pawbar_keeps_the_setting():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import _model_settings

    settings = SimpleNamespace(pawbar_concierge_max_tokens=1_234)
    ripple = _model_settings(settings, None, "ws", profile=RIPPLE_PROFILE)
    assert ripple["max_tokens"] == 8_000
    assert _model_settings(settings, None, "ws", profile=PAWBAR_PROFILE)["max_tokens"] == 1_234
    assert _model_settings(settings, None, "ws")["max_tokens"] == 1_234


def test_the_fence_filter_checks_cards_against_the_profile():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import FenceFilter

    reply = f"Here you go.\n```pawbar-card\n{_body(_card())}\n```"
    ripple = FenceFilter(profile=RIPPLE_PROFILE)
    out = "".join(ripple.feed(reply) + ripple.close())
    assert '"type":"stat"' in out
    pawbar = FenceFilter()
    out = "".join(pawbar.feed(reply) + pawbar.close())
    assert "pawbar-card" not in out and out.startswith("Here you go.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("own", "global_cap", "spent", "over"),
    [
        (None, 5.0, 4.0, False),  # no site cap: the global one
        (None, 5.0, 6.0, True),
        (50.0, 5.0, 6.0, False),  # the site's cap replaces the global one
        (50.0, 5.0, 50.0, True),
        (2.0, 5.0, 3.0, True),
        (0.0, 5.0, 0.0, True),  # 0 pauses the site
        (10.0, 0.0, 11.0, True),  # a site cap holds with no global cap
    ],
)
async def test_the_sites_own_spend_cap_wins_over_the_global_one(
    monkeypatch, own, global_cap, spent, over
):
    from pocketpaw_ee.paw_bar import concierge_runtime

    async def _spent(workspace_id, pocket_id, **_):
        return spent

    monkeypatch.setattr(concierge_runtime, "site_spend_today_usd", _spent)
    settings = SimpleNamespace(pawbar_concierge_daily_spend_cap=global_cap)
    site = SimpleNamespace(concierge_daily_spend_cap=own)
    assert await concierge_runtime._over_spend_cap(settings, "ws", "p", site) is over


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_settings_default_to_pawbar_and_the_global_cap(client):
    c, _store = client
    site = await _settings_site()

    body = (await c.get(f"/paw-bar/admin/site/{site.id}/settings")).json()

    assert body["concierge_ui_profile"] == "pawbar"
    assert body["concierge_daily_spend_cap"] is None


@pytest.mark.asyncio
async def test_settings_patch_round_trips_the_profile_and_the_cap(client):
    from pocketpaw_ee.cloud.models.site import Site

    c, _store = client
    site = await _settings_site()
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(
        url, json={"concierge_ui_profile": "ripple", "concierge_daily_spend_cap": 25}
    )
    assert res.status_code == 200, res.text
    assert res.json()["concierge_ui_profile"] == "ripple"
    assert res.json()["concierge_daily_spend_cap"] == 25
    stored = await Site.get(site.id)
    assert (stored.concierge_ui_profile, stored.concierge_daily_spend_cap) == ("ripple", 25)

    # Another field's PATCH leaves both alone; null clears the cap only.
    await c.patch(url, json={"concierge_greeting": "Hi"})
    body = (await c.get(url)).json()
    assert (body["concierge_ui_profile"], body["concierge_daily_spend_cap"]) == ("ripple", 25)
    res = await c.patch(url, json={"concierge_daily_spend_cap": None, "concierge_ui_profile": None})
    assert res.json()["concierge_daily_spend_cap"] is None
    assert res.json()["concierge_ui_profile"] == "ripple"
    res = await c.patch(url, json={"concierge_daily_spend_cap": 100})
    assert res.status_code == 200, res.text
    res = await c.patch(
        url, json={"concierge_ui_profile": "pawbar", "concierge_daily_spend_cap": 0}
    )
    assert (res.json()["concierge_ui_profile"], res.json()["concierge_daily_spend_cap"]) == (
        "pawbar",
        0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"concierge_ui_profile": "react"},
        {"concierge_ui_profile": ""},
        {"concierge_daily_spend_cap": -1},
        {"concierge_daily_spend_cap": 100.01},
        {"concierge_daily_spend_cap": "lots"},
    ],
)
async def test_settings_patch_rejects_a_bad_profile_or_cap(client, patch):
    c, _store = client
    site = await _settings_site()
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(url, json=patch)

    assert res.status_code == 422
    body = (await c.get(url)).json()
    assert (body["concierge_ui_profile"], body["concierge_daily_spend_cap"]) == ("pawbar", None)
