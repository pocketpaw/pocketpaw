# test_craft_mcp.py — the craft studios' op contracts, the one validator and the
# edit_<app> tools.
#
# Covers, for vector, photo and design: the contract file loads and the
# validator is driven by it; good batches pass and come back normalised;
# unknown ops, unknown params, bad types, invented ids, blocked run_command ids
# and over-cap batches are refused with a message naming the index; oneOf
# groups (crop, new_document); design's text colour is its own op
# (set_text_color, set_paint is the frame's box) and set_text can grow its frame
# to fit (fit); every op's preamble example validates; the tool
# returns the ``craft_edit`` envelope {app, ops} and run_core promotes it; each
# tool refuses when its app's projection is not bound; each STUDIO_<APP>
# profile exposes exactly its own tool; through the real SDK server each tool
# reaches its own app and types `ops` as an array; the provider is registered.
#
# Mutation plan: tests/mutations/craft_ops.json.

from __future__ import annotations

import asyncio
import json
import re

import pytest
from pocketpaw_ee.agent.mcp_servers import craft_ops
from pocketpaw_ee.agent.mcp_servers.craft import (
    CRAFT_TOOL_IDS,
    SERVER_NAME,
    TOOL_IDS,
    _edit_handler,
    tool_description,
)
from pocketpaw_ee.agent.mcp_servers.craft_ops import (
    APPS,
    OP_EXAMPLES,
    CraftSummary,
    command_error,
    contract,
    op_cheatsheet,
    paint_error,
    validate_ops,
)

VECTOR = {
    "colorMode": "cmyk",
    "artboard": {"width": 252, "height": 144},
    "layers": [
        {
            "id": 1,
            "kind": "Layer",
            "children": [
                {"id": 2, "kind": "Rectangle", "fill": "#ffffff"},
                {"id": 3, "kind": "Text", "text": "नमस्ते"},
            ],
        }
    ],
    "selection": [2],
}
PHOTO = {
    "width": 1008,
    "height": 576,
    "ppi": 300,
    "colorMode": "rgb",
    "layers": [
        {"id": 1, "name": "Background", "visible": True, "opacity": 1, "kind": "Pixel"},
        {"id": 2, "name": "Retouch", "visible": True, "opacity": 1, "kind": "Pixel"},
        {"id": 3, "name": "Text", "visible": True, "opacity": 1, "kind": "Type"},
    ],
    "activeLayer": 2,
    "selection": None,
    "last_edit": {"failures": []},
}
DESIGN = {
    "size_mm": {"width": 90, "height": 54},
    "bleed_mm": 3,
    "current_page": 0,
    "pages": [
        {
            "index": 0,
            "frames": [
                {"id": 5, "kind": "rectangle", "bounds_mm": {"x": -3, "y": -3, "width": 96,
                                                             "height": 18}},
                {"id": 7, "kind": "text", "bounds_mm": {"x": 8, "y": 8, "width": 74,
                                                        "height": 12}, "text": "Sharma"},
            ],
        },
        {"index": 1, "frames": []},
    ],
    "selection": [7],
}  # fmt: skip
DOCS = {"vector": VECTOR, "photo": PHOTO, "design": DESIGN}
SUMMARY = CraftSummary.from_meta(VECTOR)
CMYK = {"c": 0, "m": 0.6, "y": 1, "k": 0}


def _ok(ops, summary=SUMMARY, app="vector"):
    clean, err = validate_ops(app, ops, summary)
    assert err is None, err
    return clean


def _err(ops, summary=SUMMARY, app="vector") -> str:
    clean, err = validate_ops(app, ops, summary)
    assert clean is None and err
    return err


def _app_ok(app, ops):
    return _ok(ops, CraftSummary.from_meta(DOCS[app]), app)


def _app_err(app, ops) -> str:
    return _err(ops, CraftSummary.from_meta(DOCS[app]), app)


