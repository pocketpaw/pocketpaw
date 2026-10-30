# tests/test_prompt_entity_line.py — the canonical entity row.
# Created: 2026-08-03 (feat/prompt-entity-ids).
#
# ``entity_line`` exists so a prompt cannot name something the agent is unable to
# address. These tests hold the two properties that claim buys:
#
#   * INJECTIVITY ON ID — two entities differing only in id render differently.
#     This is the real property, and it is strictly stronger than "the output
#     contains an id", which a renderer emitting a constant would also satisfy.
#   * THE ID CANNOT BE OMITTED — not by forgetting the argument (TypeError) and
#     not by passing a name that shadows it (positional-only).
#
# The enforcement that every handler actually CALLS this lives next door in
# tests/cloud/surface/test_entity_id_contract.py. A correct renderer nobody
# reaches is the original bug intact.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted.

from __future__ import annotations

import pytest

from pocketpaw.prompt.entity import (
    ID_TAIL_CHARS,
    ID_TAIL_MARKER,
    MISSING_ID,
    entity_line,
    short_id,
    unaddressed_line,
)


class TestInjectiveOnId:
    """The property the four broken handlers did not have."""

    def test_two_entities_differing_only_in_id_render_differently(self) -> None:
        """The exact production shape: one workspace, two pockets called Sales.

        Before this module they rendered byte-identical rows and "open the Sales
        pocket" resolved to whichever one the model felt like.

        THE MUTATION THAT BREAKS THIS: drop ``id={ident}`` from ``parts``. Run:
        both rows rendered ``- Sales (type=custom)`` and the assertion failed.
        """
        one = entity_line("Sales", "68af1c2d9e4b7a0012f3c4d5", type="custom")
        two = entity_line("Sales", "68af1c2d9e4b7a0012f3c4d6", type="custom")

        assert one != two
        assert "12f3c4d5" in one
        assert "12f3c4d6" in two

    def test_ids_that_differ_only_in_the_head_still_render_differently(self) -> None:
        """The adversarial case for a TAIL, and the reason it is a tail.

        Two ids sharing a tail would render identically and reintroduce the bug.
        That is not the shape ObjectIds actually collide in — they share HEADS,
        because the leading 4 bytes are a timestamp and the next 5 are constant
        per process — but a renderer that only works for one id scheme is a trap
        for whoever adds the next one.

        So: heads differ, tails differ, rows differ. If a future id scheme puts
        the entropy at the front, this is the test that will fail and say so.

        THE MUTATION THAT BREAKS THIS: render the HEAD instead of the tail
        (``text[:ID_TAIL_CHARS]``). Run: two ids created in the same second
        rendered the same row and this failed. (Applied 2026-08-03.)
        """
        one = entity_line("Sales", "6a70c69ecdf9641d9280ebb6")
        two = entity_line("Sales", "6a70c69ecdf9641d9280ebb7")
        assert one != two

    def test_a_short_id_is_not_shortened_further(self) -> None:
        """Slugs, uuids and fixture ids pass through untouched.

        Marking an 8-char id as a tail would spend a character to say nothing,
        and would hand the resolver a marker where it expects a whole id.

        THE MUTATION THAT BREAKS THIS: drop the length guard in ``short_id`` and
        always prepend the marker. Run: ``pk-123`` rendered as ``…pk-123``.
        """
        assert entity_line("Sales", "pk-123") == "- Sales (id=pk-123)"
        assert short_id("abcd1234") == "abcd1234"

    def test_the_rendered_tail_is_the_real_tail(self) -> None:
        """What replaced "the id survives whole".

        The id used to be rendered entire, on the reasoning that an id is exact
        or it is a failed tool call. That is still true — what changed is that
        the TOOLS now resolve a tail (``pockets/id_resolve.py``), so a tail is
        exact. This test holds the renderer's half of that bargain: the chars it
        shows are genuinely the last ones of the id, so the resolver can match on
        them. The round trip through the real resolver is asserted in
        ``tests/cloud/pockets/test_id_resolve.py``.

        THE MUTATION THAT BREAKS THIS: render ``text[-ID_TAIL_CHARS - 1 : -1]``.
        Run: the endswith assertion failed. (Applied 2026-08-03.)
        """
        ident = "68af1c2d9e4b7a0012f3c4d5"
        rendered = short_id(ident)
        assert rendered == f"{ID_TAIL_MARKER}{ident[-ID_TAIL_CHARS:]}"
        assert ident.endswith(rendered.lstrip(ID_TAIL_MARKER))

    def test_labels_differing_still_render_differently(self) -> None:
        """Injectivity on id must not have cost injectivity on the label.

        THE MUTATION THAT BREAKS THIS: render only the id and drop the label.
        Run: both rows were ``- (id=p1)`` and the assertion failed.
        """
        assert entity_line("Sales", "p1") != entity_line("Support", "p1")


