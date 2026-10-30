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
        ops = svg_to_ops(
            wrap('<rect x="0" y="0" width="100" height="100"/>'), Box(x=100, y=100, w=800, h=200)
        )
        xs = [p[0] for p in all_points(ops)]
        ys = [p[1] for p in all_points(ops)]
        assert math.isclose(max(xs) - min(xs), 200, abs_tol=1.5)
        assert math.isclose(max(ys) - min(ys), 200, abs_tol=1.5)

    def test_the_fitted_drawing_is_centred_in_its_box(self):
        ops = svg_to_ops(
            wrap('<rect x="0" y="0" width="100" height="100"/>'), Box(x=100, y=100, w=800, h=200)
        )
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
            '<text x="10" y="10">hi</text><image href="x.png"/><line x1="0" y1="0" x2="9" y2="9"/>'
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
        ops = svg_to_ops(
            wrap('<rect x="-500" y="-500" width="2000" height="2000"/>'),
            Box(x=0, y=0, w=PAGE_W, h=800),
        )
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


class TestTheBugsARealGenerationFound:
    """Written after the first live Recraft call, which returned 653 paths and
    converted to 17 points. Every test here failed before its fix."""

    def test_a_mid_sized_curve_does_not_crash_the_flattener(self):
        # THE bug. The segment count was `int(...) ** 0.5 * 2`, which is a
        # FLOAT, and a float reaching range() raises TypeError. The existing
        # tests missed it because their curves were tiny or huge, so max()/min()
        # returned an int bound either way; only a mid-sized curve — which is
        # every curve in a real illustration — produced the float.
        sub = parse_path("M 100 100 C 140 60, 220 60, 260 100", scale_hint=1.0)
        assert sub and len(sub[0]) > 3

    @pytest.mark.parametrize("scale", [0.05, 0.37, 1.0, 3.3, 12.0])
    def test_the_flattener_survives_every_plausible_scale(self, scale):
        # The float only appeared at some scales, so pin a spread rather than
        # one lucky value.
        sub = parse_path("M 0 0 C 30 40, 90 40, 120 0", scale_hint=scale)
        assert sub and len(sub[0]) >= 2

    def test_a_shape_level_bug_is_not_silently_swallowed(self):
        # The second half of the same incident: _shapes caught TypeError, so the
        # crash above was swallowed for 650 of 653 paths and the drawing came
        # back looking merely sparse. A TypeError is OUR bug and must escape.
        import pocketpaw_ee.cloud.other_hand.svg_to_ink as m

        def _boom(*_a, **_k):
            raise TypeError("a bug inside the converter")

        original = m.parse_path
        m.parse_path = _boom
        try:
            with pytest.raises(TypeError):
                svg_to_ops(wrap('<path d="M0 0 L10 10"/>'), Box(x=0, y=0, w=100, h=100))
        finally:
            m.parse_path = original

    def test_a_pale_fill_is_treated_as_paper_not_ink(self):
        # Recraft returns FILLED art. Its near-white paths are highlights that
        # mean nothing as bare outlines and only spend the point budget.
        pale = svg_to_ops(
            wrap('<path fill="rgb(254,253,253)" d="M10 10 L90 10 L90 90 Z"/>'),
            Box(x=0, y=0, w=100, h=100),
        )
        dark = svg_to_ops(
            wrap('<path fill="rgb(25,21,22)" d="M10 10 L90 10 L90 90 Z"/>'),
            Box(x=0, y=0, w=100, h=100),
        )
        assert pale == []
        assert dark

    def test_a_nearly_transparent_shape_is_dropped(self):
        ops = svg_to_ops(
            wrap('<path fill="rgb(0,0,0)" fill-opacity="0.1" d="M10 10 L90 10 L90 90 Z"/>'),
            Box(x=0, y=0, w=100, h=100),
        )
        assert ops == []

    def test_an_unparseable_fill_counts_as_ink(self):
        # Conservative on purpose: dropping a real shape is worse than keeping
        # a faint one.
        ops = svg_to_ops(
            wrap('<path fill="url(#grad)" d="M10 10 L90 10 L90 90 Z"/>'),
            Box(x=0, y=0, w=100, h=100),
        )
        assert ops

    def test_an_oversized_drawing_is_thinned_whole_not_truncated(self):
        # The design reversal. Dropping the tail kept 30% of a real bee — not a
        # simpler bee, a third of one. Simplification must keep MOST shapes.
        from pocketpaw_ee.cloud.other_hand.svg_to_ink import MAX_POINTS

        def one(i: int) -> str:
            x = i % 90
            return f'<path fill="rgb(0,0,0)" d="M{x} 5 C {x + 3} 40, {x + 6} 40, {x + 9} 5"/>'

        # 2500 curves is comfortably OVER the budget — checked, because an
        # under-budget fixture would skip the thinning loop entirely and the
        # assertions below would pass without ever exercising it. (That is
        # exactly what the first version of this test did; the mutation harness
        # caught it by removing the loop and watching the test still pass.)
        shapes = 2500
        body = "".join(one(i) for i in range(shapes))
        ops = svg_to_ops(wrap(body), Box(x=0, y=0, w=1000, h=1000))
        assert sum(len(o["pts"]) for o in ops) <= MAX_POINTS
        # Truncation would keep roughly MAX_POINTS/11 shapes — about half.
        # Thinning keeps nearly all of them, which is the whole point.
        assert len(ops) > shapes * 0.9, "the budget truncated the drawing instead of thinning it"
