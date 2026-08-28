# tests/cloud/other_hand/test_svg_to_ink.py — SVG becomes pen strokes.
#
# Created 2026-08-28. This is pure geometry with no I/O, so it is the one part
# of the vector-illustration feature that can be proven rather than eyeballed —
# which matters, because the failure mode is silent: a wrong transform does not
# raise, it just draws something subtly wrong on a page nobody is diffing.

from __future__ import annotations

import math

import pytest
from pocketpaw_ee.cloud.other_hand.svg_to_ink import (
    PAGE_W,
    Box,
    SvgConvertError,
    parse_path,
    svg_to_ops,
)


def wrap(body: str, viewbox: str = "0 0 100 100") -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{body}</svg>'


def all_points(ops: list[dict]) -> list[tuple[float, float]]:
    return [(p[0], p[1]) for op in ops for p in op["pts"]]


class TestFittingIntoTheBox:
    def test_a_square_drawing_is_not_stretched_into_a_wide_box(self):
        # The tell that something machine-processed a drawing. A 100x100 canvas
        # in a 800x200 box must scale by 2 (the height fit), not 8 by 2.
        ops = svg_to_ops(wrap('<rect x="0" y="0" width="100" height="100"/>'),
                         Box(x=100, y=100, w=800, h=200))
        xs = [p[0] for p in all_points(ops)]
        ys = [p[1] for p in all_points(ops)]
        assert math.isclose(max(xs) - min(xs), 200, abs_tol=1.5)
        assert math.isclose(max(ys) - min(ys), 200, abs_tol=1.5)

    def test_the_fitted_drawing_is_centred_in_its_box(self):
        ops = svg_to_ops(wrap('<rect x="0" y="0" width="100" height="100"/>'),
                         Box(x=100, y=100, w=800, h=200))
        xs = [p[0] for p in all_points(ops)]
        # Box spans 100..900; a 200-wide drawing centred sits at 400..600.
        assert math.isclose((max(xs) + min(xs)) / 2, 500, abs_tol=2)

    def test_a_viewbox_offset_does_not_shift_the_drawing_off_its_box(self):
        # A generator that emits viewBox="50 50 100 100" is describing the same
        # picture; forgetting to subtract the origin slides it by half a canvas.
        ops = svg_to_ops(
            wrap('<rect x="50" y="50" width="100" height="100"/>', viewbox="50 50 100 100"),
            Box(x=0, y=0, w=200, h=200),
        )
        xs = [p[0] for p in all_points(ops)]
        assert math.isclose(min(xs), 0, abs_tol=1.5)
        assert math.isclose(max(xs), 200, abs_tol=1.5)

    def test_a_zero_sized_box_is_refused_rather_than_dividing_by_zero(self):
        with pytest.raises(SvgConvertError):
            Box(x=0, y=0, w=0, h=100)


