# tests/cloud/test_paw_bar_ripple_profile.py — the "ripple" concierge card profile.
#
# A site whose ``concierge_ui_profile`` is "ripple" gets full-catalog Ripple cards
# (``card_spec.RIPPLE_PROFILE``: the vendored ripple-manifest.json, 400 nodes,
# depth 16, 64,000 chars, handlers checked wherever a widget keeps them), the
# Ripple cards paragraph with no catalog, actions or lead capture, an 8,000
# token reply cap and ``FRAME_DEMO`` (rules 3 to 5 of FRAME kept verbatim)
# whatever its switches. Every other site keeps ``PAWBAR_PROFILE``, and its cards
# paragraph and all 8 of its frames are pinned byte for byte. The Ripple
# paragraph lists the data widgets (entity-detail, timeline, kv-table, ...) with
# their props, is sized for the
# landing's ~720px chat column and ends with ``_RIPPLE_EXAMPLE``, which must pass
# the ripple checks whole; its flow rule and ``ask`` line carry
# ``_RIPPLE_FLOW_EXAMPLE`` and ``_RIPPLE_ASK``, which must pass too. Also covered:
# the per-site daily spend cap over the global one, and both settings through the
# settings PATCH and its response.
# The accepted card is ripple's recorded explainer scenario
# (tests/fixtures/ripple_explainer_card.json, from ripple origin/main 09a56a6,
# packages/svelte/src/routes/live/fixtures/explainer.json, its chunks joined and
# wrapped as {ui, state}). Flow cards (``TRIP_FLOW`` is the accepted shape) and
# the ``ask`` host event pass only on ripple; ``_FLOW_REFUSALS`` lists what each
# refuses and is reused streamed. Ripple's data widgets (``RIPPLE_DATA_WIDGETS``,
# ripple-iui#182) are listed with typed props and the rules map answers to them;
# each passes with its manifest example (tests/fixtures/ripple_data_widget_cards.json).
# Page chrome (``RIPPLE_CHROME``) is refused and unlisted. A spec body missing only
# closing brackets is repaired (a live one: tests/fixtures/ripple_itinerary_missing_brace.json),
# never past 4, never for any other break, and is then refused for anything else;
# one with a single surplus } before its state (tests/fixtures/ripple_flow_surplus_brace.json)
# loses that one, never two.
# Odd JSON shapes in handler slots and row lists (``_odd_specs``) are judged by
# the rules, never by an exception; an unforeseen one is a logged refusal.
# bill-split's props are held to plain finite numbers, 2 to 12 people and text
# (``_check_bill_split``); a button's choice-card icon is a key and its description
# short plain text (``_check_choice``). Mutation plans: tests/mutations/concierge_ripple_rules.json,
# tests/mutations/concierge_bill_split.json, tests/mutations/concierge_choice_buttons.json.

# ruff: noqa: F811 — pytest fixtures imported by name

from __future__ import annotations

import hashlib
import itertools
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
from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    concierge_client,
    model,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "ripple_explainer_card.json"

# Refreshing ripple-manifest.json: see ee/pocketpaw_ee/paw_bar/ripple-manifest.json.source
# (vendored from qbtrix/ripple-iui#185 pending a release; drop the widget examples,
# update the pins below).
_RIPPLE_MANIFEST_SHA256 = "470f1535ccd266ee8ea8c27b2209e44a90dff4993b3970f47b8624fe0ae693af"


def _pin_ops(monkeypatch, *site_ids: str, cap: float | None = None) -> None:
    """Put ``site_ids`` on ``pawbar_ops_site_ids`` (and pin the global cap)."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    update: dict = {"pawbar_ops_site_ids": ",".join(site_ids)}
    if cap is not None:
        update["pawbar_concierge_daily_spend_cap"] = cap
    pinned = concierge_runtime._settings().model_copy(update=update)
    monkeypatch.setattr(concierge_runtime, "_settings", lambda: pinned)


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
    assert len(card_spec.RIPPLE_MANIFEST["widgets"]) == 199
    # Every trimmed or typed name is a real widget (a typo would trim nothing).
    every = {w["type"] for w in card_spec.RIPPLE_MANIFEST["widgets"]}
    chrome, deferred = card_spec.RIPPLE_CHROME, card_spec.RIPPLE_DEFERRED
    assert chrome <= every and chrome.isdisjoint(deferred)
    r = card_spec.RIPPLE_PROFILE
    assert r.typed.keys() <= r.detailed <= r.widget_types
    assert {"illustration", "bill-split"} <= every & r.widget_types
    assert len(r.widget_types) == 199 - len(deferred) - len(chrome)


def test_the_pawbar_profile_is_todays_rules():
    from pocketpaw_ee.paw_bar import card_spec

    RIPPLE_STRICT = card_spec.RIPPLE_PROFILE.strict
    p = card_spec.PAWBAR_PROFILE
    assert (p.widget_types, p.actions) == (card_spec.WIDGET_TYPES, card_spec.SPEC_ACTIONS)
    assert (p.max_nodes, p.max_depth, p.max_chars) == (80, 8, 32_000)
    assert p.detailed is None and p.typed == {} and p.strict is False and RIPPLE_STRICT

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


# The primitives' data widgets the authoring rules point at, listed with props; the
# example card shows their item shapes. A composite whose shapes nothing shows stays
# brief (Ripple's data widgets, RIPPLE_DATA_WIDGETS, are listed with typed props).
_DATA_WIDGETS = ("entity-detail", "timeline", "kv-table", "alert", "callout")
_BRIEF_COMPOSITES = ("analytics-dashboard",)


def _ripple_paragraph() -> str:
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import _cards_paragraph

    return _cards_paragraph([], profile=RIPPLE_PROFILE)


def _nodes(node: dict) -> list[dict]:
    return [node, *(n for kid in node.get("children", []) for n in _nodes(kid))]


def test_the_ripple_paragraph_details_the_data_widgets_at_the_landings_width():
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_EXAMPLE, _RIPPLE_RULES

    text = _ripple_paragraph()
    for widget in _DATA_WIDGETS:
        assert f"   - {widget} {{" in text, widget
    for widget in _BRIEF_COMPOSITES:
        assert f"   - {widget}: " in text and f"   - {widget} {{" not in text, widget
    assert 'call that field "kind"' in text
    entity = next(line for line in text.splitlines() if line.startswith("   - entity-detail {"))
    assert "kpis?" in entity and "meta?" in entity and "status?" in entity
    assert "300px" not in text
    assert "720px" in text and "360px" in text
    assert '"columns": "repeat(auto-fit, minmax(150px, 1fr))"' in text
    assert "Aim for about 25 to 90 nodes" in text
    assert "Match the card to the answer" in text
    assert "starts with entity-detail (title, status, kpis, meta)" in text
    # The example is the last rule, as compact JSON.
    example = json.dumps(_RIPPLE_EXAMPLE, separators=(",", ":"))
    assert _RIPPLE_RULES[-1].endswith(example) and example in text
    # The whole paragraph stays bounded: 19,814 chars before the primitives' data
    # widgets, 24,327 before Ripple's data widgets (typed lines and their rules, less
    # the page chrome), 27,849 after, 30,473 with the flow and ask rules (the flow
    # example alone is 890), 31,191 with the illustration line and rule, 31,972 with
    # the vendored illustration line, bill-split and the choice-button line.
    assert len(text) < 32_000


# Ripple's data widgets (qbtrix/ripple-iui#182): one card per widget, its manifest
# example at a9ab3b37 (bill-split: ffed544) as ui with its bound state path seeded,
# except bill-split's, which the widget holds as an object.
_DATA_CARDS: dict = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "ripple_data_widget_cards.json").read_text(
        encoding="utf-8"
    )
)
_CHROME = (
    "navbar",
    "footer",
    "hero",
    "marketing-hero",
    "newsletter",
    "logo-cloud",
    "testimonial",
    "app-shell",
    "sidebar",
    "breadcrumb",
    "sheet",
    "parallax",
    "reveal",
    "command-palette",
    "coachmark",
    "notification-center",
)


def _listing() -> dict[str, str]:
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, compact_manifest

    lines = compact_manifest(RIPPLE_PROFILE).splitlines()
    return {line[2:].split(" ", 1)[0].rstrip(":"): line for line in lines}


def _fields(line: str) -> str:
    """A listing line's field list, without its description."""
    return line[: line.index("}: ")]


@pytest.mark.parametrize("widget", sorted(_DATA_CARDS))
def test_each_data_widget_card_passes_ripple_and_not_pawbar(widget):
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE, render_card

    card = _DATA_CARDS[widget]
    assert card["ui"]["type"] == widget
    body = json.dumps(card)
    out = render_card(body, [], profile=RIPPLE_PROFILE)
    assert out is not None
    assert json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0]) == card
    assert render_card(body, [], profile=PAWBAR_PROFILE) is None


@pytest.mark.parametrize("widget", sorted(_DATA_CARDS))
def test_each_data_widget_card_streams_with_no_false_flag(widget):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, PartialScan

    scan = PartialScan(RIPPLE_PROFILE)
    assert not any(scan.feed(ch) for ch in json.dumps(_DATA_CARDS[widget]))


def test_the_data_widgets_are_the_typed_ones_and_each_has_a_card():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_DATA_WIDGETS, RIPPLE_PROFILE

    assert set(_DATA_CARDS) == set(RIPPLE_DATA_WIDGETS) == set(RIPPLE_PROFILE.typed)


def test_only_the_data_widgets_list_typed_props():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_DATA_WIDGETS, RIPPLE_PROFILE

    lines = _listing()
    assert lines["itinerary"].startswith(
        "- itinerary {budget?: number, route?: string[], days: [{id?,label,when?,"
    )
    assert _fields(lines["growth-projection"]).startswith(
        "- growth-projection {initial?: number, deposit: number, rate: number, years: number,"
    )
    assert (
        "comparison-layout {items: [{id,name,subtitle?,price?:number," in lines["comparison-layout"]
    )
    assert "winner?: {id,reason,runner_up?:{id,reason}}" in lines["comparison-layout"]
    # booking and menu-order list only what the model writes; exec-dashboard its rows.
    assert _fields(lines["booking"]) == "- booking {party?: number, preferred?: {date?,after?}"
    menu = _fields(lines["menu-order"])
    assert menu.startswith("- menu-order {items: [{id?,product_id?,name?,")
    assert menu.endswith("featured?: {id,reason?}, preset?: [{id,qty:number}]")
    dash = _fields(lines["exec-dashboard"])
    assert dash.startswith("- exec-dashboard {rows?: [Record<string,string|number>], measures?:")
    assert "kpis" not in dash and "primaryChart" not in dash and "on_filter" not in dash
    # The props every data widget shares are taught once, by the rules.
    for widget in RIPPLE_DATA_WIDGETS:
        assert "verdict" not in _fields(lines[widget]) and "currency" not in _fields(
            lines[widget]
        ), widget
    # Every other detailed widget still lists names only.
    for widget in RIPPLE_PROFILE.detailed - RIPPLE_DATA_WIDGETS.keys():
        assert ": " not in _fields(lines[widget]), widget


def test_a_typed_line_caps_each_type():
    from pocketpaw_ee.paw_bar.card_spec import _TYPE_CHARS, RIPPLE_MANIFEST, _short_type

    shorts = [
        _short_type(str(spec.get("type", "")))
        for w in RIPPLE_MANIFEST["widgets"]
        for spec in (w.get("props") or {}).values()
    ]
    assert max(map(len, shorts)) == _TYPE_CHARS == 90
    assert "stops:[{id?,time?,title,kind:sight|food|stay|transit|activ…" in _listing()["itinerary"]
    assert _short_type("string") == "" and _short_type('"a" | "b"') == "a|b"


