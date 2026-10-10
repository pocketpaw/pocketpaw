# tests/cloud/test_paw_bar_card_restart.py — a card fence the model starts over.
#
# Haiku sometimes abandons a ```pawbar-card halfway (mid JSON string, often with a
# sentence of prose glued on) and writes "```pawbar-card" again with the whole card.
# That second opener is not a close: the partial card is dropped with
# ``card.rejected{reason: "restarted"}`` and the new one streams as c2. The two
# fixtures are the raw replies seen live (tests/fixtures/pawbar_restart_fence_*.txt).
# Whatever the chunking, the visible chat text never carries the fence marker or
# the spec. A complete card left unclosed and followed straight by "pawbar-card"
# reopens as a new card, a bare ``{"ui":`` line in text is stripped to the next
# fence, and a fence still open at the end of the stream never reaches the text.
#
# Mutations: tests/mutations/paw_bar_card_restart.json.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cloud.test_paw_bar_card_streaming import _collapse, _events
from tests.cloud.test_paw_bar_concierge_v2_output import _filtered_every_way, _run

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_OPEN = "```pawbar-card\n"
_LEAKS = ("pawbar-card", "```", '{"ui"')


def _reply(name: str) -> str:
    return (_FIXTURES / f"pawbar_restart_fence_{name}.txt").read_text(encoding="utf-8")


def _second_body(reply: str) -> str:
    start = reply.rindex(_OPEN) + len(_OPEN)
    return reply[start : reply.rindex("```")]


def _chunkings(reply: str) -> list[list[str]]:
    """Whole, per character, fixed slices, and split at every offset around both
    fence openers and the closer, so a marker can straddle any boundary."""
    ways = [[reply], list(reply)]
    ways += [[reply[i : i + n] for i in range(0, len(reply), n)] for n in (2, 3, 5, 7, 64)]
    marks = [reply.index(_OPEN), reply.rindex(_OPEN), reply.rindex("```")]
    for m in marks:
        ways += [[reply[:i], reply[i:]] for i in range(max(1, m - 3), m + len(_OPEN) + 2)]
    return ways


def _text(events: list[tuple[str, object]]) -> str:
    return "".join(data for name, data in events if name == "chunk")


def _assert_no_leak(text: str) -> None:
    for leak in _LEAKS:
        assert leak not in text, (leak, text[:200])


@pytest.mark.parametrize("name", ["heart", "mealplan"])
def test_a_restarted_card_never_leaks_the_spec_into_the_text(name):
    reply = _reply(name)
    intro = reply[: reply.index(_OPEN)]
    for chunks in _chunkings(reply):
        events = _events(chunks)
        text = _text(events)
        _assert_no_leak(text)
        assert text.strip() == intro.strip()


@pytest.mark.parametrize("name", ["heart", "mealplan"])
def test_the_abandoned_card_is_dropped_as_restarted_never_invalid(name):
    reply = _reply(name)
    for chunks in _chunkings(reply):
        events = _events(chunks)
        rejected = [d for e, d in events if e == "card.rejected"]
        assert {"card_id": "c1", "reason": "restarted"} in rejected
        assert not [d for d in rejected if d["card_id"] == "c1" and d["reason"] != "restarted"]
        starts = [d["card_id"] for e, d in events if e == "card.start"]
        assert starts == ["c1", "c2"]
        # The restart comes before the new card opens.
        names = [e for e, _ in _collapse(events)]
        assert names.index("card.rejected") < names.index("card.start", 2)


def test_the_restarted_heart_card_ends_as_one_final_with_the_whole_second_body():
    reply = _reply("heart")
    body = _second_body(reply)
    for chunks in _chunkings(reply):
        events = _events(chunks)
        finals = [d for e, d in events if e == "card.final"]
        assert finals == [{"card_id": "c2", "card": json.loads(body)}]
        deltas = "".join(d["text"] for e, d in events if e == "card.delta" and d["card_id"] == "c2")
        assert deltas == body
        assert [d["reason"] for e, d in events if e == "card.rejected"] == ["restarted"]


