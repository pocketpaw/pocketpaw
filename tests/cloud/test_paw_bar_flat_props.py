# tests/cloud/test_paw_bar_flat_props.py: a ripple node's props written flat on the node.
#
# Haiku sometimes writes a node's props beside its ``type`` instead of under
# ``props`` (``FLAT_BILL`` is a live one). On the strict profile ``_lift_flat_props``
# moves each top-level key the vendored manifest declares as that widget's prop into
# ``props`` (never a structural or handler key, never an undeclared one, never over a
# key ``props`` already holds), at any depth and in every flow step, and the full
# checks then run on the lifted card, which is the one sent (``card.final``).
# Mutation plan: tests/mutations/concierge_flat_props.json.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cloud.test_paw_bar_card_streaming import _events, _fence
from tests.cloud.test_paw_bar_illustration import _HOSTILE, GOOD_SVG

FLAT_BILL = (
    '{"ui":{"type":"bill-split","title":"Dinner for 4","subtotal":186.40,"currency":"USD",'
    '"tip_percent":18,"tip_options":[15,18,20],"extras_label":"Drinks","people":['
    '{"id":"p1","name":"Guest 1","extras":0},{"id":"p2","name":"Guest 2","extras":12},'
    '{"id":"p3","name":"Guest 3","extras":0},{"id":"p4","name":"Guest 4","extras":8}],'
    '"note":"Drinks are split only between the people who had them."},"state":{}}'
)
_DATA_CARDS = Path(__file__).parents[1] / "fixtures" / "ripple_data_widget_cards.json"
_BILL_PROPS = ("title", "subtotal", "currency", "tip_percent", "tip_options", "extras_label")


def _render(spec: dict | str) -> dict | None:
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    body = spec if isinstance(spec, str) else json.dumps(spec)
    out = render_card(body, [], profile=RIPPLE_PROFILE)
    if out is None:
        return None
    return json.loads(out.removeprefix("```pawbar-card\n").removesuffix("\n```"))


def _flat_bill(**extra) -> dict:
    return {**json.loads(FLAT_BILL)["ui"], **extra}


def _people(card: dict) -> list:
    return card["ui"]["props"]["people"]


def test_the_live_flat_bill_split_is_accepted_and_sent_nested(caplog):
    with caplog.at_level("INFO", logger="pocketpaw_ee.paw_bar.card_spec"):
        card = _render(FLAT_BILL)
    assert card is not None
    ui = card["ui"]
    assert set(ui) == {"type", "props"}
    assert ui["props"]["subtotal"] == 186.40
    assert {*_BILL_PROPS, "people", "note"} == set(ui["props"])
    assert len(_people(card)) == 4
    assert "card_spec: lifted 8 flat prop(s) into props" in caplog.text


def test_the_flat_bill_split_reaches_card_final_nested():
    events = _events([_fence(FLAT_BILL + "\n")])
    final = [data for name, data in events if name == "card.final"]
    assert final, events
    assert final[0]["card"]["ui"]["props"]["subtotal"] == 186.40
    assert "subtotal" not in final[0]["card"]["ui"]


def test_a_flat_card_streams_with_no_false_flag():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, PartialScan

    assert not PartialScan(RIPPLE_PROFILE).feed(FLAT_BILL)


def test_a_flat_comparison_layout_is_lifted_and_keeps_its_bind():
    cards = json.loads(_DATA_CARDS.read_text(encoding="utf-8"))
    nested = cards["comparison-layout"]
    ui = nested["ui"]
    flat = {**nested, "ui": {k: v for k, v in ui.items() if k != "props"} | ui["props"]}
    card = _render(flat)
    assert card is not None
    assert card["ui"]["bind"] == "pick"
    assert card["ui"]["props"] == _render(nested)["ui"]["props"]
    assert set(card["ui"]) == {"type", "bind", "props"}


def test_a_node_level_handler_stays_put_and_is_still_checked():
    ok = {"action": "set", "target": "done", "value": True}
    button = {"type": "button", "label": "Done", "on_click": ok}
    card = _render({"ui": button, "state": {"done": False}})
    assert card is not None
    assert card["ui"] == {"type": "button", "on_click": ok, "props": {"label": "Done"}}
    bad = {**button, "on_click": {"action": "invoke_tool", "tool": "x"}}
    assert _render({"ui": bad, "state": {}}) is None
    # bill-split refuses a handler: still at node level after the lift, still seen.
    assert _render({"ui": _flat_bill(on_click=ok), "state": {}}) is None


@pytest.mark.parametrize("key", ["bind", "show", "id", "actions", "on_change", "each"])
def test_structural_and_handler_keys_are_never_lifted(key):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, _lift_flat_props

    # input declares bind and value as props; neither may move.
    node = {"type": "input", "label": "Email", "value": "a@b.co", key: "x"}
    out, moved = _lift_flat_props(node, RIPPLE_PROFILE)
    assert out[key] == "x" and key not in out["props"]
    assert out["value"] == "a@b.co" and "value" not in out["props"]
    assert out["props"] == {"label": "Email"} and moved == 1