@pytest.mark.parametrize("widget", _CHROME)
def test_page_chrome_is_refused_and_unlisted_on_ripple(widget):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_CHROME, RIPPLE_PROFILE

    assert RIPPLE_CHROME == set(_CHROME)
    assert widget not in RIPPLE_PROFILE.widget_types and widget not in _listing()
    text = {"type": "text", "props": {"text": "hi"}}
    assert _ripple({"ui": {"type": "flex", "children": [text]}}) is not None
    assert _ripple({"ui": {"type": "flex", "children": [text, {"type": widget}]}}) is None


def test_the_rules_point_each_answer_at_its_data_widget():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_DATA_WIDGETS
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_RULES

    rule = next(r for r in _RIPPLE_RULES if "Match the card to the answer" in r)
    for phrase in (
        "A trip is an itinerary",
        "a menu-order: items by product_id",
        "write only preferred {date, after} and party",
        "A meal plan is a meal-plan, one dish a recipe, a workout an interval-workout, "
        "study cards a flashcard-deck",
        "growth-projection (four numbers",
        "an exec-dashboard with the raw rows",
        "a comparison-layout with a winner",
        "verdict? {text, status?: good|warn|bad|info|neutral}",
        "Never write on_checkout or on_book",
        "binds primitives to state as above",
    ):
        assert phrase in rule, phrase
    assert all(widget in rule for widget in RIPPLE_DATA_WIDGETS)


def test_the_rules_name_only_real_badge_variants():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_MANIFEST

    badge = next(w for w in RIPPLE_MANIFEST["widgets"] if w["type"] == "badge")
    real = set(badge["props"]["variant"]["type"].replace('"', "").split(" | "))
    named = "success, warning, destructive, secondary, outline, default"
    assert f"badge variants ({named})" in _ripple_paragraph()
    assert set(named.split(", ")) == real


def test_the_example_card_passes_the_ripple_checks_whole():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE, render_card
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_EXAMPLE

    body = json.dumps(_RIPPLE_EXAMPLE)
    out = render_card(body, [], profile=RIPPLE_PROFILE)
    assert out is not None
    assert json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0]) == _RIPPLE_EXAMPLE
    nodes = _nodes(_RIPPLE_EXAMPLE["ui"])
    kinds = {n["type"] for n in nodes}
    assert {"entity-detail", "grid", "stat", "chart", "table", "timeline", "callout"} <= kinds
    root = _RIPPLE_EXAMPLE["ui"]["props"]
    assert root["kpis"] and root["meta"] and "actions" not in root
    # Each detailed data widget shows its item shape (its main array prop) once.
    shown = {(n["type"], k) for n in nodes for k in n.get("props", {})}
    assert {("timeline", "events"), ("kv-table", "rows"), ("table", "rows")} <= shown
    assert {("alert", "description"), ("callout", "text")} <= shown
    # No data row has a "type" naming a widget: the landing refuses those as
    # widget aliases and the server counts them as nodes.
    rows = [
        row
        for n in nodes
        for v in n.get("props", {}).values()
        if isinstance(v, list)
        for row in v
        if isinstance(row, dict)
    ]
    assert rows and not [r for r in rows if r.get("type") in RIPPLE_PROFILE.widget_types]
    assert 20 <= len(nodes) <= 40
    compact = json.dumps(_RIPPLE_EXAMPLE, separators=(",", ":"))
    assert compact.isascii() and len(compact) < 3_000
    # Not a pawbar card: its widgets are ripple's.
    assert render_card(body, [], profile=PAWBAR_PROFILE) is None


def test_the_flow_rule_teaches_the_steps_the_collected_answers_and_ask():
    from pocketpaw_ee.paw_bar.card_spec import ASK_MAX, MAX_FLOW_STEPS, RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import (
        _RIPPLE_ASK,
        _RIPPLE_FLOW_EXAMPLE,
        _RIPPLE_RULES,
    )

    text = _ripple_paragraph()
    rule = next(r for r in _RIPPLE_RULES if "Flow cards:" in r)
    for phrase in (
        "never for a one-shot answer",
        '"chain": <step 2>}',
        '"chain_map": {<option id>: <step>} branches on the pick',
        f"most {MAX_FLOW_STEPS} steps, sharing the {RIPPLE_PROFILE.max_nodes} nodes",
        "its own snake_case flowId and a clear title",
        "top-level state never reaches its steps",
        "a set into state is lost",
        'emits flow.next with value {"selection": {"id", "label"}}',
        '{"formData": {"days": "{state.days}"}}',
        "buttons emit flow.submit",
        '"onComplete": {"kind": "chat"',
        f"no {{expressions}}, at most {ASK_MAX} characters",
        "The browser appends each answer to it",
        "a comparison-layout with a winner",
    ):
        assert phrase in rule, phrase
    assert rule.endswith(json.dumps(_RIPPLE_FLOW_EXAMPLE, separators=(",", ":")))
    # The allowed-actions rule names these emits, so it never reads as dropping them.
    actions = next(r for r in _RIPPLE_RULES if "The only actions are" in r)
    assert "emit of add_to_cart, checkout, ask or, in a flow card, flow.next" in actions
    ask = json.dumps(_RIPPLE_ASK, separators=(",", ":"))
    assert f"{ask} (plain text" in text and "a comparison item's actions list" in text
    # Ripple only: the pawbar paragraph has no flow or ask.
    pawbar = _pawbar_paragraphs()
    assert "Flow cards:" not in pawbar and ask not in pawbar


def _flow_steps_of(step: dict) -> list[dict]:
    out = [step]
    for nxt in [step.get("chain"), *(step.get("chain_map") or {}).values()]:
        if nxt:
            out += _flow_steps_of(nxt)
    return out


def test_the_flow_example_passes_the_ripple_checks_and_collects_every_pick():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, render_card
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_FLOW_EXAMPLE

    out = _ripple(_RIPPLE_FLOW_EXAMPLE)
    assert out is not None
    assert json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0]) == _RIPPLE_FLOW_EXAMPLE
    assert render_card(_body(_RIPPLE_FLOW_EXAMPLE), [], profile=PAWBAR_PROFILE) is None
    steps = _flow_steps_of(_RIPPLE_FLOW_EXAMPLE["ui"])
    assert 2 <= len(steps) <= 3
    # Each step is named (the landing labels the answers by it) and each pick is
    # an emit the runner records: flow.next on the way, flow.submit on the last.
    assert len({s["flowId"] for s in steps}) == len(steps)
    for step in steps:
        assert step["title"] and step["flowId"].replace("_", "").isalpha()
        last = not (step.get("chain") or step.get("chain_map"))
        buttons = step["ui"]["children"]
        assert len(buttons) >= 2
        for button in buttons:
            click = button["on_click"]
            assert click["target"] == ("flow.submit" if last else "flow.next")
            assert click["value"]["selection"]["label"] == button["props"]["label"]
        assert ("onComplete" in step) == last
    done = steps[-1]["onComplete"]
    assert done["kind"] == "chat" and "{" not in done["message"]


def test_the_ask_the_rules_show_passes_on_a_click_and_a_comparison_item():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, render_card
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_ASK

    button = {"ui": {"type": "button", "props": {"label": "Tell me more"}, "on_click": _RIPPLE_ASK}}
    items = [{"id": i, "name": i.upper(), "actions": [_RIPPLE_ASK]} for i in ("a", "b")]
    compare = {
        "ui": {
            "type": "comparison-layout",
            "props": {"items": items, "features": [{"key": "price", "label": "Price"}]},
        }
    }
    for card in (button, compare):
        assert _ripple(card) is not None
    assert render_card(_body(button), [], profile=PAWBAR_PROFILE) is None


def _entity_actions(actions) -> dict:
    return {
        "ui": {
            "type": "entity-detail",
            "props": {"title": "X", "actions": [{"label": "Go", "actions": actions}]},
        },
        "state": {"a": 0},
    }


def _compared(image: str) -> dict:
    items = [{"id": "a", "name": "A", "image": image}, {"id": "b", "name": "B"}]
    return {"ui": {"type": "comparison-layout", "props": {"items": items}}}


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        (
            _entity_actions([{"action": "navigate", "target": "/x"}]),
            "navigate in actions[].actions",
        ),
        (
            _entity_actions({"action": "fetch", "target": "a"}),
            "an unknown action in actions[].actions",
        ),
        (_compared("https://cdn.example.com/a.png"), "a full https image on a compared item"),
    ],
)
def test_the_data_widgets_keep_actions_and_urls_in_check(spec, why):
    assert _ripple(spec) is None, why


@pytest.mark.parametrize(
    "spec",
    [
        _entity_actions([{"action": "set", "target": "a", "value": 1}]),
        _compared("/a.png"),
    ],
)
def test_the_data_widgets_pass_with_allowed_actions_and_paths(spec):
    assert _ripple(spec) is not None


def _labelled_actions(action) -> dict:
    """A comparison whose item buttons are written ``{label, action}``, the way a
    model writes a button list."""
    items = [{"id": "a", "name": "A", "actions": [{"label": "Pick", "action": action}]}]
    return {"ui": {"type": "comparison-layout", "props": {"items": [*items, {"id": "b"}]}}}


def test_a_labelled_comparison_action_is_judged_not_crashed(caplog):
    # The widget dispatches items[].actions as EventHandler objects, whose
    # ``action`` is a verb name: a label beside it is ignored, an object there is
    # nothing the engine runs.
    caplog.set_level("WARNING", logger="pocketpaw_ee.paw_bar.card_spec")
    assert _ripple(_labelled_actions("emit")) is None  # emit with no target
    assert _ripple(_labelled_actions({"action": "emit", "target": "ask"})) is None
    assert _ripple(_labelled_actions(["set"])) is None
    ok = _labelled_actions("set")
    ok["ui"]["props"]["items"][0]["actions"][0] |= {"target": "x", "value": 1}
    assert _ripple(ok) is not None
    assert "could not read" not in caplog.text


# Odd JSON (numbers, lists, objects where strings or objects are expected) in
# handler slots and row lists: each is judged by the rules, never by an exception.
_ODD = (7, 1.5, True, None, [], [[1]], {}, {"a": {"b": 1}}, "x")


def _odd_specs():
    for odd in _ODD:
        yield {"ui": {"type": "button", "props": {"label": "x"}, "on_click": odd}}
        yield {"ui": {"type": "button", "props": {"label": "x"}, "on_click": {"action": odd}}}
        yield {
            "ui": {
                "type": "button",
                "props": {"label": "x"},
                "on_click": {"action": "emit", "target": odd, "value": odd},
            }
        }
        yield {"ui": {"type": "comparison-layout", "props": {"items": odd}}}
        yield {"ui": {"type": "comparison-layout", "props": {"items": [odd, {"id": "b"}]}}}
        yield _labelled_actions(odd)
        yield {
            "ui": {"type": "comparison-layout", "props": {"items": [{"id": "a", "actions": odd}]}}
        }
        yield {
            "ui": {"type": "entity-detail", "props": {"actions": [{"label": odd, "actions": odd}]}}
        }
        yield {"ui": {"type": "flex", "children": [odd]}}
        yield {"ui": {"type": odd, "props": odd}}
        yield {"ui": {"type": "flex"}, "state": {"rows": [odd, {"action": odd}]}}