# ── contracts ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app", APPS)
def test_contract_loads_and_every_type_is_known(app) -> None:
    raw = json.loads(craft_ops.contract_path(app).read_text(encoding="utf-8"))
    c = contract(app)
    assert raw["version"] == 1 and set(raw["ops"]) == c.kinds
    assert c.max_ops == raw["maxOpsPerBatch"] == 50
    assert "run_command" in c.kinds
    for spec in raw["ops"].values():
        for ptype in spec["params"].values():
            if ptype not in ("ids", "id"):
                craft_ops._type_error(ptype, None, app)  # raises on an unknown type
        for group in spec.get("oneOf", []):
            assert set(group) <= set(spec["params"])


def test_vector_vocabulary_is_pinned() -> None:
    assert contract("vector").kinds == {
        "draw_shape", "draw_path", "add_text", "set_paint", "transform", "align",
        "arrange", "group", "ungroup", "delete", "set_text", "apply_effect",
        "pathfinder", "new_document", "set_artboard", "run_command",
    }  # fmt: skip


def test_photo_and_design_vocabularies() -> None:
    assert contract("photo").kinds == {
        "adjust_levels", "adjust_curves", "brightness_contrast", "hue_saturation", "sharpen",
        "auto_tone", "crop", "resize", "rotate", "flip", "select_subject", "select_all",
        "deselect", "fill_selection", "add_text", "new_layer", "delete_layer",
        "set_layer_opacity", "set_layer_visibility", "rename_layer", "run_command",
    }  # fmt: skip
    assert contract("design").kinds == {
        "new_document", "add_page", "delete_page", "add_text_frame", "add_shape", "set_text",
        "set_text_color", "set_paint", "transform", "align", "arrange", "delete", "set_bleed",
        "run_command",
    }  # fmt: skip


def test_photo_and_design_share_a_run_command_block_stricter_than_vector() -> None:
    v, p, d = (contract(a).raw["runCommand"] for a in APPS)
    assert p == d
    assert p["pattern"] == v["pattern"]
    assert set(p["deniedNamespaces"]) >= set(v["deniedNamespaces"])
    for seg in v["deniedSegmentPattern"][2:-2].split("|"):
        assert re.match(p["deniedSegmentPattern"], seg), seg


# ── vector (every P1 case, now through the generic validator) ───────────────


def test_summary_flattens_the_layer_tree() -> None:
    assert SUMMARY.has_document and SUMMARY.ids == {1, 2, 3}
    assert CraftSummary.from_meta(None).has_document is False


def test_summary_collects_photo_and_design_ids() -> None:
    assert CraftSummary.from_meta(PHOTO).ids == {1, 2, 3}
    assert CraftSummary.from_meta(PHOTO).selection == []
    design = CraftSummary.from_meta(DESIGN)
    assert design.ids == {5, 7} and design.selection == [7]


def test_a_visiting_card_batch_passes() -> None:
    clean = _ok(
        [
            {"op": "new_document", "width": 252, "height": 144, "colorMode": "cmyk"},
            {"op": "draw_shape", "shape": "rectangle", "x": 0, "y": 0, "width": 252,
             "height": 144, "radius": 6, "fill": CMYK, "stroke": "none"},
            {"op": "transform", "dx": 4},
            {"op": "add_text", "text": "शर्मा प्रिंटर्स", "x": 18, "y": 40, "size": 18,
             "color": {"c": 0, "m": 0, "y": 0, "k": 1}, "align": "left"},
            {"op": "draw_path", "points": [[10, 10], {"x": 20, "y": 30}], "closed": False},
            {"op": "run_command", "command": "object.blend.make", "params": {"steps": 5}},
        ]
    )  # fmt: skip
    assert len(clean) == 6


def test_ids_normalise_to_ints_and_nulls_drop() -> None:
    clean = _ok([{"op": "set_paint", "ids": ["2", 3], "fill": "#ff0000", "stroke": None}])
    assert clean == [{"op": "set_paint", "ids": [2, 3], "fill": "#ff0000"}]
    assert _ok([{"op": "set_text", "id": "3", "text": "x"}])[0]["id"] == 3


