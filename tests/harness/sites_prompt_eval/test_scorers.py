"""Prove every SD-6 scorer separates a clean page from the defect it names.

New file 2026-09-08 (feat/sites-design-skills).

WHY THIS FILE EXISTS. A scorer that returns pass on a defective page is worse than
having no eval: it certifies a regression and it does so with a number beside it.
This repo's rule is that a gate is not a gate until a mutation has been observed to
break it, and here the gate IS the measuring instrument, so the mutation is a
hand-written defective page.

Every scorer gets two controls — a page that should pass and a page that carries
exactly that one defect — plus, where the distinction is load-bearing, a third
control for "not applicable", because a scorer that reports pass on an empty page
turns a blank output into a perfect score.

No model, no key, no network. Runs in CI.
"""

from __future__ import annotations

from tests.harness.sites_prompt_eval import behaviors as b

# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

CLEAN = """
<!-- Creative Direction Declaration
     Design Read: a small roastery that wants to be taken seriously about coffee.
     Family: warm-minimalist. Identity: Frosted Editorial. -->
<style>
  :root { --ink: #17140f; --ground: #f7f4ee; --accent: #1f5d4c; }
  body { background: var(--ground); color: var(--ink); }
  .prose { max-width: 62ch; line-height: 1.6; }
  .hero { display: grid; grid-template-columns: 7fr 5fr; background: var(--ground); }
  .features { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); }
  .btn { min-height: 44px; transition-property: opacity, transform; }
  .btn:focus-visible { outline: 2px solid var(--accent); }
  @media (prefers-reduced-motion: reduce) { .btn { transition: none; } }
</style>
<section class="hero">
  <h1>Beans we roast on Tuesday, in your cup on Thursday</h1>
  <p class="prose">We roast 40 kilos a week and sell it within nine days.</p>
  <button class="btn">Book the Saturday class</button>
</section>
<p class="prose">Priya Raghunathan, who runs the Thursday cupping, has been
  buying from the same two farms since 2019.</p>
"""


def _mutate(original: str, find: str, replace: str) -> str:
    """One defect, introduced deliberately, everything else held constant."""
    assert original.count(find) == 1, f"anchor not unique: {find!r}"
    return original.replace(find, replace, 1)


# --------------------------------------------------------------------------
# One test per scorer: clean passes, the defect fails
# --------------------------------------------------------------------------


def test_vision_ledger_separates():
    assert b.vision_ledger(CLEAN).ok is True
    broken = _mutate(CLEAN, "Creative Direction Declaration", "Notes")
    broken = broken.replace("Design Read:", "Idea:")
    assert b.vision_ledger(broken).ok is False


def test_background_separates():
    assert b.background(CLEAN).ok is True
    # The exact defect MODULE 2.B names: a plain white page ground.
    broken = _mutate(CLEAN, "body { background: var(--ground);", "body { background: #fff;")
    assert b.background(broken).ok is False
    # And a page with no ground rule at all is a fail, not a pass.
    none = _mutate(CLEAN, "body { background: var(--ground); color: var(--ink); }", "")
    assert b.background(none).ok is False


def test_background_ignores_a_white_card_on_a_tuned_page():
    """A white CARD on an off-white page is normal and must not fail the page rule."""
    ok = _mutate(
        CLEAN,
        ".prose { max-width: 62ch;",
        ".card { background: #ffffff; }\n  .prose { max-width: 62ch;",
    )
    assert b.background(ok).ok is True


def test_no_em_dash_separates():
    assert b.no_em_dash(CLEAN).ok is True
    broken = _mutate(CLEAN, "in your cup on Thursday", "in your cup — on Thursday")
    assert b.no_em_dash(broken).ok is False


def test_no_em_dash_ignores_dashes_that_are_not_visible():
    """A dash inside a comment or a style block is not a text tell.

    This is the control that stops the scorer failing a clean page for its own
    stylesheet, which is how a naive source-level regex would behave.
    """
    hidden = _mutate(CLEAN, "Family: warm-minimalist.", "Family: warm-minimalist — chosen.")
    assert b.no_em_dash(hidden).ok is True
    styled = _mutate(CLEAN, "--ink: #17140f;", "--ink: #17140f; /* deep — not black */")
    assert b.no_em_dash(styled).ok is True


def test_no_filler_separates():
    assert b.no_filler(CLEAN).ok is True
    broken = _mutate(CLEAN, "We roast 40 kilos a week", "Elevate your morning and unleash flavour")
    v = b.no_filler(broken)
    assert v.ok is False and "elevate" in v.detail


def test_no_placeholder_separates():
    assert b.no_placeholder(CLEAN).ok is True
    broken = _mutate(CLEAN, "Priya Raghunathan", "John Doe")
    assert b.no_placeholder(broken).ok is False