def test_odd_shapes_in_handler_slots_and_rows_never_raise(caplog):
    from pocketpaw_ee.paw_bar.card_spec import (
        HOST_EVENTS,
        RIPPLE_PROFILE,
        _card_verbs,
        _check_strict,
        _Reject,
    )

    caplog.set_level("WARNING", logger="pocketpaw_ee.paw_bar.card_spec")
    for spec in _odd_specs():
        try:
            _check_strict(spec, _card_verbs(HOST_EVENTS), False, RIPPLE_PROFILE)
        except _Reject:
            pass
    assert "could not read" not in caplog.text


def test_an_unexpected_error_in_the_strict_walk_is_a_logged_refusal(monkeypatch, caplog):
    from pocketpaw_ee.paw_bar import card_spec

    def boom(*_a, **_k):
        raise TypeError("unhashable")

    monkeypatch.setattr(card_spec, "_check_actions", boom)
    caplog.set_level("WARNING", logger="pocketpaw_ee.paw_bar.card_spec")
    spec = {"ui": {"type": "button", "props": {"label": "x"}, "on_click": {"action": "set"}}}
    with pytest.raises(card_spec._Reject):
        card_spec._check_strict(spec, [], False, card_spec.RIPPLE_PROFILE)
    assert "could not read (TypeError)" in caplog.text


# Pins today's pawbar cards paragraph (with the compact manifest) byte for byte:
# the ripple rules must never leak into it. If the pawbar paragraph changes on
# purpose, recompute with hashlib.sha256(_pawbar_paragraphs().encode()).
_PAWBAR_PARAGRAPH_SHA256 = "96bcf44088350ed721971e5bc0b29cd52b3524c12f0d7b04d9242d8d774422d4"
# Pins every pawbar site's frame the same way: ``frame_for`` over all 8 doc-code,
# lead-capture and page-action combinations (``_SWITCHES`` order), joined by
# "\n---\n". The demo frame must never reach a pawbar site.
_PAWBAR_FRAMES_SHA256 = "f0fdea6e8f1095293ba7f0affb189db416b98de3e5851767bad1f647d0756216"
_SWITCHES = [
    {"concierge_allow_doc_code": doc, "concierge_lead_capture": lead, "concierge_page_actions": act}
    for doc, lead, act in itertools.product((False, True), repeat=3)
]


def _pawbar_paragraphs() -> str:
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import _cards_paragraph

    declared = [
        {"verb": "book", "policy": "approve", "args": {"date": "string"}, "label": "Book"},
        {"verb": "add_to_cart", "policy": "auto", "args": {}, "label": "Add"},
    ]
    rich = _cards_paragraph(declared, has_catalog=True, lead_capture=True)
    assert rich == _cards_paragraph(
        declared, has_catalog=True, lead_capture=True, profile=PAWBAR_PROFILE
    )
    return rich + "\n---\n" + _cards_paragraph([])


def test_the_pawbar_cards_paragraph_is_unchanged(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_RULES, frame_for

    text = _pawbar_paragraphs()
    assert hashlib.sha256(text.encode()).hexdigest() == _PAWBAR_PARAGRAPH_SHA256
    assert not any(rule.strip() in text for rule in _RIPPLE_RULES)

    frames = [frame_for(SimpleNamespace(**switches)) for switches in _SWITCHES]
    assert hashlib.sha256("\n---\n".join(frames).encode()).hexdigest() == _PAWBAR_FRAMES_SHA256
    # An ops site on the pawbar profile keeps the same frames.
    _pin_ops(monkeypatch, "s1")
    for switches, frame in zip(_SWITCHES, frames, strict=True):
        assert frame_for(SimpleNamespace(id="s1", **switches)) == frame
        assert (
            frame_for(SimpleNamespace(id="s1", concierge_ui_profile="pawbar", **switches)) == frame
        )


def test_an_ops_site_on_the_ripple_profile_gets_the_demo_frame(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import FRAME_DEMO, frame_for

    _pin_ops(monkeypatch, "s1")
    for switches in _SWITCHES:
        site = SimpleNamespace(id="s1", concierge_ui_profile="ripple", **switches)
        assert frame_for(site) is FRAME_DEMO


def test_a_stored_ripple_profile_off_the_ops_list_gets_the_normal_frame(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import _FRAMES, FRAME_DEMO, frame_for

    _pin_ops(monkeypatch, "s1")
    for switches in _SWITCHES:
        site = SimpleNamespace(id="s2", concierge_ui_profile="ripple", **switches)
        assert frame_for(site) is _FRAMES[tuple(switches.values())]
        assert frame_for(site) != FRAME_DEMO


def test_the_demo_frame_swaps_the_opening_and_rules_1_2_6_and_keeps_3_to_5():
    from pocketpaw_ee.paw_bar.concierge_runtime import FRAME, FRAME_DEMO

    site, demo = FRAME.split("\n"), FRAME_DEMO.split("\n")
    assert len(site) == len(demo) == 8
    assert demo[1] == site[1] == "Rules:"
    assert demo[4:7] == site[4:7]  # rules 3, 4 and 5, word for word
    assert [demo[i] == site[i] for i in (0, 2, 3, 7)] == [False] * 4
    assert demo[0].startswith("You are the demo assistant on the Ripple website")
    assert demo[2].startswith("1. When the visitor asks for something a small interface can do")
    assert demo[3].startswith(
        "2. Never write code, scripts, markup, configuration or commands outside the card"
    )
    assert "Never give medical, legal or financial advice" in demo[3]
    assert demo[7] == (
        "6. Keep the text short: one or two sentences, then the card, in the visitor's language."
    )
    assert "Answer only about this site" not in FRAME_DEMO


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


# SEC F1: a handler slot holds action objects. A string there (or a prop that
# carries handlers, a node-valued prop or a node's props given as an expression)
# is what the engine resolves at run time, so a handler kept in state, or built
# there by ``set`` pieces, would be dispatched unchecked. The findings' repros
# lead the list. ``state`` holds no action at all.
_SMUGGLED = {
    "n": {
        "type": "audit-log",
        "props": {"entries": [{"action": "emit", "target": "ask", "value": {"text": "hi"}}]},
    }
}
_BUILT = [
    {"action": "set", "target": "h.action", "value": "api"},
    {"action": "set", "target": "h.url", "value": "/api/v1/anything"},
]


def _slot(key: str, value) -> dict:
    return {"ui": {"type": "flex", "props": {key: value}}}


@pytest.mark.parametrize(
    ("spec", "why"),
    [
        (
            {
                "ui": {
                    "type": "ask-user-questions",
                    "props": {
                        "questions": [
                            {
                                "id": "q1",
                                "title": "Anything else?",
                                "allowOther": True,
                                "options": [{"title": "No"}],
                            }
                        ],
                        "changeActions": "{state.n.props.entries.0}",
                    },
                },
                "state": _SMUGGLED,
            },
            "F1a: changeActions resolves to a state row",
        ),
        (
            {
                "ui": {
                    "type": "comparison-layout",
                    "props": {
                        "items": [{"id": "a", "name": "A", "actions": "{state.n.props.entries.0}"}]
                    },
                },
                "state": {
                    "n": {
                        "type": "audit-log",
                        "props": {"entries": [{"action": "api", "url": "/x", "method": "POST"}]},
                    }
                },
            },
            "F1b: items[].actions resolves to a state row",
        ),
        (
            {
                "ui": {
                    "type": "flex",
                    "children": [
                        {"type": "button", "props": {"label": "Go"}, "on_click": _BUILT},
                        {
                            "type": "comparison-layout",
                            "props": {"items": [{"id": "a", "name": "A", "actions": "{state.h}"}]},
                        },
                    ],
                }
            },
            "a handler built by set pieces, fired from items[].actions",
        ),
        (_slot("finishActions", "{state.h}"), "a *Actions expression"),
        (_slot("changeActions", ["{state.h}"]), "an expression in a list under a slot"),
        (_slot("toggleActions", {"target": "a"}), "an object that is not an action"),
        (_slot("learn_more", "{state.h}"), "learn_more"),
        (_slot("onRowClick", "{state.h}"), "onRowClick"),
        (_slot("actions", "{state.h}"), "a composite's button list"),
        (_slot("on_close", [{"label": "x"}]), "a row, not an action, under on_*"),
        (_entity_actions("{state.h}"), "a button row's actions as an expression"),
        (
            {"ui": {"type": "comparison-layout", "props": {"items": "{state.items}"}}},
            "rows that carry handlers, as an expression",
        ),
        (
            {"ui": {"type": "map", "props": {"markers": ["{state.m}"]}}},
            "a row that carries handlers, as an expression",
        ),
        (_popover("{state.n}"), "a node-valued prop as an expression"),
        ({"ui": {"type": "ask-user-questions", "props": "{state.p}"}}, "props as an expression"),
        ({"ui": {"type": "text"}, "state": _SMUGGLED}, "an audit-log row in state"),
        (
            {"ui": {"type": "text"}, "state": {"t": {"action": "toast", "message": "hi"}}},
            "an allowed action kept in state",
        ),
        (
            {
                "ui": {"type": "text"},
                "state": {"b": {"type": "button", "on_click": {"action": "set", "target": "a"}}},
            },
            "a node with a handler kept in state",
        ),
    ],
)
def test_sec_f1_a_handler_slot_holds_only_action_objects(spec, why):
    assert _ripple(spec) is None, why


@pytest.mark.parametrize(
    "spec",
    [
        {
            "ui": {
                "type": "comparison-layout",
                "props": {
                    "items": [
                        {
                            "id": "a",
                            "name": "A",
                            "actions": [{"action": "set", "target": "pick", "value": "a"}],
                            "learn_more": {"action": "toast", "message": "More soon"},
                        }
                    ]
                },
            }
        },
        _entity_actions([{"action": "set", "target": "a", "value": 1}]),
        {
            "ui": {
                "type": "table",
                "props": {
                    "rows": "{state.rows}",
                    "onRowClick": {"action": "set", "target": "row", "value": "{item}"},
                },
            },
            "state": {"rows": [{"name": "Tea", "on_hand": 4, "actions": "Edit"}]},
        },
        {
            "ui": {
                "type": "table",
                "props": {"rows": [{"name": "Tea", "on_hand": 4, "actions": "Edit"}]},
            }
        },
        {
            "ui": {"type": "audit-log", "props": {"entries": "{state.log}"}},
            "state": {"log": [{"action": "approved"}]},
        },
        _popover("Ready in {state.mins} minutes"),
        _popover({"type": "text", "props": {"text": "{state.mins}"}}),
    ],
)
def test_sec_f1_literal_handlers_and_data_rows_still_pass(spec):
    assert _ripple(spec) is not None


# SEC F3: json.loads keeps a repeated key's last value, so an earlier, refused
# one would go unchecked (and stream out). A strict body may not repeat a key,
# and is dropped, never passed through as a legacy card.
@pytest.mark.parametrize(
    "body",
    [
        '{"ui":{"type":"richtext","props":{"html":"<img src=x onerror=alert(1)>"}},'
        '"ui":{"type":"text","props":{"text":"ok"}}}',
        '{"ui":{"type":"button","props":{"label":"x"},'
        '"on_focus":{"action":"emit","target":"ask","value":{"text":"hi"}},'
        '"on_focus":{"action":"set","target":"a","value":1}}}',
        '{"kind":"product","items":[],"kind":"note"}',
    ],
)
def test_sec_f3_a_repeated_key_drops_a_ripple_card(body):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    assert render_card(body, [], profile=RIPPLE_PROFILE) is None


# SEC-2b B: a slot the engine draws as a node (NodeRenderer) holds a literal node
# or text. A lone expression there draws whatever state holds by then, and
# ``set`` pieces can build that node at run time, never checked. Every slot the
# engine and the manifest have, each with the expression and with a literal node.
_TEXT_NODE = {"type": "text", "props": {"text": "hi"}}


def _node_slot(name: str, v) -> dict:
    col = {"key": "a", "label": "A", "formatter": v}
    ui = {
        "settings-list control": ("settings-list", {"items": [{"label": "x", "control": v}]}),
        "tabs panels": ("tabs", {"tabs": [{"value": "a", "label": "A"}], "panels": [v]}),
        "split start": ("split", {"start": v}),
        "split end": ("split", {"end": v}),
        "master-detail detail": ("master-detail", {"items": [{"id": "a"}], "detail": v}),
        "kanban cardTemplate": (
            "kanban",
            {"columns": [{"id": "c", "title": "C"}], "cardTemplate": v},
        ),
        "data-grid formatter": ("data-grid", {"columns": [col], "rows": [{"a": 1}]}),
        "tree-table formatter": ("tree-table", {"columns": [col], "rows": [{"a": 1}]}),
        "virtual-list item": ("virtual-list", {"items": [1], "item": v}),
        "popover content": ("popover", {"trigger": "Open", "content": v}),
        "hover-card trigger": ("hover-card", {"trigger": v, "content": "x"}),
        "tooltip trigger": ("tooltip", {"trigger": v, "content": "x"}),
        "context-menu trigger": ("context-menu", {"trigger": v, "items": []}),
    }[name]
    return {"ui": {"type": ui[0], "props": ui[1]}, "state": {"n": _TEXT_NODE, "x": "x"}}


_NODE_SLOT_NAMES = [
    "settings-list control",
    "tabs panels",
    "split start",
    "split end",
    "master-detail detail",
    "kanban cardTemplate",
    "data-grid formatter",
    "tree-table formatter",
    "virtual-list item",
    "popover content",
    "hover-card trigger",
    "tooltip trigger",
    "context-menu trigger",
]


@pytest.mark.parametrize("name", _NODE_SLOT_NAMES)
@pytest.mark.parametrize(
    "value", ["{state.n}", " {state.n} ", "{item}", "{row}", {"type": "iframe"}, {"label": "x"}]
)
def test_sec_b_a_node_slot_refuses_an_expression_or_a_non_node(name, value):
    assert _ripple(_node_slot(name, value)) is None


@pytest.mark.parametrize("name", _NODE_SLOT_NAMES)
@pytest.mark.parametrize("value", [_TEXT_NODE, "Open", "Hi {state.x}"])
def test_sec_b_a_node_slot_takes_a_literal_node_or_text(name, value):
    assert _ripple(_node_slot(name, value)) is not None


@pytest.mark.parametrize(
    "ui",
    [
        {"type": "settings-list", "props": {"items": "{state.rows}"}},
        {"type": "settings-list", "props": {"items": ["{state.row}"]}},
        {"type": "tabs", "props": {"tabs": [{"value": "a", "label": "A"}], "panels": "{state.p}"}},
        {"type": "data-grid", "props": {"columns": "{state.cols}", "rows": [{"a": 1}]}},
        {"type": "tree-table", "props": {"columns": "{state.cols}", "rows": [{"a": 1}]}},
    ],
)
def test_sec_b_a_list_of_node_rows_is_literal(ui):
    # The rows would be resolved from state with their nodes inside.
    assert _ripple({"ui": ui, "state": {"rows": [], "p": [], "cols": []}}) is None


def test_sec_b_a_node_built_by_set_pieces_never_reaches_a_slot():
    # The finding's repro: on_focus builds a button with an api on_click in
    # state, and a settings row draws it.
    pieces = [
        {"action": "set", "target": "n.type", "value": "button"},
        {"action": "set", "target": "n.on_click.action", "value": "api"},
        {"action": "set", "target": "n.on_click.url", "value": "/x"},
    ]
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "input", "bind": "q", "props": {"label": "Name"}, "on_focus": pieces},
                {
                    "type": "settings-list",
                    "props": {"items": [{"label": "More", "control": "{state.n}"}]},
                },
            ],
        }
    }
    assert _ripple(spec) is None