@pytest.mark.parametrize(
    ("ops", "needle"),
    [
        ([{"op": "draw_rect"}], "not a vector operation"),
        ([{"op": "draw_shape", "shape": "rectangle", "x": 0, "y": 0, "width": 1}], "'height'"),
        ([{"op": "draw_shape", "shape": "hexagon"}], "must be one of"),
        ([{"op": "draw_path", "closed": True}], "at least one of: d, points"),
        ([{"op": "add_text", "text": "hi", "x": 1, "y": 2, "colour": "#000000"}], "no param"),
        ([{"op": "add_text", "text": "", "x": 1, "y": 2}], "non-empty"),
        ([{"op": "set_paint", "fill": "red"}], "must be a paint"),
        ([{"op": "set_paint", "fill": {"c": 2, "m": 0, "y": 0, "k": 0}}], "must be a paint"),
        ([{"op": "transform", "scale": True}], "must be a number"),
        ([{"op": "transform", "ids": [2]}], "at least one of"),
        ([{"op": "delete", "ids": [99]}], "99 is not in the document"),
        ([{"op": "delete", "ids": []}], "non-empty list"),
        ([{"op": "group", "ids": [True]}], "not an id"),
        ([{"op": "align", "align": "middle", "to": "page"}], "must be one of"),
        ([{"op": "apply_effect", "effect": "../x"}], "effect id"),
        ([{"op": "new_document", "width": 0, "height": 144}], "> 0"),
        ([{"op": "new_document", "width": 9, "height": 9}, {"op": "delete", "ids": [2]}],
         "replaced the document"),
        ([], "empty"),
        ("nope", "must be a list"),
    ],
)  # fmt: skip
def test_bad_batches_are_refused(ops, needle) -> None:
    assert needle in _err(ops)


def test_errors_name_the_index() -> None:
    assert _err([{"op": "transform", "dx": 1}, {"op": "bogus"}]).startswith("ops[1]")


def test_no_selection_needs_ids_until_something_is_created() -> None:
    empty = CraftSummary.from_meta({**VECTOR, "selection": []})
    assert "nothing is selected" in _err([{"op": "set_paint", "fill": "none"}], empty)
    _ok(
        [
            {"op": "draw_shape", "shape": "ellipse", "x": 0, "y": 0, "width": 5, "height": 5},
            {"op": "set_paint", "fill": "none"},
        ],
        empty,
    )


@pytest.mark.parametrize("app", APPS)
def test_no_document_refuses(app) -> None:
    err = _err([{"op": "run_command", "command": "edit.undo"}], CraftSummary(False), app)
    assert f"No {app} document is open" in err and f"/studio/{app}" in err


@pytest.mark.parametrize("app", APPS)
def test_batch_cap(app) -> None:
    too_many = [{"op": "run_command", "command": "edit.undo"}] * (contract(app).max_ops + 1)
    assert "maximum" in _app_err(app, too_many)


_BLOCKED_EVERYWHERE = [
    "file.open", "file.save", "file.export", "document.export", "document.save",
    "document.open", "app.quit", "prefs.set", "clipboard.importSvg", "command.batch",
    "color.loadProfile", "view.zoom", "tool.select", "help.github", "object.export",
    "window.close", "FILE.open", "file", "object..align", "object.align; rm",
]  # fmt: skip


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("command", _BLOCKED_EVERYWHERE)
def test_run_command_blocks_file_io_and_app_commands(app, command) -> None:
    assert command_error(command, app) is not None
    assert "ops[0].command" in _app_err(app, [{"op": "run_command", "command": command}])


@pytest.mark.parametrize(
    "command",
    [
        "edit.preferences.general", "edit.keyboardShortcuts", "layer.exportAs",
        "layer.quickExportAsPng", "layer.smartObjects.saveContents",
        "layer.smartObjects.relinkToFile", "book.exportPdf", "links.relinkFolder",
        "snippet.export", "place.load",
    ],
)  # fmt: skip
def test_photo_and_design_block_their_own_io_commands(command) -> None:
    assert command_error(command, "photo") is not None
    assert command_error(command, "design") is not None