class TestPathGrammar:
    def test_relative_commands_accumulate_from_the_current_point(self):
        # "l" twice from (10,10) must land at (30,30), not at (20,20).
        sub = parse_path("M 10 10 l 10 10 l 10 10")
        assert sub[0][-1] == (30.0, 30.0)

    def test_h_and_v_move_only_their_own_axis(self):
        sub = parse_path("M 0 0 H 50 V 20")
        assert sub[0] == [(0.0, 0.0), (50.0, 0.0), (50.0, 20.0)]

    def test_z_closes_the_subpath_back_to_its_start(self):
        sub = parse_path("M 5 5 L 40 5 L 40 40 Z")
        assert sub[0][0] == sub[0][-1] == (5.0, 5.0)

    def test_a_curve_is_flattened_into_many_points_not_a_straight_line(self):
        sub = parse_path("M 0 0 C 0 100 100 100 100 0", scale_hint=4.0)
        assert len(sub[0]) > 6, "a bezier drawn as 2 points is a chord, not a curve"
        # A symmetric cubic bulges downward; its midpoint must leave the chord.
        mid = sub[0][len(sub[0]) // 2]
        assert mid[1] > 20

    def test_the_s_shorthand_reflects_the_previous_control_point(self):
        # Written out, and written with S, must describe the same curve.
        explicit = parse_path("M 0 0 C 0 50 50 50 50 0 C 50 -50 100 -50 100 0", scale_hint=4)
        short = parse_path("M 0 0 C 0 50 50 50 50 0 S 100 -50 100 0", scale_hint=4)
        assert math.isclose(explicit[0][-1][0], short[0][-1][0], abs_tol=0.01)
        assert math.isclose(min(p[1] for p in explicit[0]), min(p[1] for p in short[0]), abs_tol=2)

    def test_an_arc_bulges_instead_of_cutting_the_chord(self):
        sub = parse_path("M 0 50 A 50 50 0 0 1 100 50", scale_hint=4)
        pts = sub[0]
        assert len(pts) > 6
        # A sweep-1 arc from (0,50) to (100,50) passes near (50,0).
        assert min(p[1] for p in pts) < 10

    def test_an_unknown_command_loses_a_segment_not_the_drawing(self):
        sub = parse_path("M 0 0 L 10 10 B 5 5 L 20 20")
        assert sub and sub[0][0] == (0.0, 0.0)
        assert (20.0, 20.0) in sub[0]


class TestShapeElements:
    def test_a_circle_becomes_a_closed_ring_of_points(self):
        ops = svg_to_ops(wrap('<circle cx="50" cy="50" r="40"/>'), Box(x=0, y=0, w=100, h=100))
        pts = all_points(ops)
        assert len(pts) > 10
        # Every point sits on the ring, at the box's scale (40/100 * 100 = 40).
        for x, y in pts:
            assert math.isclose(math.hypot(x - 50, y - 50), 40, abs_tol=2)

    def test_a_polygon_is_closed_and_a_polyline_is_not(self):
        box = Box(x=0, y=0, w=100, h=100)
        poly = svg_to_ops(wrap('<polygon points="0,0 100,0 100,100"/>'), box)
        line = svg_to_ops(wrap('<polyline points="0,0 100,0 100,100"/>'), box)
        assert poly[0]["pts"][0] == poly[0]["pts"][-1]
        assert line[0]["pts"][0] != line[0]["pts"][-1]

    def test_elements_a_pen_cannot_draw_are_skipped_silently(self):
        body = (
            '<text x="10" y="10">hi</text>'
            '<image href="x.png"/>'
            '<line x1="0" y1="0" x2="9" y2="9"/>'
        )
        ops = svg_to_ops(wrap(body), Box(x=0, y=0, w=100, h=100))
        assert len(ops) == 1, "only the line is drawable"


class TestTheOutputIsSafeToRender:
    def test_every_op_is_a_valid_path_op(self):
        ops = svg_to_ops(wrap('<circle cx="50" cy="50" r="30"/>'), Box(x=0, y=0, w=200, h=200))
        assert ops
        for op in ops:
            assert op["t"] == "path"
            assert len(op["pts"]) >= 2
            assert all(len(p) == 2 for p in op["pts"])

    def test_no_coordinate_escapes_the_page_width(self):
        # The frontend validator drops a whole op if any point is out of range,
        # so an overflowing drawing would vanish rather than clip.
        ops = svg_to_ops(wrap('<rect x="-500" y="-500" width="2000" height="2000"/>'),
                         Box(x=0, y=0, w=PAGE_W, h=800))
        for x, _y in all_points(ops):
            assert 0 <= x <= PAGE_W

    def test_a_pathological_svg_is_capped_instead_of_hanging_the_renderer(self):
        from pocketpaw_ee.cloud.other_hand.svg_to_ink import MAX_POINTS

        body = "".join(f'<circle cx="{i % 100}" cy="{i % 100}" r="40"/>' for i in range(4000))
        ops = svg_to_ops(wrap(body), Box(x=0, y=0, w=1000, h=1000))
        assert sum(len(o["pts"]) for o in ops) <= MAX_POINTS

    def test_an_svg_with_nothing_drawable_returns_no_ops_rather_than_raising(self):
        # The turn should continue without the picture, not fail.
        assert svg_to_ops(wrap("<defs><linearGradient/></defs>"), Box(x=0, y=0, w=100, h=100)) == []

    def test_malformed_xml_is_a_clear_error_not_a_stack_trace(self):
        with pytest.raises(SvgConvertError, match="could not parse"):
            svg_to_ops("<svg><path d='M0 0", Box(x=0, y=0, w=100, h=100))