def test_sec_b_node_slots_cover_the_engine_and_the_manifest():
    from pocketpaw_ee.paw_bar.card_spec import _NODE_PROPS, _NODE_ROWS

    assert _NODE_PROPS["split"] == {"start", "end"}
    assert _NODE_PROPS["master-detail"] == {"detail"}
    assert _NODE_PROPS["kanban"] == {"cardTemplate"}
    assert _NODE_PROPS["virtual-list"] == {"item"}
    # From the manifest's Array<{ control?: UISpec }>, not listed by hand.
    assert _NODE_ROWS[("settings-list", "items")] == "control"
    assert _NODE_ROWS[("tabs", "panels")] == ""
    assert _NODE_ROWS[("data-grid", "columns")] == _NODE_ROWS[("tree-table", "columns")]


# SEC-2b D: NaN / Infinity (or a float too big to hold) would reach card.final,
# and the SSE frame carrying it would not be JSON. Refused at parse, both profiles.
@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_sec_d_a_non_finite_number_drops_the_card(number):
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE, render_card

    body = '{"ui": {"type": "text", "props": {"text": "hi"}}, "state": {"v": ' + number + "}}"
    assert render_card(body, [], profile=RIPPLE_PROFILE) is None
    assert render_card(body, [], profile=PAWBAR_PROFILE) is None
    assert render_card(body.replace(number, "1.5e3"), [], profile=RIPPLE_PROFILE) is not None


# SEC-2b A (ripple profile): a body is read as the client reads it, trimmed of
# what JS trim() strips; one that still is not JSON is dropped, not passed on.
@pytest.mark.parametrize("ch", ["\ufeff", "\u00a0", "\u2028", "\u3000", "\x0b", "\x0c"])
def test_sec_a_a_trailing_trimmed_character_does_not_skip_the_ripple_checks(ch):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    ask = {"action": "emit", "target": "ask", "value": {"text": "hi"}}
    bad = _body({"ui": {"type": "button", "props": {"label": "x"}, "on_focus": ask}})
    assert render_card(bad + ch, [], profile=RIPPLE_PROFILE) is None
    good = _body({"ui": _TEXT_NODE})
    assert render_card(ch + good + ch, [], profile=RIPPLE_PROFILE) is not None
    assert render_card(good + "\x1c", [], profile=RIPPLE_PROFILE) is None


# SEC F7: a follow-up emits ``props.event`` (default "follow-up") with the typed
# text, so the event is an emit target: a host event the widget declares.
@pytest.mark.parametrize(
    ("props", "node", "verbs"),
    [
        ({"event": "ask"}, {}, ["checkout"]),
        ({"event": "checkout"}, {}, []),
        ({"event": "flow.submit"}, {}, ["checkout"]),
        ({"event": None}, {}, ["checkout"]),
        ({}, {}, ["checkout"]),
        ({"event": "steal"}, {"on_submit": {"action": "set", "target": "q"}}, ["checkout"]),
    ],
)
def test_sec_f7_a_follow_up_emits_only_a_declared_host_event(props, node, verbs):
    spec = {"ui": {"type": "follow-up", "props": props, **node}}
    assert _ripple(spec, verbs=verbs) is None


def test_sec_f7_a_follow_up_with_a_declared_event_or_its_own_submit_passes():
    declared = {"ui": {"type": "follow-up", "props": {"event": "checkout"}}}
    assert _ripple(declared, verbs=["checkout"]) is not None
    handled = {"ui": {"type": "follow-up", "on_submit": {"action": "set", "target": "q"}}}
    assert _ripple(handled, verbs=[]) is not None


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
        {
            "ui": {
                "type": "search",
                "props": {"results": [{"id": 1, "label": "x", "href": "file:///etc"}]},
            }
        },
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


def test_i2_the_rich_text_editor_is_not_a_ripple_widget():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE

    # Its value seeds HTML into a Tiptap editor.
    assert "rich-text" not in RIPPLE_PROFILE.widget_types
    spec = {"ui": {"type": "rich-text", "props": {"value": "<img src=x onerror=alert(1)>"}}}
    assert _ripple(spec) is None


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
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "//evil.com"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "https://evil.com"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "http://evil.com"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "data:text/html,<b>x</b>"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "http:evil.com"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "https:\\\\evil.com"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "secondaryCtaHref": "https://evil.com"}}},
        {"ui": {"type": "mention", "props": {"name": "x", "href": "{state.next}"}}},
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
    assert _ripple({"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "tel:+15550100"}}})
    assert _ripple({"ui": {"type": "text", "props": {"text": "Call tel:5550100, 10/12"}}})
    assert _ripple({"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "/contact"}}})


# R5: an expression can build a URL at render time. A URL-ish key refuses
# expressions outright; anywhere else, string literals concatenated inside an
# expression may not spell a script or data link.
@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "cta", "props": {"label": "x", "href": "{'java'+'script:alert(1)'}"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "href": "{state.a}:alert(1)"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "href": "javascript{state.c}"}}},
        {"ui": {"type": "cta", "props": {"label": "x", "ctaHref": "{state.a}:alert(1)"}}},
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
# Final pass: ops-only settings, markdown expressions, Unicode folding
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "markdown", "props": {"content": "Total {state.a + state.b}"}}},
        {"ui": {"type": "markdown", "props": {"content": "{state.items.map(x)}"}}},
        {"ui": {"type": "markdown", "props": {"content": "{'[x](' + state.u + ')'}"}}},
        {"ui": {"type": "markdown", "props": {"text": "{state.ok ? 'a' : 'b'}"}}},
        {"ui": {"type": "approval-gate", "props": {"body": "{state.a.concat(state.b)}"}}},
        {"ui": {"type": "stream-text", "props": {"text": "{state.a + 1}", "markdown": True}}},
        {"ui": {"type": "button", "on_click": {"action": "toast", "message": "{state.a + 'x'}"}}},
        {
            "ui": {
                "type": "button",
                "on_click": {
                    "action": "validate",
                    "condition": "state.a",
                    "message": "{f(state.a)}",
                },
            }
        },
    ],
)
def test_markdown_and_toast_text_take_only_plain_paths(spec):
    assert _ripple(spec) is None


def test_markdown_and_toast_text_keep_plain_paths():
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "markdown",
                    "props": {"content": "**{item.name}** has {state.a.b} left, row {index}"},
                },
                {
                    "type": "button",
                    "on_click": {"action": "toast", "message": "Saved {state.list[0].name}"},
                },
                # Not markdown: an ordinary text keeps full expressions.
                {"type": "text", "props": {"text": "Each pays {state.total / state.people}"}},
            ],
        },
        "state": {"a": {"b": 1}, "total": 4, "people": 2, "list": [{"name": "x"}]},
    }
    assert _ripple(spec) is not None


@pytest.mark.parametrize(
    "spec",
    [
        {
            "ui": {
                "type": "text",
                "props": {
                    "text": "\uff4a\uff41\uff56\uff41\uff53\uff43\uff52\uff49\uff50\uff54"
                    "\uff1aalert(1)"
                },
            }
        },
        {"ui": {"type": "text", "props": {"text": "java\u200bscript:alert(1)"}}},
        {"ui": {"type": "text", "props": {"text": "java\u00adscript:alert(1)"}}},
        {"ui": {"type": "text", "props": {"text": "java\u2060script:alert(1)"}}},
        {"ui": {"type": "text", "props": {"text": "java\ufeffscript:alert(1)"}}},
        {"ui": {"type": "image", "props": {"src": "\uff0f\uff0fevil.com/x.png"}}},
        {"ui": {"type": "image", "props": {"src": "https\u200b://evil.com/x.png"}}},
        {"ui": {"type": "markdown", "props": {"content": "[x](d\u200bata:text/html,x)"}}},
        {"ui": {"type": "text", "style": {"background": "u\u200brl(/x)"}}},
        {"ui": {"type": "text", "style": {"background": "\uff55\uff52\uff4c(/x)"}}},
    ],
)
def test_fullwidth_and_invisible_characters_cannot_hide_a_scheme(spec):
    assert _ripple(spec) is None