@pytest.mark.parametrize(
    ("app", "command"),
    [
        ("vector", "object.align"), ("vector", "object.arrange.bringToFront"),
        ("vector", "edit.colors.toCMYK"), ("vector", "shape.star"),
        ("photo", "select.inverse"), ("photo", "filter.noise.reduceNoise"),
        ("photo", "select.refineEdge"), ("photo", "layer.newFillLayer.solidColor"),
        ("design", "object.qrCode"), ("design", "edit.stepAndRepeat"),
        ("design", "data.merge"), ("design", "preflight.run"),
    ],
)  # fmt: skip
def test_run_command_allows_engine_commands(app, command) -> None:
    assert command_error(command, app) is None
    _app_ok(app, [{"op": "run_command", "command": command}])


def test_paints() -> None:
    for good in ("none", "#1e88e5", [0, 0.5, 1], CMYK, {"gray": 0.2}, {"swatch": "Gold"}):
        assert paint_error(good) is None, good
    for bad in ("#fff", "blue", [0, 0, 2], {"c": 0, "m": 0, "y": 0}, {"gray": True}, 5):
        assert paint_error(bad) is not None, bad


# ── photo ───────────────────────────────────────────────────────────────────


def test_a_passport_photo_batch_passes() -> None:
    clean = _app_ok(
        "photo",
        [
            {"op": "auto_tone"},
            {"op": "crop", "preset": "passport_35x45"},
            {"op": "select_subject", "mode": "replace"},
            {"op": "run_command", "command": "select.inverse"},
            {"op": "fill_selection", "color": "#ffffff"},
            {"op": "deselect"},
            {"op": "adjust_levels", "inBlack": 10, "inWhite": 245},
            {"op": "adjust_curves", "points": [[0, 0], [128, 140], [255, 255]]},
            {"op": "sharpen", "amount": 40},
            {"op": "set_layer_opacity", "layer": "2", "opacity": 0.5},
            {"op": "rotate", "by": "90cw"},
        ],
    )
    assert clean[9] == {"op": "set_layer_opacity", "layer": 2, "opacity": 0.5}


@pytest.mark.parametrize(
    ("ops", "needle"),
    [
        ([{"op": "levels"}], "not a photo operation"),
        ([{"op": "adjust_levels", "gamma": 20}], "from 0.1 to 10"),
        ([{"op": "adjust_levels"}], "at least one of"),
        ([{"op": "adjust_curves", "points": [[0, 0]]}], "[in, out] pairs"),
        ([{"op": "adjust_curves", "points": [[0, 0], [300, 1]]}], "[in, out] pairs"),
        ([{"op": "brightness_contrast", "brightness": 200}], "from -150 to 150"),
        ([{"op": "crop", "x": 0, "y": 0, "width": 10}], "exactly one of"),
        ([{"op": "crop", "preset": "passport_35x45", "x": 4}], "drop x"),
        ([{"op": "crop", "preset": "a4"}], "must be one of"),
        ([{"op": "rotate", "by": 90}], "must be one of"),
        ([{"op": "fill_selection", "color": "white"}], '"#rrggbb"'),
        ([{"op": "fill_selection", "color": {"c": 0, "m": 0, "y": 0, "k": 0}}], '"#rrggbb"'),
        ([{"op": "set_layer_opacity", "opacity": 50}], "from 0 to 1"),
        ([{"op": "delete_layer", "layer": 9}], "9 is not in the document"),
        ([{"op": "rename_layer", "layer": 2}], "missing required param 'name'"),
        ([{"op": "auto_tone", "strength": 1}], "Allowed: none"),
    ],
)  # fmt: skip
def test_bad_photo_batches_are_refused(ops, needle) -> None:
    assert needle in _app_err("photo", ops)


# ── design ──────────────────────────────────────────────────────────────────