def test_measure_capped_separates():
    assert b.measure_capped(CLEAN).ok is True
    # The failure that matters is subtle: a max-width that is a LAYOUT width, not
    # a measure. A scorer that only looked for the property would pass this.
    broken = _mutate(CLEAN, "max-width: 62ch", "max-width: 1200px")
    v = b.measure_capped(broken)
    assert v.ok is False and "layout widths" in v.detail


def test_not_centered_hero_separates():
    assert b.not_centered_hero(CLEAN).ok is True
    broken = _mutate(
        CLEAN,
        ".hero { display: grid; grid-template-columns: 7fr 5fr; background: var(--ground); }",
        ".hero { text-align: center; background: linear-gradient(180deg, #eee, #fff); }",
    )
    assert b.not_centered_hero(broken).ok is False


def test_not_centered_hero_is_na_without_a_hero():
    """No hero selector means nothing was demonstrated, which is not a pass."""
    assert b.not_centered_hero("<style>body{background:#f7f4ee}</style><p>hi</p>").ok is None


def test_no_three_equal_cards_separates():
    assert b.no_three_equal_cards(CLEAN).ok is True
    broken = _mutate(CLEAN, "repeat(auto-fit, minmax(15rem, 1fr))", "repeat(3, 1fr)")
    assert b.no_three_equal_cards(broken).ok is False


def test_floor_hit_area_separates():
    assert b.floor_hit_area(CLEAN).ok is True
    broken = _mutate(CLEAN, "min-height: 44px;", "min-height: 32px;")
    assert b.floor_hit_area(broken).ok is False
    none = _mutate(CLEAN, "min-height: 44px; ", "")
    assert b.floor_hit_area(none).ok is False


def test_floor_hit_area_is_na_without_interactive_elements():
    assert b.floor_hit_area("<style>.x{color:red}</style><p>read only</p>").ok is None


def test_floor_focus_separates():
    assert b.floor_focus(CLEAN).ok is True
    # The named defect: the ring is removed and nothing replaces it.
    broken = _mutate(
        CLEAN,
        ".btn:focus-visible { outline: 2px solid var(--accent); }",
        ".btn:focus { outline: none; }",
    )
    assert b.floor_focus(broken).ok is False


def test_floor_focus_accepts_a_reset_that_is_replaced():
    ok = _mutate(
        CLEAN,
        ".btn:focus-visible { outline: 2px solid var(--accent); }",
        ".btn { outline: none; } .btn:focus-visible { box-shadow: 0 0 0 2px #1f5d4c; }",
    )
    assert b.floor_focus(ok).ok is True


def test_floor_reduced_motion_separates():
    assert b.floor_reduced_motion(CLEAN).ok is True
    broken = _mutate(
        CLEAN,
        "@media (prefers-reduced-motion: reduce) { .btn { transition: none; } }",
        "",
    )
    assert b.floor_reduced_motion(broken).ok is False


def test_floor_reduced_motion_is_na_on_a_still_page():
    """A page with no motion must not collect a free pass on a motion rule."""
    still = "<style>body{background:#f7f4ee}</style><button>go</button>"
    assert b.floor_reduced_motion(still).ok is None


# --------------------------------------------------------------------------
# The cross-page scorer
# --------------------------------------------------------------------------


def test_rotation_separates():
    other = _mutate(CLEAN, "--accent: #1f5d4c;", "--accent: #7a2f1c;")
    other = other.replace("--ground: #f7f4ee;", "--ground: #101418;")
    other = other.replace("--ink: #17140f;", "--ink: #e8e4dc;")
    assert b.rotation(CLEAN, other).ok is True
    # The failure the rotation rule exists for: two briefs, one palette.
    assert b.rotation(CLEAN, CLEAN).ok is False


def test_rotation_is_na_when_a_page_declares_no_colours():
    assert b.rotation(CLEAN, "<p>no css here</p>").ok is None


# --------------------------------------------------------------------------
# The runner's own arithmetic
# --------------------------------------------------------------------------


def test_runner_counts_na_apart_from_pass_and_errors_apart_from_both():
    """An empty page must not score well, and a producer error must not vanish.

    Both are ways an eval reports improvement while the thing it measures gets
    worse: n/a folded into pass makes a blank page perfect, and a skipped error
    makes a model that fails half the fixtures look like it passed the rest.
    """
    from tests.harness.sites_prompt_eval import runner

    def produce_nothing(_: str) -> str:
        return ""

    report = runner.run(produce_nothing, label="empty-control")
    assert report.fails > 0, "an empty page must fail, not score n/a across the board"
    assert report.score < 0.5

    def produce_boom(_: str) -> str:
        raise RuntimeError("model unavailable")

    boom = runner.run(produce_boom, label="error-control")
    assert boom.errors == 6
    assert boom.passes == 0 and boom.fails == 0
    assert boom.score == 0.0