@pytest.mark.asyncio
async def test_settings_patch_refuses_ripple_off_the_ops_list(client, monkeypatch):
    c, _store = client
    site = await _settings_site()
    _pin_ops(monkeypatch, "some-other-site", cap=5.0)
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(url, json={"concierge_ui_profile": "ripple", "concierge_greeting": "Yo"})

    assert res.status_code == 403
    assert res.json()["detail"] == "ops_only_setting"
    body = (await c.get(url)).json()
    assert body["concierge_ui_profile"] == "pawbar"
    assert body["concierge_greeting"] != "Yo"  # nothing in the PATCH was written
    # pawbar is always fine.
    assert (await c.patch(url, json={"concierge_ui_profile": "pawbar"})).status_code == 200


@pytest.mark.asyncio
async def test_settings_patch_lets_owners_lower_the_cap_but_not_raise_it(client, monkeypatch):
    c, _store = client
    site = await _settings_site()
    _pin_ops(monkeypatch, cap=5.0)
    url = f"/paw-bar/admin/site/{site.id}/settings"

    res = await c.patch(url, json={"concierge_daily_spend_cap": 25})
    assert res.status_code == 403
    assert res.json()["detail"] == "ops_only_setting"
    assert (await c.get(url)).json()["concierge_daily_spend_cap"] is None
    for lower in (5, 3, 0):
        res = await c.patch(url, json={"concierge_daily_spend_cap": lower})
        assert res.status_code == 200, res.text
        assert res.json()["concierge_daily_spend_cap"] == lower
    assert (await c.patch(url, json={"concierge_daily_spend_cap": None})).status_code == 200

    # On the list, up to 100.
    _pin_ops(monkeypatch, str(site.id), cap=5.0)
    res = await c.patch(url, json={"concierge_daily_spend_cap": 25})
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_settings_patch_any_cap_lowers_when_there_is_no_global_cap(client, monkeypatch):
    c, _store = client
    site = await _settings_site()
    _pin_ops(monkeypatch, cap=0.0)
    url = f"/paw-bar/admin/site/{site.id}/settings"

    assert (await c.patch(url, json={"concierge_daily_spend_cap": 40})).status_code == 200


def test_the_ops_list_is_a_comma_separated_setting():
    from pocketpaw_ee.paw_bar.concierge_runtime import is_ops_site

    from pocketpaw.config import Settings

    assert Settings.model_fields["pawbar_ops_site_ids"].default == ""
    settings = SimpleNamespace(pawbar_ops_site_ids=" a1 , b2,,")
    assert is_ops_site(SimpleNamespace(id="b2"), settings)
    assert not is_ops_site(SimpleNamespace(id="c3"), settings)
    assert not is_ops_site(SimpleNamespace(id=""), SimpleNamespace(pawbar_ops_site_ids=""))
    assert not is_ops_site(None, settings)


# A card of the wrong shape is dropped, never raised into the turn.
_BAD_SHAPES = [
    '{"ui": "x"}',
    '{"ui": null}',
    '{"ui": [1]}',
    '{"ui": 1}',
    '{"ui": {"type": "text"}, "state": "x"}',
    '{"ui": {"type": "text"}, "state": [1]}',
    '{"kind": "product", "items": 5}',
    '{"items": [1, "a", null]}',
    '{"kind": "form", "fields": 7, "verb": 3}',
]


@pytest.mark.parametrize("body", _BAD_SHAPES)
@pytest.mark.parametrize("profile_name", ["PAWBAR_PROFILE", "RIPPLE_PROFILE"])
def test_a_card_of_the_wrong_shape_is_dropped_not_raised(body, profile_name):
    from pocketpaw_ee.paw_bar import card_spec

    profile = getattr(card_spec, profile_name)
    out = card_spec.render_card(body, [], profile=profile)
    if body.startswith('{"ui"'):
        assert out is None
    assert card_spec.validate_and_hydrate(json.loads(body), [], profile=profile) is None


def test_a_null_state_is_absent_on_pawbar_and_refused_on_ripple():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    plain = render_card('{"ui": {"type": "text"}}', [])
    assert plain is not None
    # paw-bar has always read a null state as no state; that does not change.
    assert render_card('{"ui": {"type": "text"}, "state": null}', []) == plain
    assert (
        render_card('{"ui": {"type": "text"}, "state": null}', [], profile=RIPPLE_PROFILE) is None
    )


def test_an_unexpected_error_in_the_checks_drops_the_card_and_logs_no_contents(monkeypatch, caplog):
    import logging

    from pocketpaw_ee.paw_bar import card_spec

    def _boom(*_a, **_kw):
        raise KeyError("boom")

    monkeypatch.setattr(card_spec, "_check_strict", _boom)
    monkeypatch.setattr(card_spec, "_check_tree", _boom)
    body = _body({"ui": {"type": "text", "props": {"text": "secret-visitor-words"}}})
    with caplog.at_level(logging.WARNING):
        assert card_spec.render_card(body, [], profile=card_spec.RIPPLE_PROFILE) is None
        assert card_spec.render_card(body, []) is None
    assert "card" in caplog.text
    assert "secret-visitor-words" not in caplog.text


# --------------------------------------------------------------------------- #
# The runtime
# --------------------------------------------------------------------------- #


def _widget(actions=()):
    return SimpleNamespace(id="w1", spec=SimpleNamespace(actions=list(actions)))


def test_the_profile_comes_from_the_site_and_ripple_needs_the_ops_list(monkeypatch):
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import ui_profile

    _pin_ops(monkeypatch, "s1", "s3")
    assert ui_profile(SimpleNamespace(id="s1", concierge_ui_profile="ripple")) is RIPPLE_PROFILE
    # A stored "ripple" off the list (set before, or written behind the PATCH) is pawbar.
    assert ui_profile(SimpleNamespace(id="s2", concierge_ui_profile="ripple")) is PAWBAR_PROFILE
    for site in (
        SimpleNamespace(id="s1"),
        SimpleNamespace(id="s1", concierge_ui_profile="RIPPLE"),
        None,
    ):
        assert ui_profile(site) is PAWBAR_PROFILE


def test_the_ripple_cards_paragraph_needs_no_catalog_or_lead_capture(monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import build_prompt

    _pin_ops(monkeypatch, "s1")
    site = SimpleNamespace(id="s1", concierge_ui_profile="ripple", concierge_lead_capture=False)
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
    ("ops", "own", "global_cap", "spent", "over"),
    [
        (False, None, 5.0, 4.0, False),  # no site cap: the global one
        (False, None, 5.0, 6.0, True),
        (False, 50.0, 5.0, 6.0, True),  # off the list a site cap only lowers
        (False, 2.0, 5.0, 3.0, True),
        (False, 2.0, 5.0, 1.0, False),
        (False, 0.0, 5.0, 0.0, True),  # 0 pauses the site
        (False, 10.0, 0.0, 11.0, True),  # a site cap holds with no global cap
        (False, 10.0, 0.0, 9.0, False),
        (True, 50.0, 5.0, 6.0, False),  # an ops site may go past the global cap
        (True, 50.0, 5.0, 50.0, True),
        (True, None, 5.0, 6.0, True),
    ],
)
async def test_the_site_cap_lowers_and_only_ops_sites_raise_it(
    monkeypatch, ops, own, global_cap, spent, over
):
    from pocketpaw_ee.paw_bar import concierge_runtime

    async def _spent(workspace_id, pocket_id, **_):
        return spent

    monkeypatch.setattr(concierge_runtime, "site_spend_today_usd", _spent)
    settings = SimpleNamespace(
        pawbar_concierge_daily_spend_cap=global_cap, pawbar_ops_site_ids="s1" if ops else ""
    )
    site = SimpleNamespace(id="s1", concierge_daily_spend_cap=own)
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
async def test_settings_patch_round_trips_the_profile_and_the_cap(client, monkeypatch):
    from pocketpaw_ee.cloud.models.site import Site

    c, _store = client
    site = await _settings_site()
    _pin_ops(monkeypatch, str(site.id), cap=5.0)
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


# --------------------------------------------------------------------------- #
# Ops-only rules on a real turn (a stale stored value can't get past them)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_turn_ignores_a_stored_ripple_profile_off_the_ops_list(
    concierge_client, model, monkeypatch
):
    from pocketpaw_ee.paw_bar import concierge_runtime

    from tests.cloud.test_paw_bar_concierge_v2 import _HOURS_KB, _chat, _seed_kb
    from tests.cloud.test_paw_bar_concierge_v2 import _site as _v2_site
    from tests.cloud.test_paw_bar_concierge_v2 import _widget as _v2_widget

    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    site = await _v2_site(concierge_ui_profile="ripple")
    widget = await store.create_widget(_v2_widget())

    _pin_ops(monkeypatch, "another-site")
    await _chat(client, widget.id)
    assert "Authoring rules:" not in model.user_prompt()
    assert model.last["info"].instructions == concierge_runtime.FRAME_LEADS

    _pin_ops(monkeypatch, str(site.id))
    await _chat(client, widget.id)
    assert "Authoring rules:" in model.user_prompt()
    assert model.last["info"].instructions == concierge_runtime.FRAME_DEMO


@pytest.mark.asyncio
async def test_a_turn_holds_a_stored_high_cap_to_the_global_one_off_the_ops_list(
    concierge_client, model, monkeypatch
):
    from tests.cloud.test_paw_bar_concierge_v2 import _HOURS_KB, _chat, _frames, _seed_kb
    from tests.cloud.test_paw_bar_concierge_v2 import _site as _v2_site
    from tests.cloud.test_paw_bar_concierge_v2 import _widget as _v2_widget
    from tests.cloud.test_paw_bar_concierge_v2_degrade import _LIMIT, _seed_spend

    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    site = await _v2_site(concierge_daily_spend_cap=50.0)
    widget = await store.create_widget(_v2_widget())
    await _seed_spend(0.6)

    _pin_ops(monkeypatch, cap=0.5)
    res = await _chat(client, widget.id)
    assert _LIMIT in _frames(res.text)
    assert model.calls == []

    _pin_ops(monkeypatch, str(site.id), cap=0.5)
    res = await _chat(client, widget.id)
    assert _LIMIT not in _frames(res.text)
    assert len(model.calls) == 1


# --------------------------------------------------------------------------- #
# Flow cards (Ripple's chain) and the ask host event
# --------------------------------------------------------------------------- #


def _go(target: str = "flow.next", label: str = "Next") -> dict:
    return {
        "type": "button",
        "props": {"label": label},
        "on_click": {"action": "emit", "target": target},
    }


def _step(n: int, *kids: dict, **more) -> dict:
    """Flow step ``n``: a question, then Back and Next."""
    ui = {"type": "flex", "children": [{"type": "text", "props": {"text": f"Q{n}"}}, *kids]}
    ui["children"] += [_go("flow.back", "Back"), _go()]
    return {"flowId": f"s{n}", "title": f"Step {n}", "ui": ui, **more}