def test_a_visiting_card_layout_passes() -> None:
    clean = _app_ok(
        "design",
        [
            {"op": "set_paint", "ids": [5], "fill": CMYK},
            {"op": "set_text", "id": "7", "text": "शर्मा प्रिंटर्स", "align": "center"},
            {"op": "add_text_frame", "page": 0, "x": 8, "y": 30, "width": 74, "height": 8,
             "text": "+91 98765 43210", "size": 9, "color": {"c": 0, "m": 0, "y": 0, "k": 1}},
            {"op": "align", "align": "center", "to": "page"},
            {"op": "add_shape", "shape": "line", "x1": 8, "y1": 26, "x2": 82, "y2": 26,
             "stroke": "#000000", "strokeWidth": 0.5},
            {"op": "add_page", "after": 0},
            {"op": "set_bleed", "bleed": 3},
        ],
    )  # fmt: skip
    assert clean[1]["id"] == 7


def test_design_new_document_then_build_on_it() -> None:
    _app_ok(
        "design",
        [
            {"op": "new_document", "size": "A5", "bleed": 3, "pages": 1},
            {"op": "add_shape", "shape": "rectangle", "x": -3, "y": -3, "width": 154,
             "height": 40, "fill": CMYK},
            {"op": "transform", "dy": 2},
        ],
    )  # fmt: skip
    _app_ok("design", [{"op": "new_document", "width": 210, "height": 99}])


@pytest.mark.parametrize(
    ("ops", "needle"),
    [
        ([{"op": "add_text"}], "not a design operation"),
        ([{"op": "add_text_frame", "text": "x", "x": 0, "y": 0, "width": 10}], "'height'"),
        ([{"op": "add_text_frame", "text": "x", "x": 0, "y": 0, "width": 10, "height": 5,
           "page": -1}], ">= 0"),
        ([{"op": "add_shape", "shape": "line", "x1": 0, "y1": 0, "x2": 5}], "'y2'"),
        ([{"op": "new_document", "size": "A4", "width": 210, "height": 297}], "drop height, width"),
        ([{"op": "new_document", "bleed": 3}], "exactly one of"),
        ([{"op": "new_document", "size": "A4", "pages": 0}], ">= 1"),
        ([{"op": "set_text", "id": 7}], "at least one of"),
        ([{"op": "set_paint", "ids": [6], "fill": "none"}], "6 is not in the document"),
        ([{"op": "align", "align": "center", "to": "artboard"}], "must be one of"),
        ([{"op": "new_document", "size": "A4"}, {"op": "delete", "ids": [5]}],
         "replaced the document"),
        ([{"op": "set_bleed"}], "missing required param 'bleed'"),
    ],
)  # fmt: skip
def test_bad_design_batches_are_refused(ops, needle) -> None:
    assert needle in _app_err("design", ops)


def test_design_text_colour_is_its_own_op() -> None:
    # "Make the name red" painted a red box behind the name (set_paint fill on the text
    # frame). Text colour has its own verb, and set_paint's engine line says it is the box.
    red = {"c": 0, "m": 1, "y": 1, "k": 0}
    clean = _app_ok("design", [{"op": "set_text_color", "ids": ["7"], "color": red}])
    assert clean[0] == {"op": "set_text_color", "ids": [7], "color": red}
    _app_ok("design", [{"op": "set_text_color", "color": "#c00000"}])  # the selection
    assert "missing required param 'color'" in _app_err(
        "design", [{"op": "set_text_color", "ids": [7]}]
    )
    ops = contract("design").ops
    assert "type.char" in ops["set_text_color"]["engine"]
    assert "never the text" in ops["set_paint"]["engine"]
    desc = tool_description("design")
    assert "never its text" in desc and "set_text_color" in desc


def test_design_set_text_can_grow_its_frame_to_fit() -> None:
    # "Make the phone bigger" set 18 pt in a 13 pt frame and the number vanished (overset).
    _app_ok("design", [{"op": "set_text", "id": 7, "size": 18, "fit": True}])
    _app_ok("design", [{"op": "set_text", "id": 7, "fit": True}])  # fix a reported overset
    assert "true or false" in _app_err("design", [{"op": "set_text", "id": 7, "fit": "yes"}])
    assert "overset" in contract("design").ops["set_text"]["engine"]


