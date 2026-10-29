# tests/cloud/test_ripple_each_props_lift.py — the spec normalizer lifts an
# ``each`` node's loop fields out of ``props``.
#
# Ripple reads ``items`` / ``item_as`` / ``index_as`` only as node-level fields
# on ``each``; inside ``props`` they are ignored and the loop renders nothing.
# The chat agent sometimes writes ``{"type":"each","props":{"items":...}}``.
# ``normalize_ripple_spec`` (the chokepoint for chat cards and every pocket
# persist/read path) moves them to the node. Node level wins when both exist,
# and the props copy is always dropped.

from __future__ import annotations

import copy
import logging
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec


def _packing_spec(each: dict) -> dict:
    """The live failing card: an ``each`` nested inside card > flex."""
    return {
        "version": "1.0",
        "state": {"clothes": [{"name": "Socks", "packed": False}]},
        "ui": {
            "type": "card",
            "props": {"title": "Packing"},
            "children": [
                {
                    "type": "flex",
                    "props": {"direction": "column"},
                    "children": [
                        {"type": "text", "props": {"text": "Packed 0 of 30"}},
                        each,
                    ],
                }
            ],
        },
    }


def _row() -> dict:
    return {
        "type": "checkbox",
        "props": {"label": "{item.name}"},
        "bind": "clothes.{index}.packed",
    }


def _each_of(spec: dict) -> dict:
    return spec["ui"]["children"][0]["children"][1]


def test_each_items_in_props_is_lifted_to_node_level():
    raw = _packing_spec(
        {"type": "each", "props": {"items": "{state.clothes}"}, "children": [_row()]}
    )
    before = copy.deepcopy(raw)

    each = _each_of(normalize_ripple_spec(raw))

    assert each["items"] == "{state.clothes}"
    assert "items" not in (each.get("props") or {})
    # The loop body survives untouched.
    child = each["children"][0]
    assert child["bind"] == "clothes.{index}.packed"
    assert child["props"]["label"] == "{item.name}"
    assert raw == before  # input not mutated


def test_item_as_and_index_as_in_props_are_lifted():
    raw = _packing_spec(
        {
            "type": "each",
            "props": {"items": "{state.clothes}", "item_as": "c", "index_as": "i"},
            "children": [_row()],
        }
    )

    each = _each_of(normalize_ripple_spec(raw))

    assert (each["items"], each["item_as"], each["index_as"]) == ("{state.clothes}", "c", "i")
    assert not {"items", "item_as", "index_as"} & set(each.get("props") or {})


def test_node_level_items_is_a_no_op():
    raw = _packing_spec({"type": "each", "items": "{state.clothes}", "children": [_row()]})

    each = _each_of(normalize_ripple_spec(raw))

    assert each["items"] == "{state.clothes}"
    assert "items" not in (each.get("props") or {})


def test_node_level_wins_when_both_present_and_props_copy_is_dropped():
    raw = _packing_spec(
        {
            "type": "each",
            "items": "{state.clothes}",
            "props": {"items": "{state.other}"},
            "children": [_row()],
        }
    )

    each = _each_of(normalize_ripple_spec(raw))

    assert each["items"] == "{state.clothes}"
    assert "items" not in (each.get("props") or {})


def test_props_items_on_a_non_each_node_is_left_alone():
    raw = _packing_spec({"type": "list", "props": {"items": [{"title": "a"}]}})

    node = _each_of(normalize_ripple_spec(raw))

    assert node["props"]["items"] == [{"title": "a"}]
    assert "items" not in node or node.get("items") is None


@pytest.mark.asyncio
async def test_chat_quality_line_flags_props_items_on_each(caplog):
    """On the raw shape the log-only quality line counts ``each.items`` as an
    unknown prop (the manifest's ``each`` declares no props)."""
    import json
    from pathlib import Path

    from pocketpaw_ee.cloud import ripple_validator

    manifest_path = Path(__file__).resolve().parents[2] / "src/pocketpaw/ripple/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = _packing_spec(
        {"type": "each", "props": {"items": "{state.clothes}"}, "children": [_row()]}
    )

    with (
        patch.object(ripple_validator, "get_manifest", AsyncMock(return_value=manifest)),
        caplog.at_level(logging.INFO, logger=ripple_validator.log.name),
    ):
        await ripple_validator.log_chat_spec_quality(raw, run_id="run-each")

    rec = next(r for r in caplog.records if r.getMessage() == "ripple_chat_spec.quality")
    assert "each.items" in rec.unknown_props