def _flow(steps: int = 3, last: dict | None = None, **at: dict) -> dict:
    """A linear flow card of ``steps`` steps ending in a chat ``onComplete``;
    ``at`` (``s2={...}``) merges keys into one step, ``last`` into the last."""
    end = {"onComplete": {"kind": "chat", "message": "Plan it"}, **(last or {})}
    step = {**_step(steps, **end), **at.get(f"s{steps}", {})}
    for n in range(steps - 1, 0, -1):
        step = {**_step(n, chain=step), **at.get(f"s{n}", {})}
    return {"ui": {"version": "2.0", "intent": "custom", **step}, "state": {}}


def _ask(text: str = "Add a rest day", key: str = "on_click") -> dict:
    return {
        "type": "button",
        "props": {"label": "Ask"},
        key: {"action": "emit", "target": "ask", "value": {"text": text}},
    }


_ASKED = _ask()["on_click"]
# Handlers that fire without an explicit visitor action, or are not the composite
# button list keyed exactly ``actions``.
_NOT_ASK_HANDLERS = (
    "on_focus",
    "on_blur",
    "on_input",
    "on_change",
    "on_complete",
    "on_mount",
    "on_error",
    "on_flip",
    "finishActions",
    "nextActions",
    "refreshActions",
    "toggleActions",
    "submitActions",
)


def _asks_from(key: str) -> dict:
    return {"ui": {"type": "flex", "props": {key: _ASKED}}}


# The accepted shape: a step root (flow fields on ``ui``), each step's ``ui`` a
# node tree, ``chain_map`` branching on the pick, ``chain`` next, and a terminal
# ``onComplete`` that sends one chat message.
TRIP_FLOW = {
    "ui": {
        "version": "2.0",
        "id": "trip",
        "flowId": "style",
        "intent": "custom",
        "title": "Plan a trip",
        "description": "Three quick picks, then I write the plan.",
        "ui": {
            "type": "flex",
            "props": {"direction": "column", "gap": "8px"},
            "children": [
                {"type": "segmented", "bind": "style", "props": {"options": ["calm", "busy"]}},
                _go(),
            ],
        },
        "chain_map": {
            "calm": {
                "flowId": "days",
                "title": "How many days?",
                "ui": {
                    "type": "flex",
                    "children": [
                        {"type": "slider", "bind": "days", "props": {"min": 2, "max": 10}},
                        _go("flow.back", "Back"),
                        _go(),
                    ],
                },
                "chain": {
                    "flowId": "who",
                    "title": "Who is going?",
                    "form_fields": [{"name": "people", "label": "People", "required": True}],
                    "ui": {
                        "type": "flex",
                        "children": [
                            {"type": "number-input", "bind": "people"},
                            _go("flow.submit", "Done"),
                        ],
                    },
                    "onComplete": {
                        "kind": "chat",
                        "message": "Plan a calm trip",
                    },
                },
            },
            "busy": {
                "flowId": "busy",
                "title": "Packed days it is",
                "ui": {
                    "type": "flex",
                    "children": [_ask("Plan a packed trip instead"), _go("flow.forward", "Skip")],
                },
                "onComplete": {"kind": "chat", "message": "Plan a packed trip"},
            },
        },
    },
    "state": {"style": "calm", "days": 4, "people": 2},
}


def test_fl1_a_flow_card_passes_ripple_whole_and_not_pawbar():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    for card in (TRIP_FLOW, _flow(3), _flow(8)):
        out = _ripple(card)
        assert out is not None
        assert json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0]) == card
        # The pawbar profile is unchanged: a typeless root is no node there.
        assert render_card(_body(card), []) is None
    assert render_card(_body(TRIP_FLOW), [], profile=RIPPLE_PROFILE, verbs=[]) is not None


def _fanout(branches: int) -> dict:
    root = _step(1, chain_map={f"b{i}": _step(i + 2) for i in range(branches)})
    return {"ui": root}


def _wide_step(n: int, kids: int) -> dict:
    return {"flowId": f"w{n}", "ui": {"type": "flex", "children": [{"type": "text"}] * kids}}


_FLOW_REFUSALS = [
    (_flow(3, s2={"data": {"items": []}}), "a step key outside the allowlist"),
    (_flow(3, s2={"type": "flex"}), "a node's fields on a step"),
    (
        _flow(3, last={"onComplete": {"kind": "invoke_tool", "tool": "delete_all"}}),
        "onComplete invoke_tool",
    ),
    (
        _flow(3, last={"onComplete": {"kind": "invoke_tool", "message": "x"}}),
        "onComplete invoke_tool shaped like a chat",
    ),
    (
        _flow(3, last={"onComplete": {"kind": "call_binding", "binding": "x"}}),
        "onComplete call_binding",
    ),
    (_flow(3, last={"onComplete": {"kind": "create_pocket"}}), "onComplete create_pocket"),
    (_flow(3, last={"onComplete": {"kind": "navigate", "url": "/x"}}), "onComplete navigate"),
    (_flow(3, last={"onComplete": {"kind": "emit", "event": "checkout"}}), "onComplete emit"),
    (_flow(3, last={"onComplete": {"kind": "made_up", "message": "x"}}), "onComplete unknown kind"),
    (
        _flow(
            3, last={"onComplete": {"kind": "chat", "message": "x", "then": {"kind": "navigate"}}}
        ),
        "a chat onComplete with a then",
    ),
    (
        _flow(3, last={"onComplete": {"kind": "chat", "message": 7}}),
        "a chat onComplete with no text",
    ),
    (
        _flow(3, last={"onComplete": {"kind": "chat", "message": "x" * 501}}),
        "a chat onComplete over 500 chars",
    ),
    (
        _flow(3, last={"onComplete": {"kind": "chat", "message": "go javascript:alert(1)"}}),
        "a script link in the message",
    ),
    (_flow(3, last={"onComplete": "chat"}), "onComplete not an object"),
    ({"ui": _go()}, "a flow emit outside a flow card"),
    ({"ui": {"type": "flex", "children": [_go("flow.submit")]}}, "flow.submit outside a flow card"),
    (
        {"ui": _go()} | {"state": {"next": {"action": "emit", "target": "flow.back"}}},
        "a flow emit in a plain card's state",
    ),
    (
        _flow(3, s2={"ui": {"type": "flex", "children": [_go("flow.jump")]}}),
        "an emit to a flow verb Ripple has not got",
    ),
    (_flow(9), "nine steps"),
    (_fanout(8), "nine steps through chain_map"),
    ({"ui": {**_wide_step(1, 200), "chain": _wide_step(2, 200)}}, "402 nodes across two steps"),
    (_flow(2, s2={"ui": _chain(17)["ui"]}), "a step nested deeper than 16"),
    (
        _flow(3, s3={"ui": {"type": "image", "props": {"src": "https://evil.example/x.png"}}}),
        "a full URL in step 3",
    ),
    (_flow(3, s3={"title": "See https://evil.example"}), "a full URL in step 3's title"),
    (_flow(3, s2={"ui": {"type": "embed", "props": {"url": "/x"}}}), "a deferred widget in a step"),
    (
        _flow(3, s2={"ui": {"type": "button", "on_click": {"action": "api", "url": "/x"}}}),
        "an action off the set in a step",
    ),
    (_flow(2, s2={"ui": None}), "a step whose ui is not a node"),
    (_flow(2, s2={"ui": {"children": [_go()]}}), "a step whose ui has no type"),
    ({"ui": {"flowId": "a", "title": "No ui"}}, "a step with no ui"),
    ({"ui": {**_step(1), "chain": "next"}}, "a chain that is not a step"),
    ({"ui": {**_step(1), "chain_map": [_step(2)]}}, "a chain_map that is not an object"),
    ({"ui": {"title": "x"}}, "a typeless root with no flow fields"),
    ({"ui": _ask("x" * 501)}, "an ask over 500 chars"),
    ({"ui": _ask("hi javascript:alert(1)")}, "an ask with a script link"),
    (
        {"ui": {"type": "button", "on_click": {"action": "emit", "target": "ask", "value": "hi"}}},
        "an ask that is not {text}",
    ),
    (
        {
            "ui": {
                "type": "button",
                "on_click": {"action": "emit", "target": "ask", "value": {"text": "hi", "to": "x"}},
            }
        },
        "an ask with more than text",
    ),
    (
        {"ui": {"type": "button", "on_click": {"action": "emit", "target": "ask"}}},
        "an ask with no value",
    ),
    # ask only on an explicit visitor action: every other handler is refused.
    *[(_asks_from(key), f"an ask in {key}") for key in _NOT_ASK_HANDLERS],
    (
        {"ui": {"type": "input", "props": {"autofocus": True}, "on_focus": _ASKED}},
        "an ask in on_focus with autofocus (fires on mount)",
    ),
    ({"ui": {"type": "input", "props": {"on_input": _ASKED}}}, "an ask in a props on_input"),
    ({"ui": {"type": "timer", "props": {"minutes": 1}, "on_complete": _ASKED}}, "a timer's ask"),
    ({"ui": {"type": "wizard-layout", "props": {"finishActions": _ASKED}}}, "a wizard's ask"),
    (
        {"ui": {"type": "input", "on_focus": {"action": "flow", "steps": [], "actions": [_ASKED]}}},
        "an actions list nested in on_focus",
    ),
    ({"ui": {"type": "text", "props": {"x": _ASKED}}}, "an ask outside any handler"),
    ({"ui": {"type": "text"}, "state": {"go": _ASKED}}, "an ask in state"),
    ({"ui": {"type": "text"}, "state": {"actions": [_ASKED]}}, "an actions list in state"),
    ({"ui": {"type": "text"}, "state": {"b": _ask()}}, "a button kept in state"),
    (_flow(2, s2={"form_fields": [{"name": "x", "actions": [_ASKED]}]}), "an ask in form_fields"),
]


@pytest.mark.parametrize(("spec", "why"), _FLOW_REFUSALS, ids=[w for _, w in _FLOW_REFUSALS])
def test_fl1_flow_and_ask_refusals(spec, why):
    assert _ripple(spec) is None, why


def test_fl1_the_step_and_node_bounds_are_inclusive():
    assert _ripple(_flow(8)) is not None
    assert _ripple(_fanout(7)) is not None
    # 400 nodes across two steps, each alone well under the bound.
    assert _ripple({"ui": {**_wide_step(1, 199), "chain": _wide_step(2, 199)}}) is not None
    # Each step is its own root, so each may nest 16 deep.
    assert _ripple(_flow(2, s1={"ui": _chain(16)["ui"]}, s2={"ui": _chain(16)["ui"]})) is not None
    assert (
        _ripple(_flow(3, last={"onComplete": {"kind": "chat", "message": "x" * 500}})) is not None
    )


_IN_FLOW = {"action": "flow", "steps": [{"action": "set", "target": "a", "value": 1}, _ASKED]}
_IN_BRANCH = {"action": "branch", "if": "1", "then": [_ASKED]}
# Explicit visitor actions: a click, a submit, a pick, a composite's button list.
_ASK_PASSES = [
    ({"ui": _ask("x" * 500)}, "on_click at the cap"),
    ({"ui": {"type": "button", "props": {"on_click": _ASKED}}}, "a props on_click"),
    ({"ui": {"type": "button", "on_click": _IN_FLOW}}, "a flow under on_click"),
    ({"ui": {"type": "button", "on_click": _IN_BRANCH}}, "a branch under on_click"),
    ({"ui": {"type": "calendar", "props": {"on_select": _ASKED}}}, "on_select"),
    ({"ui": {"type": "calendar", "on_select": _ASKED}}, "a node's on_select"),
    (
        {"ui": {"type": "form", "props": {"fields": [{"name": "q"}]}, "on_submit": _ASKED}},
        "on_submit",
    ),
    (
        {
            "ui": {
                "type": "comparison-layout",
                "props": {"items": [{"id": "a", "title": "Plan A", "actions": [_ASKED]}]},
            }
        },
        "comparison items[].actions",
    ),
    (
        {
            "ui": {
                "type": "entity-detail",
                "props": {"title": "Sam", "actions": [{"label": "Ask", "actions": _IN_FLOW}]},
            }
        },
        "entity-detail actions[].actions with a flow",
    ),
    (
        {
            "ui": {
                "type": "order-status",
                "props": {"actions": [{"label": "Ask", "actions": [_IN_BRANCH]}]},
            }
        },
        "order-status actions[].actions with a branch",
    ),
    (_flow(2, s2={"ui": _ask()}), "an ask in a flow step"),
]


