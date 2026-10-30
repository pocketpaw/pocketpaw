# tests/cloud/surface/test_studio_craft_preamble.py — the three craft studio
# preambles (/studio/vector, /studio/photo, /studio/design) render the page's
# document projection and say what they must: print work for India, Indic text,
# units, the exact op names with one example each, the run_command block list,
# and failures from the last batch. Design says text colour is set_text_color
# (set_paint is the frame's box), that overset text disappears, and the browser's
# layout-check lines ("overset", "shrunk", "overlap", "off page frame <id>:").
# With no projection each one tells the agent there is nothing to edit. The cache
# key moves with the document.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.agent.mcp_servers.craft_ops import APPS, contract
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import studio_craft

VECTOR = {
    "title": "Sharma card",
    "colorMode": "Cmyk",
    "artboard": {"width": 252, "height": 144},
    "layers": [
        {
            "id": 1,
            "kind": "Layer",
            "name": "Layer 1",
            "bounds": {"x": 0, "y": 0, "width": 252, "height": 144},
            "children": [
                {
                    "id": 2,
                    "kind": "Rectangle",
                    "bounds": {"x": 0, "y": 0, "width": 252, "height": 144},
                    "fill": {"c": 0, "m": 0.6, "y": 1, "k": 0},
                    "stroke": "None",
                    "strokeWidth": 0,
                },
                {
                    "id": 3,
                    "kind": "Text",
                    "name": "नमस्ते",
                    "bounds": {"x": 18, "y": 24.5, "width": 53.75, "height": 32},
                    "fill": "#000000",
                    "text": "नमस्ते",
                },
            ],
        }
    ],
    "selection": [3],
    "last_edit": {"failures": ["ops[1] transform: nothing selected"]},
}
PHOTO = {
    "name": "ramesh.jpg",
    "width": 413,
    "height": 531,
    "ppi": 300,
    "colorMode": "RGB",
    "layers": [
        {"id": 1, "name": "Background", "kind": "Pixel", "visible": True, "opacity": 1},
        {"id": 4, "name": "Retouch", "kind": "Pixel", "visible": False, "opacity": 0.6},
    ],
    "activeLayer": 4,
    "selection": {"bounds": {"x": 10, "y": 20, "width": 300, "height": 400}},
    "last_edit": {"failures": ["ops[0] crop: crop box is outside the image"]},
}
DESIGN = {
    "title": "Visiting card",
    "size_mm": {"width": 90, "height": 54},
    "bleed_mm": 3,
    "current_page": 0,
    "pages": [
        {
            "index": 0,
            "frames": [
                {"id": 5, "kind": "rectangle", "fill": {"c": 0, "m": 0.6, "y": 1, "k": 0},
                 "bounds_mm": {"x": -3, "y": -3, "width": 96, "height": 18}},
                {"id": 7, "kind": "text", "text": "शर्मा प्रिंटर्स",
                 "bounds_mm": {"x": 8, "y": 8, "width": 74, "height": 12}},
            ],
        },
        {"index": 1, "frames": []},
    ],
    "selection": [7],
    "last_edit": {"failures": ["ops[2] add_text_frame: page 4 does not exist"]},
}  # fmt: skip
DOCS = {"vector": VECTOR, "photo": PHOTO, "design": DESIGN}
BUILDERS = {
    "vector": studio_craft.build_vector_preamble,
    "photo": studio_craft.build_photo_preamble,
    "design": studio_craft.build_design_preamble,
}


def _preamble(app: str, doc):
    meta = SurfaceMeta(route_path=f"/studio/{app}", **{app: doc})
    return asyncio.run(BUILDERS[app]("ws", "u", meta))


def _render(app: str, doc) -> str:
    return _preamble(app, doc).text


# ── vector ──────────────────────────────────────────────────────────────────


def test_vector_renders_the_projection() -> None:
    text = _render("vector", VECTOR)
    assert '<surface kind="studio_vector" route="/studio/vector" />' in text
    assert "artboard: 252 x 144 pt (3.50 x 2.00 in)" in text
    assert "colour mode: cmyk" in text
    assert "- Layer 1 (id=1, kind=Layer, bounds=0,0 252x144)" in text
    assert (
        "  - Rectangle (id=2, kind=Rectangle, bounds=0,0 252x144, "
        "fill=cmyk(0,0.6,1,0), stroke=none)" in text
    )
    assert "id=3" in text and "text='नमस्ते'" in text and "fill=#000000" in text
    assert "SELECTION: 3" in text
    assert "! ops[1] transform: nothing selected" in text