def test_design_no_selection_needs_ids_until_a_frame_is_created() -> None:
    empty = CraftSummary.from_meta({**DESIGN, "selection": []})
    assert "nothing is selected" in _err([{"op": "set_paint", "fill": "none"}], empty, "design")
    _ok(
        [
            {"op": "add_shape", "shape": "ellipse", "x": 0, "y": 0, "width": 5, "height": 5},
            {"op": "set_paint", "fill": "none"},
        ],
        empty,
        "design",
    )


# ── the examples every preamble lists ───────────────────────────────────────


@pytest.mark.parametrize("app", APPS)
def test_every_op_has_one_valid_example(app) -> None:
    assert set(OP_EXAMPLES[app]) == contract(app).kinds
    for op, example in OP_EXAMPLES[app].items():
        assert example["op"] == op
        _app_ok(app, [example])
    sheet = op_cheatsheet(app)
    assert sheet.count("\n") == len(contract(app).kinds) - 1


def test_vector_cheatsheet_spells_set_paint() -> None:
    sheet = op_cheatsheet("vector")
    assert '"op":"set_paint"' in sheet and '"strokeWidth":0' in sheet


# ── the tools ───────────────────────────────────────────────────────────────


def _call(args: dict, app="vector", doc: object = "default") -> dict:
    from pocketpaw_ee.cloud.chat.agent_service import bind_craft, unbind_craft

    doc = DOCS[app] if doc == "default" else doc
    token = bind_craft(None if doc is None else {app: doc})
    try:
        return asyncio.run(_edit_handler(app, args))
    finally:
        unbind_craft(token)


def _body(result: dict) -> dict:
    assert not result.get("is_error"), result
    return json.loads(result["content"][0]["text"])


def test_tool_returns_the_craft_edit_envelope() -> None:
    body = _body(_call({"ops": [{"op": "set_paint", "ids": ["2"], "fill": CMYK}]}))
    assert body["ok"] is True and body["dispatched"] == 1
    assert body["craft_edit"] == {
        "app": "vector",
        "ops": [{"op": "set_paint", "ids": [2], "fill": CMYK}],
    }
    assert "vector_edit" not in body
    assert "applied" in body["note"]


@pytest.mark.parametrize(
    ("app", "op"),
    [
        ("photo", {"op": "crop", "preset": "passport_35x45"}),
        ("design", {"op": "set_text", "id": 7, "text": "Sharma"}),
    ],
)
def test_photo_and_design_tools_return_their_app_in_the_envelope(app, op) -> None:
    assert _body(_call({"ops": [op]}, app))["craft_edit"] == {"app": app, "ops": [op]}


def test_tool_accepts_ops_as_a_json_string() -> None:
    result = _call({"ops": json.dumps([{"op": "transform", "dx": 3}])})
    assert _body(result)["dispatched"] == 1


def test_tool_reports_validation_errors() -> None:
    result = _call({"ops": [{"op": "run_command", "command": "file.save"}]})
    assert result["is_error"] is True
    assert result["content"][0]["text"].startswith("Error: ops[0].command")


@pytest.mark.parametrize("app", APPS)
def test_tool_refuses_off_surface(app) -> None:
    result = _call({"ops": [{"op": "run_command", "command": "edit.undo"}]}, app, doc=None)
    assert result["is_error"] is True
    assert f"No {app} document" in result["content"][0]["text"]


def test_a_tool_ignores_another_apps_projection() -> None:
    from pocketpaw_ee.cloud.chat.agent_service import bind_craft, unbind_craft

    token = bind_craft({"vector": VECTOR})
    try:
        result = asyncio.run(_edit_handler("photo", {"ops": [{"op": "auto_tone"}]}))
    finally:
        unbind_craft(token)
    assert result["is_error"] is True and "No photo document" in result["content"][0]["text"]