@pytest.mark.parametrize(("card", "why"), _ASK_PASSES, ids=[w for _, w in _ASK_PASSES])
def test_fl1_an_ask_passes_on_an_explicit_visitor_action_and_only_on_ripple(card, why):
    from pocketpaw_ee.paw_bar.card_spec import render_card

    assert _ripple(card) is not None, why
    assert render_card(_body(card), []) is None  # pawbar has no ask
    assert render_card(_body({"ui": _ask()}), [], verbs=["checkout"]) is None


# SEC F2: flow.submit at a terminal step sends its onComplete chat message, so it
# is held to the same explicit-visitor-action rule as ask (the findings' repro
# first). flow.next / back / forward stay ungated.
_SUBMIT = {"action": "emit", "target": "flow.submit"}


def _submits_from(key: str) -> dict:
    return _flow(1, s1={"ui": {"type": "flex", "props": {key: _SUBMIT}}})


_SUBMIT_REFUSALS = [
    (
        {
            "ui": {
                "flowId": "s1",
                "onComplete": {"kind": "chat", "message": "Send my plan"},
                "ui": {
                    "type": "input",
                    "bind": "q",
                    "props": {"label": "Your name"},
                    "on_input": _SUBMIT,
                },
            }
        },
        "the findings' on_input repro",
    ),
    *[(_submits_from(key), f"flow.submit in {key}") for key in _NOT_ASK_HANDLERS],
    (
        _flow(1, s1={"ui": {"type": "input", "on_focus": {"action": "flow", "steps": [_SUBMIT]}}}),
        "in a flow under on_focus",
    ),
    (_flow(1, s1={"form_fields": [{"name": "x", "actions": [_SUBMIT]}]}), "in form_fields"),
]


@pytest.mark.parametrize(("spec", "why"), _SUBMIT_REFUSALS, ids=[w for _, w in _SUBMIT_REFUSALS])
def test_sec_f2_flow_submit_only_from_an_explicit_visitor_action(spec, why):
    assert _ripple(spec) is None, why


def test_sec_f2_flow_submit_passes_on_a_click_a_submit_or_a_button_list():
    assert _ripple(TRIP_FLOW) is not None
    form = {"type": "form", "props": {"fields": [{"name": "q"}]}, "on_submit": _SUBMIT}
    assert _ripple(_flow(1, s1={"ui": form})) is not None
    rows = {"type": "comparison-layout", "props": {"items": [{"id": "a", "actions": [_SUBMIT]}]}}
    assert _ripple(_flow(1, s1={"ui": rows})) is not None
    focus_next = {"type": "input", "on_focus": {"action": "emit", "target": "flow.next"}}
    assert _ripple(_flow(1, s1={"ui": focus_next})) is not None


# SEC F4: what an expression resolves to is never seen here (a 9-character
# "{state.big}" sends 60,000), so the ask text and the onComplete message are
# plain text.
@pytest.mark.parametrize(
    "spec",
    [
        {"ui": _ask("{state.big}"), "state": {"big": "x" * 60_000}},
        {"ui": _ask("Hi {state.name}")},
        _flow(1, last={"onComplete": {"kind": "chat", "message": "{state.days} days"}}),
        _flow(1, last={"onComplete": {"kind": "chat", "message": "[x]({state.a}{state.b})"}}),
    ],
)
def test_sec_f4_the_ask_text_and_the_chat_message_are_plain_text(spec):
    assert _ripple(spec) is None


# SEC F5: only a step's onComplete is read and checked; anywhere else it is refused.
@pytest.mark.parametrize(
    "spec",
    [
        {
            "ui": {
                "flowId": "a",
                "onComplete": {"kind": "chat", "message": "x"},
                "ui": {"type": "flex", "onComplete": {"kind": "invoke_tool", "tool": "delete_all"}},
            }
        },
        _flow(
            2,
            s2={"ui": {"type": "flex", "props": {"onComplete": {"kind": "chat", "message": "x"}}}},
        ),
        {"ui": {"type": "text"}, "state": {"onComplete": {"kind": "invoke_tool"}}},
        _flow(2, s2={"form_fields": [{"name": "x", "onComplete": {"kind": "navigate"}}]}),
    ],
)
def test_sec_f5_an_on_complete_off_a_step_is_refused(spec):
    assert _ripple(spec) is None


# A node met inside a passive handler may not ask from its own click either.
def test_sec_a_node_inside_a_passive_handler_may_not_ask():
    spec = {"ui": {"type": "input", "on_focus": {"action": "set", "target": "b", "value": _ask()}}}
    assert _ripple(spec) is None


# SEC F6: a lead form inside a flow step is the lead card, so the runner does not
# add a second contact reply.
def test_sec_f6_a_lead_form_in_a_flow_step_is_the_lead_card():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, has_lead_form, render_card

    form = {"type": "form", "props": {"verb": "send_to_team", "fields": [{"name": "email"}]}}
    card = _flow(2, s2={"ui": form})
    body = _body(card)
    assert render_card(body, [], profile=RIPPLE_PROFILE, lead_capture=True) is not None
    assert has_lead_form(body)
    assert not has_lead_form(_body(_flow(2)))


# --------------------------------------------------------------------------- #
# Closer repair: a ripple body that is not JSON only for missing closers
# --------------------------------------------------------------------------- #

_MISSING_BRACE = Path(__file__).parents[1] / "fixtures" / "ripple_itinerary_missing_brace.json"
_REPAIR_LOG = "card_spec: repaired a card missing %d closing bracket(s)"


def _lisbon() -> str:
    """The live itinerary body whose ``ui`` node never closes before ``,"state":``."""
    return json.loads(_MISSING_BRACE.read_text(encoding="utf-8"))["body"]


def _ripple_body(body: str, **kw) -> dict | None:
    """The card object ``render_card`` emits for ``body`` on ripple, or None."""
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    out = render_card(body, [], profile=RIPPLE_PROFILE, **kw)
    return None if out is None else json.loads(out[len("```pawbar-card\n") : -len("\n```")])


def _cut_end(body: str, n: int) -> str:
    """``body`` without its last ``n`` characters, all closing brackets."""
    assert set(body[-n:]) <= set("}]")
    return body[:-n]


def _cut_ui_brace(body: str) -> str:
    """``body`` without the ``}`` closing its ``ui`` node, before ``, "state": ``."""
    i = body.rindex(', "state": ')
    assert body[i - 1] == "}"
    return body[: i - 1] + body[i:]


def _repairs(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "pocketpaw_ee.paw_bar.card_spec" and r.msg == _REPAIR_LOG
    ]


def test_the_live_itinerary_missing_its_ui_brace_is_repaired_then_checked(caplog):
    body = _lisbon()
    with pytest.raises(ValueError):
        json.loads(body)
    i = body.rindex(',"state":')
    with caplog.at_level("INFO", logger="pocketpaw_ee.paw_bar.card_spec"):
        card = _ripple_body(body)
    # One brace, before the root's state: not at the end, where state would
    # land inside the ui node.
    assert card == json.loads(body[:i] + "}" + body[i:])
    assert card["state"] == {} and "state" not in card["ui"]
    assert card["ui"]["type"] == "itinerary" and len(card["ui"]["props"]["days"]) == 2
    assert _repairs(caplog) == ["card_spec: repaired a card missing 1 closing bracket(s)"]
    assert "Lisbon" not in caplog.text


def test_pawbar_never_repairs_a_body():
    from pocketpaw_ee.paw_bar.card_spec import (
        PAWBAR_PROFILE,
        RIPPLE_PROFILE,
        has_lead_form,
        render_card,
    )

    body = _cut_end(_body({"ui": _TEXT_NODE}), 1)
    assert render_card(body, [], profile=RIPPLE_PROFILE) is not None
    assert render_card(body, [], profile=PAWBAR_PROFILE) is None
    assert render_card(body + "}", [], profile=PAWBAR_PROFILE) is not None
    form = {"type": "form", "props": {"verb": "send_to_team", "fields": [{"name": "email"}]}}
    lead = _cut_end(_body({"ui": form}), 1)
    assert has_lead_form(lead, RIPPLE_PROFILE)
    assert not has_lead_form(lead)


def test_closers_cut_off_the_end_are_restored_up_to_four():
    spec = _chain(6)
    body = _body(spec)
    for n in (1, 2, 3, 4):
        assert _ripple_body(_cut_end(body, n)) == spec, n
    assert _ripple_body(_cut_end(body, 5)) is None


def test_the_ui_brace_and_the_end_closers_count_together():
    spec = {"ui": _chain(2)["ui"], "state": {"a": [{"b": 1}]}}
    body = _cut_ui_brace(_body(spec))
    assert _ripple_body(body) == spec
    assert _ripple_body(_cut_end(body, 3)) == spec  # 1 + 3
    assert _ripple_body(_cut_end(body, 4)) is None  # 1 + 4


@pytest.mark.parametrize(
    "body",
    [
        '{"ui":{"type":"text" "props":{"text":"hi"}}}',  # a missing comma
        '{"ui":{"type":"text" "props":{"text":"hi"}}',  # ...and a closer
        '{"ui":{"type":"text","props":{"text":"hi}}}',  # a missing quote
        '{"ui":{"type":"text","props":{"text":hi"}}}',
        '{"ui":{"type":"text","props":{"text":"hi"}}}}',  # an extra closer stays
        '{"ui":{"type":"text","props":{"text":"hi"]}}',  # a wrong one is not swapped
        '{"ui":{"type":"text","props":{"text":"hi"}}} x',
        '{"ui":{"type":"text","props":{"text":"hi"}},"state":}',
        '{"ui":{"type":"text","props":{"text":"hi"}},"state"',
    ],
)
def test_a_body_broken_other_than_by_missing_closers_stays_refused(body):
    assert _ripple_body(body) is None


def test_brackets_and_escapes_inside_strings_are_not_closers():
    spec = {"ui": {"type": "text", "props": {"text": 'say "}]" then ] and \\ ok'}}}
    body = _body(spec)
    assert _ripple_body(body) == spec
    assert _ripple_body(_cut_end(body, 2)) == spec


_SURPLUS_BRACE = Path(__file__).parents[1] / "fixtures" / "ripple_flow_surplus_brace.json"
_SURPLUS_LOG = "card_spec: repaired a card with a surplus closer before its state"


def _trip_surplus() -> str:
    """The live flow body with one ``}`` too many before ``,"state":``."""
    return json.loads(_SURPLUS_BRACE.read_text(encoding="utf-8"))["body"]


def _add_root_brace(body: str, n: int = 1) -> str:
    """``body`` (``_body``'s spacing) with ``n`` more ``}`` before its root's state."""
    i = body.rindex(', "state": ')
    return body[:i] + "}" * n + body[i:]