def test_vector_orients_for_indian_print_work() -> None:
    text = _render("vector", VECTOR)
    for needle in (
        "visiting cards", "wedding cards", "flex banners", "logos", "India",
        "Hindi", "72 pt = 1 inch", "y pointing DOWN", "CMYK",
        "mcp__pocketpaw_craft__edit_vector", "run_command", "file, export",
        "never claim it is done",
    ):  # fmt: skip
        assert needle in text, needle


def test_vector_large_trees_are_capped() -> None:
    kids = [{"id": i, "kind": "Path", "bounds": {}} for i in range(10, 200)]
    text = _render("vector", {**VECTOR, "layers": [{"id": 1, "kind": "Layer", "children": kids}]})
    assert "more layers" in text


# ── photo ───────────────────────────────────────────────────────────────────


def test_photo_renders_the_projection() -> None:
    text = _render("photo", PHOTO)
    assert '<surface kind="studio_photo" route="/studio/photo" />' in text
    assert "image: ramesh.jpg, 413 x 531 px at 300 ppi (35.0 x 45.0 mm)" in text
    assert "colour mode: rgb" in text
    assert "- Background (id=1, kind=Pixel, visible=yes, opacity=1)" in text
    assert "- Retouch (id=4, kind=Pixel, visible=no, opacity=0.6)" in text
    assert "ACTIVE LAYER: 4" in text
    assert "SELECTION: 10,20 300x400 px" in text
    assert "! ops[0] crop: crop box is outside the image" in text


def test_photo_orients_for_passport_and_retouch_work() -> None:
    text = _render("photo", PHOTO)
    for needle in (
        "PHOTO EDITOR", "passport", "India", "Hindi", "pixels", "y pointing DOWN",
        '"preset":"passport_35x45"', "413 x 531 px at 300 ppi", "ACTIVE LAYER",
        "mcp__pocketpaw_craft__edit_photo", "run_command", "file, export",
        "never claim it is done",
    ):  # fmt: skip
        assert needle in text, needle


def test_photo_without_a_selection() -> None:
    text = _render("photo", {**PHOTO, "selection": None, "activeLayer": None})
    assert "SELECTION: nothing" in text and "ACTIVE LAYER: none" in text


# ── design ──────────────────────────────────────────────────────────────────


def test_design_renders_the_projection() -> None:
    text = _render("design", DESIGN)
    assert '<surface kind="studio_design" route="/studio/design" />' in text
    assert "page size: 90 x 54 mm (trim), bleed 3 mm" in text
    assert "pages: 2; page in view: 0" in text
    assert "PAGE 0:" in text and "PAGE 1: (empty)" in text
    assert "  - rectangle (id=5, kind=rectangle, bounds=-3,-3 96x18, fill=cmyk(0,0.6,1,0))" in text
    assert "id=7" in text and "text='शर्मा प्रिंटर्स'" in text
    assert "SELECTION: 7" in text
    assert "! ops[2] add_text_frame: page 4 does not exist" in text


def test_design_orients_for_print_layout_in_mm() -> None:
    text = _render("design", DESIGN)
    for needle in (
        "PAGE LAYOUT EDITOR", "letterheads", "India", "Hindi", "MILLIMETRES",
        "TRIM top-left", "bleed", "0-based", "mcp__pocketpaw_craft__edit_design",
        "run_command", "file, export", "never claim it is done",
    ):  # fmt: skip
        assert needle in text, needle


def test_design_says_text_colour_is_not_the_frame_fill() -> None:
    # "Make the name red" painted a red box behind the name: set_paint read as text colour.
    text = _render("design", DESIGN)
    assert '{"op":"set_text_color"' in text
    assert "set_paint paints a frame's BOX" in text
    assert "never its text" in text
    assert "Fill, stroke and strokeWidth all go through set_paint" not in text