def test_the_restarted_meal_plan_is_judged_on_its_second_body_alone():
    # The model's second meal-plan body has stray closers, so it is refused, but
    # as c2 on its own body: c1 is only ever "restarted", and nothing reaches text.
    reply = _reply("mealplan")
    body = _second_body(reply)
    for chunks in _chunkings(reply):
        events = _events(chunks)
        deltas = "".join(d["text"] for e, d in events if e == "card.delta" and d["card_id"] == "c2")
        assert deltas == body
        assert [d for e, d in events if e == "card.rejected"] == [
            {"card_id": "c1", "reason": "restarted"},
            {"card_id": "c2", "reason": "invalid"},
        ]


def test_a_restart_left_open_at_the_end_is_truncated_and_never_shown():
    reply = _reply("heart")
    cut = reply[: reply.rindex("```")]
    for chunks in ([cut], [cut[:-400], *cut[-400:]]):
        events = _events(chunks)
        _assert_no_leak(_text(events))
        assert events[-1] == ("card.rejected", {"card_id": "c2", "reason": "truncated"})


def test_a_held_opener_at_the_end_of_the_stream_is_truncated():
    half = '{"ui":{"type":"text","props":{"text":"half'
    for tail in ("```", "```pawb", "```pawbar-card"):
        events = _events(["Hi\n", _OPEN, half, "\n\n", tail])
        _assert_no_leak(_text(events))
        assert events[-1][0] == "card.rejected" and events[-1][1]["reason"] in {
            "truncated",
            "restarted",
        }


def test_a_fence_mid_string_that_is_not_a_restart_still_closes_the_card():
    body = '{"ui":{"type":"text","props":{"text":"half\n'
    reply = f"Hi\n{_OPEN}{body}```\nAfter."
    for chunks in ([reply], list(reply)):
        events = _events(chunks)
        assert ("card.rejected", {"card_id": "c1", "reason": "invalid"}) in events
        assert _text(events) == "Hi\n\nAfter."


def test_a_complete_card_left_open_then_restarted_reopens_as_a_new_card():
    card = '{"ui":{"type":"text","props":{"text":"one"}}}'
    other = '{"ui":{"type":"text","props":{"text":"two"}}}'
    reply = f"Hi\n{_OPEN}{card}\n{_OPEN}{other}\n```\nbye"
    for chunks in ([reply], list(reply), [reply[i : i + 5] for i in range(0, len(reply), 5)]):
        events = _events(chunks)
        _assert_no_leak(_text(events))
        finals = [d["card"] for e, d in events if e == "card.final"]
        assert finals == [json.loads(card), json.loads(other)]


def test_a_bare_spec_line_in_the_text_is_stripped_to_its_fence():
    card = '{"ui":{"type":"text","props":{"text":"x"}}}'
    reply = f"Here you go.\n{card}\n```\nMore text."
    for chunks in ([reply], list(reply)):
        events = _events(chunks)
        assert _text(events) == "Here you go.\n\nMore text."


def test_a_bare_spec_at_the_very_start_is_stripped():
    card = '{"ui":{"type":"text","props":{"text":"x"}}}'
    assert _text(_events(list(card))) == ""


def test_a_brace_line_that_is_not_a_spec_still_streams():
    reply = 'Use {"a": 1} here.\n{"u": 2}\n{ok}'
    for chunks in ([reply], list(reply)):
        assert _text(_events(chunks)) == reply


def test_a_restarted_card_on_a_pawbar_site_shows_only_the_second_card():
    half = '{"ui":{"type":"text","props":{"text":"half'
    card = '{"ui":{"type":"text","props":{"text":"Open daily"}}}'
    reply = f"Hi\n{_OPEN}{half}\n\n{_OPEN}{card}\n```\nbye"
    out = _filtered_every_way(reply)
    assert out.count("```pawbar-card") == 1
    assert out.startswith("Hi\n```pawbar-card\n") and out.endswith("```\nbye")
    assert "half" not in out
    assert _run([reply]) == out


def test_a_held_reopen_at_the_end_of_the_stream_is_dropped_not_shown():
    card = '{"ui":{"type":"text","props":{"text":"one"}}}'
    for tail in ("pawba", "pawbar-card"):
        reply = f"Hi\n{_OPEN}{card}\n```{tail}"
        for chunks in ([reply], list(reply)):
            assert _text(_events(chunks)) == "Hi\n"