def test_the_live_flow_with_a_surplus_brace_is_repaired_then_checked(caplog):
    body = _trip_surplus()
    with pytest.raises(json.JSONDecodeError, match="Extra data"):
        json.loads(body)
    i = body.rindex(',"state":')
    assert body[i - 1] == "}"
    with caplog.at_level("INFO", logger="pocketpaw_ee.paw_bar.card_spec"):
        card = _ripple_body(body)
    assert card == json.loads(body[: i - 1] + body[i:])
    assert card["state"] == {} and card["ui"]["flowId"] == "trip_style"
    assert [r.getMessage() for r in caplog.records if r.msg == _SURPLUS_LOG] == [_SURPLUS_LOG]
    assert "Food" not in caplog.text


@pytest.mark.parametrize(
    "spec",
    [
        {"ui": _TEXT_NODE, "state": {"a": [1, {"b": "}"}]}},
        {"ui": _chain(3)["ui"], "state": {}},
    ],
)
def test_one_surplus_closer_before_state_is_dropped(spec):
    body = _add_root_brace(_body(spec))
    assert _ripple_body(body) == spec
    assert _ripple_body(body.replace(', "state": ', ' ,\n "state" : ')) == spec


@pytest.mark.parametrize(
    "body",
    [
        _add_root_brace(_body({"ui": _TEXT_NODE, "state": {}}), 2),  # two surplus
        _add_root_brace(_body({"ui": _TEXT_NODE, "state": {}})) + "}",  # and one at the end
        _add_root_brace(_body({"ui": _TEXT_NODE, "state": {}}))[:-1],  # root never closes
        _add_root_brace(_body({"ui": _TEXT_NODE, "state": []})),  # state not an object
        _add_root_brace(_body({"ui": _TEXT_NODE, "state": {}, "x": 1})),  # more than state
        '{"ui":{"type":"text","props":{"text":"hi"}}},"theme":{}}',  # not state
        '{"ui":{"type":"text","props":{"text":"hi"}}},"state":{} x}',
        '{"kind":"note","text":"hi"},"state":{}}',  # legacy
    ],
)
def test_only_one_surplus_closer_before_a_lone_state_is_dropped(body):
    assert _ripple_body(body) is None


@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "image", "props": {"src": "https://evil.example/x.png"}}, "state": {}},
        {"ui": {"type": "button", "on_click": {"action": "fetch"}}, "state": {}},
        {"ui": _TEXT_NODE, "state": {"x": {"action": "set", "target": "a"}}},
    ],
)
def test_a_surplus_closer_repair_is_still_refused_for_anything_else(spec):
    body = _add_root_brace(_body(spec))
    assert _ripple_body(_add_root_brace(_body({"ui": _TEXT_NODE, "state": {}}))) is not None
    assert _ripple_body(body) is None


def test_a_surplus_closer_repair_still_refuses_a_repeated_key():
    ok = '{"ui":{"type":"text","props":{"text":"hi"}}},"state":{"a":1,"b":2}}'
    assert _ripple_body(ok) is not None
    assert _ripple_body(ok.replace('"b"', '"a"')) is None


def test_pawbar_never_drops_a_surplus_closer():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, render_card

    body = _add_root_brace(_body({"ui": _TEXT_NODE, "state": {}}))
    assert render_card(body, [], profile=PAWBAR_PROFILE) is None


# A legacy card passes through verbatim, unchecked, so only a spec is repaired.
def test_a_legacy_card_is_never_repaired():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    body = '{"kind":"note","text":"hi"}'
    assert render_card(body, [], profile=RIPPLE_PROFILE) is not None
    assert render_card(body[:-1], [], profile=RIPPLE_PROFILE) is None


def test_a_body_past_the_size_bound_is_not_repaired(caplog):
    body = _cut_end(_body({"ui": _TEXT_NODE, "state": {"pad": "x" * 64_000}}), 1)
    with caplog.at_level("INFO", logger="pocketpaw_ee.paw_bar.card_spec"):
        assert _ripple_body(body) is None
    assert _repairs(caplog) == []


# What a repair inserts is closers; every other check reads the repaired body.
@pytest.mark.parametrize(
    "spec",
    [
        {"ui": {"type": "image", "props": {"src": "https://evil.test/x.png"}}, "state": {}},
        {"ui": {"type": "text", "props": {"meta": {"x": {"action": "invoke_tool"}}}}, "state": {}},
        {"ui": {"type": "button", "on_click": {"action": "emit", "target": "pay"}}, "state": {}},
        {"ui": {"type": "button", "props": {"label": "x"}, "on_click": "{state.go}"}, "state": {}},
        {"ui": _TEXT_NODE, "state": {"go": {"action": "navigate", "url": "/x"}}},
        {"ui": {"type": "no-such-widget"}, "state": {}},
    ],
)
def test_a_repaired_card_is_still_refused_for_anything_else(spec):
    body = _body(spec)
    assert _ripple_body(body) is None
    assert _ripple_body(_cut_end(body, 1)) is None
    assert _ripple_body(_cut_ui_brace(body)) is None


# The repeat sits in the object left open (the root), so the parser meets it only
# once the repair closes it; the first, refused ui would otherwise go unchecked.
def test_a_repaired_body_still_may_not_repeat_a_key():
    body = (
        '{"ui":{"type":"richtext","props":{"html":"<img src=x onerror=alert(1)>"}},'
        '"ui":{"type":"text","props":{"text":"ok"}}'
    )
    assert _ripple_body(body + "}") is None
    assert _ripple_body(body) is None
    assert _ripple_body('{"ui":{"type":"text","props":{"text":"ok"}}') is not None


# --------------------------------------------------------------------------- #
# bill-split
# --------------------------------------------------------------------------- #


def _bill(**props) -> dict:
    base = {
        "title": "Dinner for 4",
        "currency": "USD",
        "subtotal": 186.4,
        "tip_percent": 18,
        "people": [{"id": f"p{i}", "name": f"P{i}", "extras": 0} for i in range(1, 5)],
    }
    return {"ui": {"type": "bill-split", "bind": "{state.bill}", "props": {**base, **props}}}


def test_a_bill_split_card_passes_the_full_validator():
    from pocketpaw_ee.paw_bar.card_spec import PAWBAR_PROFILE, render_card

    full = _bill(tax=14.9, tip_options=[10, 15, 20], extras_label="Drinks", note="Even split")
    for spec in (_bill(), full, _bill(people=_people(2)), _bill(people=_people(12))):
        assert _ripple(spec) is not None
    assert render_card(_body(_bill()), [], profile=PAWBAR_PROFILE) is None


def _people(n: int) -> list[dict]:
    return [{"id": f"p{i}", "name": f"P{i}"} for i in range(n)]


@pytest.mark.parametrize(
    "props",
    [
        {"people": _people(1)},
        {"people": _people(13)},
        {"people": "4 people"},
        {"people": [*_people(2), "p3"]},
        {"people": [{"id": "p1", "name": "A"}, {"id": "p2"}]},
        {"people": [{"id": "p1", "name": "A"}, {"id": "p2", "name": "B", "extras": "9"}]},
        {"subtotal": "186.4"},
        {"subtotal": True},
        {"subtotal": "{state.total}"},
        {"subtotal": None},
        {"tax": "14"},
        {"tip_percent": False},
        {"tip_options": [15, "18"]},
        {"tip_options": 18},
        {"title": 4},
        {"currency": {"code": "USD"}},
        {"extras_label": ["Drinks"]},
        {"note": 1},
    ],
)
def test_a_bill_split_with_bad_props_is_refused(props):
    assert _ripple(_bill(**props)) is None


def test_a_bill_split_with_a_non_finite_subtotal_is_refused():
    from pocketpaw_ee.paw_bar.card_spec import _check_bill_split, _Reject

    body = _body(_bill(subtotal=1.5))
    assert '"subtotal": 1.5' in body and _ripple_body(body) is not None
    for bad in ("NaN", "Infinity", "1e999"):
        assert _ripple_body(body.replace('"subtotal": 1.5', f'"subtotal": {bad}')) is None
    for bad in (float("nan"), float("inf")):
        with pytest.raises(_Reject):
            _check_bill_split({}, _bill(subtotal=bad)["ui"]["props"])


def _bill_with(node: dict = {}, props: dict = {}) -> dict:  # noqa: B006 (read only)
    ui = _bill()["ui"]
    return {"ui": {**ui, **node, "props": {**ui["props"], **props}}}


@pytest.mark.parametrize(
    "spec",
    [
        _bill_with(node={"on_change": {"action": "set", "target": "x", "value": 1}}),
        _bill_with(props={"on_change": {"action": "toast", "message": "hi"}}),
    ],
)
def test_a_handler_on_a_bill_split_is_refused(spec):
    assert _ripple(spec) is None


def test_the_bill_split_typed_line_lists_its_props():
    line = _fields(_listing()["bill-split"])
    assert line == (
        "- bill-split {subtotal: number, tax?: number, tip_percent?: number, "
        "tip_options?: number[], people: [{id,name,extras?:number}], extras_label?, note?"
    )


def test_the_rules_map_a_bill_split_and_leave_its_bind_unseeded():
    text = _ripple_paragraph()
    assert "Splitting a bill or a tip between people is a bill-split" in text
    assert "never seed its bind key in state" in text
    assert "(a bill split," not in text


# --------------------------------------------------------------------------- #
# Flow choice buttons (ripple choice cards): icon and description
# --------------------------------------------------------------------------- #


def _choice(**props) -> dict:
    button = {
        "type": "button",
        "props": {"label": "Food", **props},
        "on_click": {
            "action": "emit",
            "target": "flow.submit",
            "value": {"selection": {"id": "food", "label": "Food"}},
        },
    }
    return {
        "ui": {
            "flowId": "trip_style",
            "intent": "select",
            "title": "What kind of trip?",
            "ui": {"type": "flex", "children": [button]},
            "onComplete": {"kind": "chat", "message": "Plan a trip for me."},
        }
    }


@pytest.mark.parametrize(
    "props",
    [
        {},
        {"icon": "food", "description": "Markets, street stalls and long lunches"},
        {"icon": "not-a-known-icon"},  # unknown keys pass: the widget ignores them
        {"icon": "a" * 24, "description": "x" * 120},
        {"icon": None, "description": None},
    ],
)
def test_a_choice_button_with_an_icon_key_and_a_short_hint_passes(props):
    assert _ripple(_choice(**props)) is not None
    assert _ripple({"ui": {"type": "button", "props": {"label": "Go", **props}}}) is not None


@pytest.mark.parametrize(
    "props",
    [
        {"icon": "<svg onload=alert(1)>"},
        {"icon": "https://evil.test/i.png"},
        {"icon": "/icons/food.png"},
        {"icon": "Food"},
        {"icon": "{state.icon}"},
        {"icon": ""},
        {"icon": "a" * 25},
        {"icon": 3},
        {"icon": ["food"]},
        {"description": "x" * 121},
        {"description": "Pick {state.x}"},
        {"description": 7},
        {"description": {"text": "hi"}},
    ],
)
def test_a_choice_button_with_a_bad_icon_or_hint_is_refused(props):
    assert _ripple(_choice(**props)) is None
    assert _ripple({"ui": {"type": "button", "props": {"label": "Go", **props}}}) is None


def test_the_button_carries_the_choice_props_and_the_rules_teach_them_briefly():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_MANIFEST

    button = next(w for w in RIPPLE_MANIFEST["widgets"] if w["type"] == "button")
    assert {"icon", "description"} <= button["props"].keys()
    assert "icon?, description?" in _listing()["button"]
    text = _ripple_paragraph()
    assert "Choice buttons: label at most 18 chars" in text
    assert "description hint up to 60" in text
    assert "coffee drinks culture" not in text  # the icon keys stay out of the prompt
