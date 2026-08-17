# tests/cloud/pockets/test_id_resolve.py — the other half of the shortened id.
# Created: 2026-08-03 (feat/prompt-entity-suffix).
#
# The prompt renders the last 8 characters of an id instead of all 24, because
# the id was 24 of the ~70 chars in a widget row and that row repeats 12 times
# inside a 1500-char cap. That is only safe if the tools can turn what the agent
# was SHOWN back into what they need, so these two halves must agree — and the
# round-trip test below is the one that holds them together. A renderer and a
# resolver that drift produce ids the agent cannot use, which is a worse failure
# than the prompt bloat either was meant to fix.
#
# THE SAFETY PROPERTY, and the reason this file leans on it so hard: AN
# AMBIGUOUS TAIL RAISES. It never picks, never takes the first match, never
# falls back to "closest". The entire point of rendering an id was that
# "- Sales (type=custom)" let a tool call land on the wrong entity silently; a
# short id that resolved loosely would be that same bug with extra steps.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and every one was applied, run,
# observed to fail, and reverted.

from __future__ import annotations

import pytest
from bson import ObjectId
from pocketpaw_ee.cloud.pockets.id_resolve import (
    AmbiguousId,
    normalize_id_input,
    resolve_id,
)

from pocketpaw.prompt.entity import ID_TAIL_MARKER, entity_line, short_id


class _Widget:
    def __init__(self, ident: str, name: str = "Revenue") -> None:
        self.id = ident
        self.name = name


class TestRoundTrip:
    """What the renderer showed is what the resolver accepts."""

    def test_a_rendered_tail_resolves_back_to_the_whole_id(self) -> None:
        """The contract between the two modules, asserted end to end.

        Neither half is interesting alone: a renderer that shortens correctly
        and a resolver that expands correctly are still broken if they disagree
        about WHICH characters. This drives the real renderer and feeds its
        output to the real resolver.

        THE MUTATION THAT BREAKS THIS: render the head in ``short_id``
        (``text[:ID_TAIL_CHARS]``) while the resolver keeps matching tails. Run:
        KeyError, no candidate ended with the head. (Applied 2026-08-03.)
        """
        ids = [str(ObjectId()) for _ in range(12)]
        widgets = [_Widget(i) for i in ids]

        for ident in ids:
            assert resolve_id(short_id(ident), widgets) == ident

    def test_the_id_as_it_appears_in_a_real_prompt_row_resolves(self) -> None:
        """Goes through ``entity_line``, not just ``short_id``.

        The agent does not see ``short_id``'s return value; it sees a row. If
        anything in the row assembly mangles the id — a strip, a case change, a
        stray character — this is where it shows up.

        THE MUTATION THAT BREAKS THIS: uppercase the id inside ``entity_line``.
        Run: KeyError, no lowercase-hex candidate matched. (Applied 2026-08-03.)
        """
        ident = str(ObjectId())
        row = entity_line("Revenue", ident, state="native")
        # Pull the id back out of the row exactly as an agent would read it.
        shown = row.split("id=")[1].split(",")[0]

        assert resolve_id(shown, [_Widget(ident)]) == ident

    def test_a_whole_id_still_resolves_to_itself(self) -> None:
        """Every pre-existing caller sends a whole id and must not regress.

        The frontend, stored references, and every tool call written before this
        change pass 24 chars. That path has to stay exact.

        THE MUTATION THAT BREAKS THIS: drop the ``if given in ids`` fast path.
        Run: a whole id is longer than ID_TAIL_CHARS, hit the length guard, and
        raised KeyError. (Applied 2026-08-03.)
        """
        ident = str(ObjectId())
        assert resolve_id(ident, [_Widget(ident)]) == ident