def test_design_warns_that_overset_text_disappears() -> None:
    # "Make the phone bigger": 18 pt in a frame that fits 13 pt, the number vanished, and the
    # reply claimed success.
    text = _render("design", DESIGN)
    for needle in (
        "OVERSET", "disappears", '"fit":true', "overset frame <id>:",
        "never tell the user it worked",
    ):  # fmt: skip
        assert needle in text, needle


def test_design_renders_an_overset_failure_from_the_browser() -> None:
    failure = (
        'overset frame 7: text does not fit its 74 x 12 mm box at 18 pt; hidden: "98400 12345"'
    )
    text = _render("design", {**DESIGN, "last_edit": {"failures": [failure]}})
    assert f"! {failure}" in text


def test_design_names_every_layout_check_line_and_never_claims_a_clean_layout() -> None:
    # Round 2: "make the phone 30 pt" ran the number over the address and off the card,
    # and the reply said it did not overlap, twice.
    text = _render("design", DESIGN)
    for needle in (
        "LAYOUT CHECK", "overset frame <id>:", "shrunk frame <id>:", "overlap frame <id>:",
        "off page frame <id>:", "Never tell the user text\n  fits",
        "answer from those failure lines only", "make room first",
    ):  # fmt: skip
        assert needle in text, needle


def test_design_renders_layout_check_lines_from_the_browser() -> None:
    failures = [
        "shrunk frame 42: text set at 13.3 pt, not 26 pt, so it fits its 53 x 6.2 mm box;"
        " make room around the frame for a bigger size",
        'overlap frame 42: its box now overlaps frame 44 (text "Main Bazaar") by 53 x 2.1 mm;'
        " move one of them or make the text smaller",
        "off page frame 44: its box runs 1.4 mm past the bottom trim edge, so that text is"
        " cut off when the page is trimmed",
    ]
    text = _render("design", {**DESIGN, "last_edit": {"failures": failures}})
    for failure in failures:
        assert f"! {failure}" in text


def test_design_marks_template_field_frames_and_never_deletes_them() -> None:
    # Round 3 (R3-m7): to make room the agent deleted the address field's frame and re-made it as
    # free text, which cut the user's fill-in box loose from the page.
    frame = {"id": 44, "kind": "text", "bounds_mm": {}, "text": "Main Bazaar", "field": "Address"}
    frames = [frame]
    text = _render("design", {**DESIGN, "pages": [{"index": 0, "frames": frames}]})
    assert "field='Address'" in text
    for needle in ("TEMPLATE FIELDS", "never delete or re-make", "move or resize it instead"):
        assert needle in text, needle


def test_design_reports_sizes_only_from_the_check_lines() -> None:
    # Round 3 (R3-m2): "I did not reduce the size" while fit had shrunk the heading 69 -> 46.5 pt.
    text = _render("design", DESIGN)
    for needle in ("Never state a text size", "shrunk line"):
        assert needle in text, needle


def test_design_large_documents_are_capped() -> None:
    frames = [{"id": i, "kind": "text", "bounds_mm": {}} for i in range(10, 200)]
    text = _render("design", {**DESIGN, "pages": [{"index": 0, "frames": frames}]})
    assert "more frames" in text


# ── all three ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("app", APPS)
def test_no_document(app) -> None:
    text = _render(app, None)
    assert f"No {app} document is open" in text
    assert f"<{app}-document>" not in text
    assert f'<surface kind="studio_{app}"' in text


@pytest.mark.parametrize("app", APPS)
def test_cache_key_moves_with_the_document(app) -> None:
    key_a = _preamble(app, DOCS[app]).cache_key
    key_b = _preamble(app, {**DOCS[app], "last_edit": None}).cache_key
    assert key_a != key_b


@pytest.mark.parametrize("app", APPS)
def test_preamble_lists_every_op_with_an_example(app) -> None:
    text = _render(app, DOCS[app])
    for op in contract(app).kinds:
        assert f'{{"op":"{op}"' in text, op
    assert "set_fill" not in text and "{ops}" not in text


@pytest.mark.parametrize("app", APPS)
def test_registry_routes_each_studio_to_its_preamble(app) -> None:
    from pocketpaw_ee.cloud.surface.domain import SurfaceKind
    from pocketpaw_ee.cloud.surface.surface_registry import SURFACES

    spec = next(s for s in SURFACES if s.kind is SurfaceKind(f"studio_{app}"))
    assert spec.build_preamble is BUILDERS[app]