def test_run_core_promotes_the_envelope() -> None:
    from pocketpaw_ee.cloud.chat.runs.run_core import _EDITOR_ENVELOPE_MARKERS, _timeline_payload

    assert "craft_edit" in _EDITOR_ENVELOPE_MARKERS
    assert "vector_edit" not in _EDITOR_ENVELOPE_MARKERS

    op = {"op": "add_text", "text": "{शादी}", "x": 1, "y": 2}
    text = _call({"ops": [op]})["content"][0]["text"]
    assert _timeline_payload(text, "craft_edit") == {"app": "vector", "ops": [op]}


def test_run_core_binds_every_craft_projection() -> None:
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.chat.runs.run_core import _craft_from_ctx
    from pocketpaw_ee.cloud.surface.domain import SurfaceMeta

    def ctx(**meta):
        return SimpleNamespace(surface_context=SimpleNamespace(meta=SurfaceMeta(**meta)))

    assert _craft_from_ctx(ctx(photo=PHOTO)) == {"photo": PHOTO}
    assert _craft_from_ctx(ctx(design=DESIGN, vector=VECTOR)) == {"design": DESIGN,
                                                                  "vector": VECTOR}  # fmt: skip
    assert _craft_from_ctx(ctx()) is None
    assert _craft_from_ctx(SimpleNamespace(surface_context=None)) is None


@pytest.mark.parametrize(
    ("kind", "app"),
    [("STUDIO_VECTOR", "vector"), ("STUDIO_PHOTO", "photo"), ("STUDIO_DESIGN", "design")],
)
def test_each_studio_surface_exposes_only_its_own_tool(kind, app) -> None:
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
    from pocketpaw_ee.cloud.surface.surface_registry import SURFACES

    spec = next(s for s in SURFACES if s.kind is SurfaceKind[kind])
    assert spec.kind.value == f"studio_{app}"
    profile = spec.profile_resolver(SurfaceMeta())
    assert profile.ripple_mode == "off"
    assert set(profile.allow_mcp_tool_ids) == {TOOL_IDS[app]}
    assert TOOL_IDS[app] == f"mcp__{SERVER_NAME}__edit_{app}"


def test_each_registered_tool_reaches_its_own_app() -> None:
    """Through the real SDK server: the per-app closure must not share one app."""
    pytest.importorskip("claude_agent_sdk")
    import mcp.types as types
    from pocketpaw_ee.agent.mcp_servers.craft import build_craft_server
    from pocketpaw_ee.cloud.chat.agent_service import bind_craft, unbind_craft

    _, server = build_craft_server()
    listed = asyncio.run(
        server["instance"].request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
    ).root.tools
    for tool in listed:  # the SDK shorthand would have collapsed ops to a bare string
        ops = tool.inputSchema["properties"]["ops"]
        assert ops["type"] == "array" and "description" in ops, tool.name
    call = server["instance"].request_handlers[types.CallToolRequest]
    token = bind_craft(DOCS)
    try:
        for app, op in (("photo", {"op": "auto_tone"}), ("design", {"op": "add_page"})):
            req = types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name=f"edit_{app}", arguments={"ops": [op]}),
            )
            text = asyncio.run(call(req)).root.content[0].text
            assert json.loads(text)["craft_edit"] == {"app": app, "ops": [op]}
    finally:
        unbind_craft(token)


def test_one_server_hosts_three_tools() -> None:
    assert SERVER_NAME == "pocketpaw_craft"
    assert CRAFT_TOOL_IDS == tuple(f"mcp__pocketpaw_craft__edit_{a}" for a in APPS)


def test_provider_is_registered() -> None:
    from pocketpaw._registry import providers

    by_name = {type(p).__name__: p for p in providers("pocketpaw.mcp_servers")}
    assert "CloudCraftMcpProvider" in by_name
    assert by_name["CloudCraftMcpProvider"].tool_ids() == list(CRAFT_TOOL_IDS)


@pytest.mark.parametrize("app", APPS)
def test_tool_description_names_every_op(app) -> None:
    desc = tool_description(app)
    listed = desc.split("op names are exactly: ")[1].split(". ")[0]
    assert set(listed.split(", ")) == contract(app).kinds
    assert f"/studio/{app}" in desc


def test_vector_description_rules_out_the_guessed_names() -> None:
    assert "no set_fill" in tool_description("vector")