class TestAmbiguityRaises:
    """The property that keeps a short id from becoming the original bug."""

    def test_a_tail_matching_two_entities_raises(self) -> None:
        """Never pick. The whole design rests on this.

        THE MUTATION THAT BREAKS THIS: ``return matches[0]`` instead of raising
        on a multi-match. Run: no exception, the wrong widget came back, and
        ``pytest.raises`` failed. (Applied 2026-08-03.)
        """
        one = "aaaaaaaaaaaaaaaa" + "deadbeef"
        two = "bbbbbbbbbbbbbbbb" + "deadbeef"

        with pytest.raises(AmbiguousId):
            resolve_id("deadbeef", [_Widget(one), _Widget(two)])

    def test_the_error_names_the_candidates(self) -> None:
        """An agent told only "ambiguous" has to guess again, which is the bug.

        THE MUTATION THAT BREAKS THIS: raise ``AmbiguousId(given, [])``. Run:
        neither id appeared in the message and this failed.
        """
        one = "aaaaaaaaaaaaaaaa" + "deadbeef"
        two = "bbbbbbbbbbbbbbbb" + "deadbeef"

        with pytest.raises(AmbiguousId) as caught:
            resolve_id("deadbeef", [_Widget(one), _Widget(two)])

        assert one in str(caught.value)
        assert two in str(caught.value)

    def test_an_exact_id_beats_being_a_tail_of_another(self) -> None:
        """A caller passing a real id must never be told its own id is ambiguous.

        Contrived as a fixture, and the ordering it pins is not: exact-match-first
        is what keeps every existing full-id caller safe no matter what else is in
        the collection.

        THE MUTATION THAT BREAKS THIS: move the ``given in ids`` check below the
        tail matching. Run: AmbiguousId, for an id that exists exactly.
        """
        exact = "beefcafe"
        longer = "0123456789abcdef" + "beefcafe"

        assert resolve_id(exact, [_Widget(exact), _Widget(longer)]) == exact

    def test_no_match_raises_rather_than_returning_none(self) -> None:
        """A None return would get used as an id somewhere downstream.

        THE MUTATION THAT BREAKS THIS: ``return ""`` instead of raising KeyError.
        Run: no exception and ``pytest.raises`` failed.
        """
        with pytest.raises(KeyError):
            resolve_id("ffffffff", [_Widget(str(ObjectId()))])

    def test_something_longer_than_a_tail_is_not_matched_loosely(self) -> None:
        """A 20-char near-miss is a typo, not a shortening.

        Matching it by suffix would mean a mistyped id could still land on a
        real entity, which is exactly the silent-wrong-entity failure this
        design exists to make impossible.

        THE MUTATION THAT BREAKS THIS: delete the ``len(given) > ID_TAIL_CHARS``
        guard. Run: the typo resolved to a real widget and this failed.
        """
        ident = str(ObjectId())
        typo = ident[1:]  # 23 chars, still ends with the real tail

        with pytest.raises(KeyError):
            resolve_id(typo, [_Widget(ident)])


class TestWhatTheAgentActuallySends:
    """A model retyping an id does not reproduce it byte for byte."""

    def test_the_tail_marker_is_stripped(self) -> None:
        """The agent may echo ``…3f9a1c07`` verbatim, marker included.

        THE MUTATION THAT BREAKS THIS: stop stripping ``ID_TAIL_MARKER`` in
        ``normalize_id_input``. Run: KeyError — no id ends with the marker.
        """
        ident = str(ObjectId())
        assert resolve_id(f"{ID_TAIL_MARKER}{ident[-8:]}", [_Widget(ident)]) == ident

    def test_an_ascii_ellipsis_is_stripped_too(self) -> None:
        """Models substitute "..." for "…" constantly. Cheap to accept.

        THE MUTATION THAT BREAKS THIS: drop "..." from ``_STRIPPABLE_PREFIXES``.
        Run: KeyError. (Applied 2026-08-03.)
        """
        ident = str(ObjectId())
        assert resolve_id(f"...{ident[-8:]}", [_Widget(ident)]) == ident

    def test_quotes_and_whitespace_are_stripped(self) -> None:
        """Ids arrive wrapped in whatever punctuation surrounded them.

        THE MUTATION THAT BREAKS THIS: return ``str(raw)`` unchanged from
        ``normalize_id_input``. Run: both assertions failed.
        """
        assert normalize_id_input('  "abc123"  ') == "abc123"
        assert normalize_id_input(f"  {ID_TAIL_MARKER}abc123 ") == "abc123"

    def test_an_empty_id_raises_rather_than_matching_everything(self) -> None:
        """``endswith("")`` is True for every string — the dangerous default.

        Without the empty guard, a missing id would match the first candidate
        and silently mutate an arbitrary entity.

        THE MUTATION THAT BREAKS THIS: remove the ``if not given`` guard. Run:
        the empty string matched every widget and raised AmbiguousId instead —
        and with a single candidate it returned that widget outright, which is
        the silent wrong-entity write. (Applied 2026-08-03.)
        """
        with pytest.raises(KeyError):
            resolve_id("", [_Widget(str(ObjectId()))])
        with pytest.raises(KeyError):
            resolve_id(None, [_Widget(str(ObjectId()))])


class TestDictCandidates:
    """The pocket wire dict spells its id ``_id``; the domain objects use ``id``."""

    def test_a_wire_dict_resolves_on_underscore_id(self) -> None:
        """``pocket_to_wire_dict`` emits ``_id`` — the resolver must read it.

        THE MUTATION THAT BREAKS THIS: drop the ``_id`` fallback in ``_id_of``.
        Run: no candidate produced an id, KeyError. (Applied 2026-08-03.)
        """
        ident = str(ObjectId())
        assert resolve_id(short_id(ident), [{"_id": ident, "name": "Sales"}]) == ident

    def test_candidates_with_no_id_are_skipped_not_crashed_on(self) -> None:
        """A malformed row must not take down prompt assembly.

        THE MUTATION THAT BREAKS THIS: remove the ``if i`` filter. Run: the
        empty string entered the candidate list and the empty-tail hazard
        returned.
        """
        ident = str(ObjectId())
        assert resolve_id(short_id(ident), [{"name": "no id here"}, {"_id": ident}]) == ident
