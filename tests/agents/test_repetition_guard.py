# Unit tests for the streamed-reply repetition guard (UX-4): it trips on a
# sentence or short paragraph looping back to back, keeps one copy, and never
# trips on legitimate lists, tables, code or a status line that recurs a few times.

from __future__ import annotations

from pocketpaw.agents.repetition_guard import RepetitionGuard

_LOOP = "I have everything I need. Let me write the complete site file and deliver it to you."


def _feed(guard: RepetitionGuard, text: str, step: int = 7) -> bool:
    """Feed ``text`` in small chunks the way a stream arrives."""
    tripped = False
    for i in range(0, len(text), step):
        tripped = guard.feed(text[i : i + step]) or tripped
    return tripped


def test_the_two_sentence_loop_from_the_report_trips_and_keeps_one_copy():
    guard = RepetitionGuard()
    intro = "Great brief, a wedding site it is.\n\n"
    reply = intro + " ".join([_LOOP] * 40)
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == intro + _LOOP


def test_the_loop_trips_early_not_after_a_thousand_copies():
    guard = RepetitionGuard()
    fed = ""
    for _ in range(40):
        fed += _LOOP + " "
        if guard.feed(_LOOP + " "):
            break
    assert fed.count(_LOOP) <= 5


def test_a_loop_with_no_space_between_copies_trips():
    guard = RepetitionGuard()
    reply = _LOOP * 20
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == _LOOP


def test_one_sentence_repeated_on_its_own_lines_trips():
    guard = RepetitionGuard()
    line = "Now I'll write the complete site file."
    reply = "\n".join([line] * 8)
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == line


def test_a_three_sentence_paragraph_repeated_trips():
    guard = RepetitionGuard()
    para = (
        "I checked the layout and it looks right. The hero needs a stronger line. "
        "Let me update the copy now.\n\n"
    )
    reply = para * 6
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == para.strip()


def test_a_status_line_recurring_a_few_times_does_not_trip():
    guard = RepetitionGuard()
    reply = "Let me check the preview again.\n" * 4
    assert not _feed(guard, reply)


def test_a_markdown_list_with_repeated_items_does_not_trip():
    guard = RepetitionGuard()
    reply = "Here is the menu:\n" + "- Seasonal special, price on request\n" * 10
    assert not _feed(guard, reply)


def test_a_table_with_repeated_rows_does_not_trip():
    guard = RepetitionGuard()
    reply = "| Day | Hours |\n|---|---|\n" + "| Closed for the season | Closed |\n" * 10
    assert not _feed(guard, reply)


def test_code_inside_a_fence_does_not_trip():
    guard = RepetitionGuard()
    body = "      console.log('rendering the section now');\n" * 10 + "}\n" * 10
    reply = "Here is the script:\n```js\n" + body + "```\nThat is all."
    assert not _feed(guard, reply)


def test_ordinary_varied_prose_does_not_trip():
    guard = RepetitionGuard()
    reply = (
        "I built the hero with your tagline. The services section lists all four "
        "treatments. The booking form posts to your inbox. Prices sit under each "
        "service. The footer carries your address and hours. Want a gallery next?"
    )
    assert not _feed(guard, reply)
    assert guard.kept_text(reply) == reply
