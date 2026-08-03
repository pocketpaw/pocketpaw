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

from pocketpaw.prompt.entity import MISSING_ID, entity_line


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
        assert "68af1c2d9e4b7a0012f3c4d5" in one
        assert "68af1c2d9e4b7a0012f3c4d6" in two

    def test_the_id_survives_whole(self) -> None:
        """An id is exact or it is a failed tool call — never shortened.

        Every other field here is advisory. This one is an address, so no cap
        applies to it and none should be added later without reading this test.

        THE MUTATION THAT BREAKS THIS: truncate the id to 8 chars in
        ``entity_line``. Run: the full-id assertion failed.
        """
        ident = "68af1c2d9e4b7a0012f3c4d5"
        assert ident in entity_line("Sales", ident)

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
