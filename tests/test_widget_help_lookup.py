# tests/test_widget_help_lookup.py — a lookup miss must not return the library.
# Created: 2026-08-04 (fix/prompt-tells-the-truth).
#
# WHAT THIS CAUGHT. ``widget_help`` resolves a widget type in three tiers, and
# both the miss path and the third tier were broken in the same direction:
# they answered a question nobody asked, at enormous size.
#
#   * A TOTAL MISS returned ``RIPPLE_DESIGN_RULES`` — the entire 58,765-char
#     design rulebook, larger than the whole system prompt, never once
#     mentioning the widget that was asked about. This module carries guidance
#     for 16 widgets; the manifest carries 150+. The inline prompt sends the
#     agent here for widgets outside the core six, so the miss path WAS the
#     common path.
#   * TIER 3 matched the type ANYWHERE in a section body, so every lookup hit
#     ``# WIDGET CATALOG`` (a bare list of every widget name) and
#     ``# CANONICAL SHAPES`` (13,479 chars covering all of them). Measured
#     before the fix: sparkline 40,177 chars, gauge 34,629, definition-list
#     18,623 — none containing that widget's prop schema, because a listing
#     that names a widget is not documentation of it.
#
# The two compounded into the exact failure the caller was built to prevent.
# The inline prompt cites ``definition-list`` shipping with ``description``
# where the manifest says ``definition``; a lookup for ``definition-list``
# returned 18,623 characters that named it only in a catalog line, and the
# agent guessed. After the fix that lookup is 746 chars and says which tool
# has the schema.
#
# WHY SIZE IS ASSERTED AS A CEILING, NOT A NUMBER. Guidance text gets edited;
# pinning exact lengths would fail on every wording change and teach people to
# bump the number. The invariant is the ORDER OF MAGNITUDE — a miss must not
# be catalog-sized.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted (``scripts/mutate.py``).

from __future__ import annotations

import pytest

# A widget that is real (it is in the ripple manifest) but has no hand-written
# guidance here — the case the old code answered with the whole rulebook.
UNCOVERED = "navbar"
# A widget this module genuinely documents.
COVERED = "chart"


def _help(*types: str) -> str:
    from pocketpaw.ripple._inline_core import widget_help

    return widget_help(list(types))


def _rulebook() -> str:
    from pocketpaw.ripple._design import RIPPLE_DESIGN_RULES

    return RIPPLE_DESIGN_RULES


class TestAMissIsShortAndHonest:
    def test_an_uncovered_type_does_not_return_the_whole_rulebook(self) -> None:
        """The headline regression.

        THE MUTATION THAT BREAKS THIS: restore ``return RIPPLE_DESIGN_RULES``
        as the no-parts fallback. Run: 58,765 chars came back and this failed.
        (Applied 2026-08-04.)
        """
        out = _help(UNCOVERED)
        assert out != _rulebook()
        assert len(out) < 3_000, (
            f"a lookup miss returned {len(out)} chars — that is a document dump, not an answer"
        )

    def test_the_miss_names_the_type_that_missed(self) -> None:
        """An answer that doesn't name the question is indistinguishable from
        a wrong answer. The old dump never mentioned the requested widget.

        THE MUTATION THAT BREAKS THIS: drop ``asked`` from the note's first
        line. Run: failed. (Applied 2026-08-04.)
        """
        assert UNCOVERED in _help(UNCOVERED)

    def test_the_miss_routes_to_the_tool_that_can_answer(self) -> None:
        """A dead end makes the model improvise; a redirect does not.

        Asserts the CALL, not the bare name. A first draft checked only that
        the string ``get_widget_spec`` appeared somewhere in the note, and a
        mutation that deleted the entire routing sentence escaped it — the
        name survived in a later paragraph that merely mentions the tool. A
        mention is not a redirect.

        THE MUTATION THAT BREAKS THIS: replace the routing sentence in
        ``_unknown_types_note`` with a bare "That is not the full widget
        list." Run: failed. (Applied 2026-08-04.)
        """
        assert "call `get_widget_spec(types=[...])`" in _help(UNCOVERED)

    def test_the_miss_explains_what_an_unknown_type_means(self) -> None:
        """This is what makes a stale manifest pin self-diagnosing rather than
        silent: if get_widget_spec also rejects the type, the deployment
        genuinely cannot render it, and the agent is told to pick another
        widget instead of emitting one that draws nothing.

        Asserts the consequence, not the word "manifest" — which appears three
        times in the note for unrelated reasons and let a mutation of this very
        paragraph escape.

        THE MUTATION THAT BREAKS THIS: soften "manifest does not carry that
        widget" to "manifest may lack that widget". Run: failed. (Applied
        2026-08-04.)
        """
        out = _help(UNCOVERED)
        assert "manifest does not carry" in out, (
            "the note must state the unknown type is genuinely absent, not merely undocumented here"
        )
        assert "render as nothing" in out, "the note must state the consequence of emitting it"