@pytest.mark.parametrize(
    ("kind", "prop", "key"),
    [
        ("entity-detail", "title", "actions"),
        ("wizard-layout", "steps", "nextActions"),
        ("ripple-frame", "spec", "state"),
    ],
)
def test_a_declared_handler_or_state_prop_stays_on_the_node(kind, prop, key):
    from pocketpaw_ee.paw_bar.card_spec import _LIFTABLE, RIPPLE_PROFILE, _lift_flat_props

    assert prop in _LIFTABLE[kind]
    node = {"type": kind, prop: "T", key: [{"action": "set", "target": "a", "value": 1}]}
    out, _ = _lift_flat_props(dict(node), RIPPLE_PROFILE)
    assert out[key] == node[key] and out["props"] == {prop: "T"}


def test_a_flat_composite_handler_is_still_checked_where_it_sits():
    bad = [{"label": "Go", "actions": {"action": "invoke_tool", "tool": "x"}}]
    ui = {"type": "entity-detail", "title": "Aero 14", "actions": bad}
    assert _render({"ui": ui, "state": {}}) is None
    ok = [{"label": "Go", "actions": {"action": "set", "target": "a", "value": 1}}]
    card = _render({"ui": {**ui, "actions": ok}, "state": {"a": 0}})
    assert card is not None and card["ui"]["actions"] == ok


def test_value_lifts_where_it_is_a_display_prop():
    card = _render({"ui": {"type": "stat", "label": "Guests", "value": 4}, "state": {}})
    assert card is not None
    assert card["ui"] == {"type": "stat", "props": {"label": "Guests", "value": 4}}


def test_an_unknown_key_is_not_lifted():
    card = _render({"ui": _flat_bill(mood="festive"), "state": {}})
    assert card is not None
    assert card["ui"]["mood"] == "festive"
    assert "mood" not in card["ui"]["props"]


def test_an_existing_props_key_wins_over_a_flat_duplicate():
    ui = _flat_bill(props={"subtotal": 99.5})
    card = _render({"ui": ui, "state": {}})
    assert card is not None
    assert card["ui"]["props"]["subtotal"] == 99.5
    assert card["ui"]["props"]["title"] == "Dinner for 4"
    assert card["ui"]["subtotal"] == 186.40  # the shadowed flat key stays where it was
    # A bad nested value still fails though a good flat one sits beside it.
    assert _render({"ui": _flat_bill(props={"subtotal": "lots"}), "state": {}}) is None


def test_a_hostile_flat_illustration_svg_is_still_refused():
    good = {"type": "illustration", "svg": GOOD_SVG, "title": "A sun"}
    card = _render({"ui": good, "state": {}})
    assert card is not None and card["ui"]["props"]["svg"] == GOOD_SVG
    for svg, _ in _HOSTILE:
        assert _render({"ui": {**good, "svg": svg}, "state": {}}) is None, svg


def test_nested_children_and_flow_steps_are_lifted():
    ui = {
        "type": "flex",
        "children": [
            {"type": "text", "text": "Your split"},
            {"type": "card", "children": [_flat_bill()]},
        ],
    }
    card = _render({"ui": ui, "state": {}})
    assert card is not None
    text, inner = card["ui"]["children"]
    assert text == {"type": "text", "props": {"text": "Your split"}}
    assert inner["children"][0]["props"]["subtotal"] == 186.40
    flow = {
        "flowId": "split",
        "title": "Split it",
        "ui": {"type": "text", "text": "Ready?"},
        "chain": {"flowId": "bill", "title": "The bill", "ui": _flat_bill()},
    }
    card = _render({"ui": flow, "state": {}})
    assert card is not None
    assert card["ui"]["title"] == "Split it"  # a flow step is not a widget
    assert card["ui"]["ui"]["props"] == {"text": "Ready?"}
    assert card["ui"]["chain"]["ui"]["props"]["subtotal"] == 186.40


def test_the_pawbar_profile_does_not_lift():
    from pocketpaw_ee.paw_bar.card_spec import render_card

    flat = {"ui": {"type": "text", "text": "hi"}}
    out = render_card(json.dumps(flat), [])
    assert out is not None and '"props"' not in out


def _known_good() -> list[tuple[str, dict]]:
    from pocketpaw_ee.paw_bar import concierge_runtime as rt

    cards = json.loads(_DATA_CARDS.read_text(encoding="utf-8"))
    explainer = Path(__file__).parents[1] / "fixtures" / "ripple_explainer_card.json"
    return [
        *cards.items(),
        ("explainer", json.loads(explainer.read_text(encoding="utf-8"))),
        ("example", rt._RIPPLE_EXAMPLE),
        ("flow-example", rt._RIPPLE_FLOW_EXAMPLE),
    ]


@pytest.mark.parametrize(("name", "spec"), _known_good(), ids=[n for n, _ in _known_good()])
def test_a_nested_card_is_left_exactly_as_it_was(name, spec):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, _lift_flat_props

    # A node's props object (an input's {"type", "label", "size"}) is never read as a node.
    ui = json.loads(json.dumps(spec["ui"]))
    out, moved = _lift_flat_props(ui, RIPPLE_PROFILE)
    assert (moved, out) == (0, spec["ui"]), name


def test_props_that_look_like_a_flat_widget_are_not_lifted():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, _lift_flat_props

    node = {"type": "input", "bind": "email", "props": {"type": "text", "size": "sm"}}
    out, moved = _lift_flat_props(json.loads(json.dumps(node)), RIPPLE_PROFILE)
    assert (moved, out) == (0, node)