class TestTheIdCannotBeOmitted:
    """Structural, not conventional — the API refuses the bug."""

    def test_forgetting_the_id_is_a_type_error(self) -> None:
        """The guarantee that makes this module worth existing.

        A default would make ``entity_line(name)`` legal and the whole class of
        bug would come straight back, silently, at the first hurried call site.

        THE MUTATION THAT BREAKS THIS: give ``entity_id`` a default of ``None``.
        Run: no TypeError was raised and ``pytest.raises`` failed.
        """
        with pytest.raises(TypeError):
            entity_line("Sales")  # type: ignore[call-arg]

    def test_a_fact_may_be_named_label_or_entity_id(self) -> None:
        """What the positional-only ``/`` actually buys.

        The obvious test here — ``entity_line("Sales", id="p1")`` raises — passes
        with the ``/`` removed too, because ``entity_id`` is simply missing. It
        proved nothing; the mutation harness caught it escaping and this replaced
        it.

        The real property is collision: facts are arbitrary caller-chosen names,
        and two of them collide with this signature. Without the ``/`` a handler
        rendering a ``label=`` fact gets "got multiple values for argument
        'label'" — a crash in prompt assembly, from a field name.

        THE MUTATION THAT BREAKS THIS: remove the ``/`` from the signature. Run:
        TypeError, multiple values for argument 'label'. (Applied 2026-08-03.)
        """
        row = entity_line("Sales", "p1", label="Q3", entity_id="not-the-id")
        assert row == "- Sales (id=p1, label=Q3, entity_id=not-the-id)"

    def test_naming_the_id_as_a_keyword_does_not_smuggle_it_in(self) -> None:
        """``entity_line("Sales", id="p1")`` is a missing id, and must not pass.

        THE MUTATION THAT BREAKS THIS: give ``entity_id`` a default of ``None``.
        Run: the call bound ``id`` as a fact, returned a row with ``id=?``, and
        ``pytest.raises`` failed. (Applied 2026-08-03.)
        """
        with pytest.raises(TypeError):
            entity_line("Sales", id="p1")  # type: ignore[call-arg,misc]


class TestMissingIdIsVisible:
    """A caller with no id gets a hole in the prompt, not a tidy-looking row."""

    def test_a_none_id_renders_the_marker_and_does_not_raise(self) -> None:
        """Degrade, never drop — a row is still better than no row.

        The marker is not empty and does not read like an id, so the agent
        cannot pass it to a tool and a human reading a prompt dump sees it.

        THE MUTATION THAT BREAKS THIS: ``return ""`` when ``entity_id`` is None.
        Run: the row was empty and both assertions failed.
        """
        row = entity_line("report.pdf", None, mime="application/pdf")
        assert f"id={MISSING_ID}" in row
        assert "report.pdf" in row

    def test_the_marker_is_not_mistakable_for_an_id(self) -> None:
        """Guards the constant itself.

        THE MUTATION THAT BREAKS THIS: set ``MISSING_ID = "unknown"``. Run: it
        is alphanumeric and reads as a real id — the assertion failed.
        """
        assert MISSING_ID
        assert not MISSING_ID.isalnum()

    def test_an_empty_id_is_treated_as_missing(self) -> None:
        """``""`` and ``None`` are one thing, not two.

        Services return both for "absent" depending on whether the field is
        optional or defaulted, and ``id=`` with nothing after it reads as a
        formatting bug rather than as missing data.

        THE MUTATION THAT BREAKS THIS: use ``str(entity_id)`` directly instead
        of ``_clean(...) or MISSING_ID``. Run: the row rendered ``(id=)``.
        """
        assert f"id={MISSING_ID}" in entity_line("report.pdf", "")