class TestCoverageIsNotLost:
    def test_a_covered_type_still_returns_its_full_guidance(self) -> None:
        """The fix must not become a regression in the other direction.

        THE MUTATION THAT BREAKS THIS: route the WIDGET_SHAPES tier through
        ``_unknown_types_note`` too. Run: chart guidance collapsed to the miss
        note and this failed. (Applied 2026-08-04.)
        """
        out = _help(COVERED)
        assert len(out) > 5_000, "a documented widget must still get its real guidance"
        assert "get_widget_spec" not in out, "a hit must not be dressed up as a miss"

    def test_no_types_still_returns_the_full_rulebook(self) -> None:
        """Pre-existing, deliberate behaviour: asking for everything is a real
        request. Only the MISS path changed.

        THE MUTATION THAT BREAKS THIS: make the empty-types call return the
        unknown-types note. Run: failed. (Applied 2026-08-04.)
        """
        from pocketpaw.ripple._inline_core import widget_help

        assert widget_help([]) == _rulebook()
        assert widget_help(None) == _rulebook()

    def test_a_partial_hit_reports_the_half_that_missed(self) -> None:
        """Silently dropping the miss reads as full coverage, and the agent
        emits the undocumented widget on the strength of the other one's
        schema.

        THE MUTATION THAT BREAKS THIS: skip the ``if unresolved`` append in the
        parts branch. Run: navbar vanished from a chart+navbar lookup and this
        failed. (Applied 2026-08-04.)
        """
        out = _help(COVERED, UNCOVERED)
        assert "bar" in out.lower(), "the covered widget's guidance must survive"
        assert UNCOVERED in out, "the uncovered widget's miss must be reported"


class TestTier3MatchesHeadingsNotProse:
    def test_a_listing_that_merely_names_a_widget_is_not_guidance(self) -> None:
        """``# WIDGET CATALOG`` names every widget, so a body-substring search
        matched it for every lookup and returned a name list as though it were
        documentation.

        THE MUTATION THAT BREAKS THIS: match on the section BODY
        (``t in sect.lower()``) instead of the heading. Run: the lookup ballooned
        past the ceiling and this failed. (Applied 2026-08-04.)
        """
        out = _help(UNCOVERED)
        assert "WIDGET CATALOG" not in out, (
            "a section that merely lists widget names came back as guidance"
        )

    @pytest.mark.parametrize("widget", ["sparkline", "gauge", "definition-list"])
    def test_the_measured_regressions_stay_fixed(self, widget: str) -> None:
        """The three widgets measured before the fix at 40,177 / 34,629 /
        18,623 chars. Named individually because they are the evidence.

        THE MUTATION THAT BREAKS THIS: same as above — body-substring search.
        Run: all three failed. (Applied 2026-08-04.)
        """
        assert len(_help(widget)) < 3_000