class TestRowStaysOneRow:
    """A row that is really half a row corrupts the block around it."""

    def test_a_newline_in_a_label_does_not_split_the_row(self) -> None:
        """Names and filenames are user-supplied, so this is reachable.

        The preamble cap is line-aware; a label carrying a newline invents a
        line boundary that is not in the data, and the cap then cuts there.

        THE MUTATION THAT BREAKS THIS: make ``_clean`` return ``str(value)``
        unchanged. Run: the row contained a newline and the assertion failed.
        """
        row = entity_line("Q3\nplan", "p1")
        assert "\n" not in row
        assert "Q3 plan" in row

    def test_a_newline_in_a_fact_does_not_split_the_row(self) -> None:
        """Same hazard, other half of the signature.

        THE MUTATION THAT BREAKS THIS: stop routing fact values through
        ``_clean``. Run: the row contained a newline.
        """
        assert "\n" not in entity_line("Sales", "p1", note="line\nbreak")


class TestFactRendering:
    def test_facts_render_in_call_order_after_the_id(self) -> None:
        """The handler decides what a reader sees first; the id always leads.

        THE MUTATION THAT BREAKS THIS: ``sorted(facts.items())``. Run: ``agents``
        sorted ahead of ``type`` and ``widgets`` and the index assertions failed.
        """
        row = entity_line("Sales", "p1", type="custom", widgets=3, agents=2)
        assert row == "- Sales (id=p1, type=custom, widgets=3, agents=2)"

    def test_an_empty_fact_value_renders_the_marker(self) -> None:
        """``mime=`` with nothing after it reads as a bug, not as unknown data.

        THE MUTATION THAT BREAKS THIS: drop the ``or MISSING_ID`` from the fact
        comprehension. Run: the row rendered ``(id=f1, mime=)``.
        """
        expected = f"- report.pdf (id=f1, mime={MISSING_ID})"
        assert entity_line("report.pdf", "f1", mime=None) == expected

    def test_no_facts_is_a_valid_row(self) -> None:
        """Not every entity has anything worth saying beyond its name.

        THE MUTATION THAT BREAKS THIS: require at least one fact. Run: TypeError.
        """
        assert entity_line("Sales", "p1") == "- Sales (id=p1)"

    def test_a_nameless_entity_still_renders(self) -> None:
        """Degrade, never drop — the id is the part that matters anyway.

        THE MUTATION THAT BREAKS THIS: ``return ""`` for a falsy label. Run: the
        row was empty and the id assertion failed.
        """
        row = entity_line("", "p1")
        assert "(unnamed)" in row
        assert "id=p1" in row


class TestUnaddressedLine:
    """The row for an entity no tool addresses by id.

    Added 2026-08-03 (feat/prompt-entity-suffix). Review's verdict on the first
    pass was that files and agents were rendering ids no tool accepts, justified
    after the fact. This is what replaced that: no id at all, and the ``kind``
    argument turns the exemption into a claim the contract test re-checks against
    the tool schemas on every run.
    """

    def test_it_renders_no_id(self) -> None:
        """The whole point — the chars an unusable id would cost are not spent.

        THE MUTATION THAT BREAKS THIS: have ``unaddressed_line`` delegate to
        ``entity_line`` with a None id. Run: the row carried ``id=?``.
        """
        row = unaddressed_line("file", "report.pdf", mime="application/pdf")
        assert row == "- report.pdf (mime=application/pdf)"
        assert "id=" not in row

    def test_the_kind_is_never_rendered(self) -> None:
        """``kind`` exists to be READ BY THE TEST, not by the model.

        Leaking it would put a word in the prompt that means nothing to the
        agent and costs tokens on every row.

        THE MUTATION THAT BREAKS THIS: prepend ``f"{kind}: "`` to the label.
        Run: 'file' appeared in the row and this failed.
        """
        assert "file" not in unaddressed_line("file", "report.pdf")

    def test_a_row_with_no_facts_has_no_empty_parens(self) -> None:
        """``- workspace:w1`` — a KB scope has nothing to add and should say so.

        THE MUTATION THAT BREAKS THIS: always append ``(…)``. Run: the row
        rendered ``- workspace:w1 ()`` and this failed. (Applied 2026-08-03.)
        """
        assert unaddressed_line("kb_scope", "workspace:w1") == "- workspace:w1"

    def test_it_still_collapses_newlines(self) -> None:
        """Same one-row-per-row hazard as ``entity_line``; same fix.

        THE MUTATION THAT BREAKS THIS: stop routing the label through ``_clean``.
        Run: the row contained a newline.
        """
        assert "\n" not in unaddressed_line("file", "Q3\nplan.pdf")

    def test_it_still_names_a_nameless_entity(self) -> None:
        """Degrade, never drop.

        THE MUTATION THAT BREAKS THIS: return "" for a falsy label. Run: empty.
        """
        assert unaddressed_line("file", "") == "- (unnamed)"
