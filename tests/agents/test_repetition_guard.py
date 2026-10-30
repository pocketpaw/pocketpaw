# Unit tests for the streamed-reply repetition guard (UX-4): it trips on a
# sentence or short paragraph looping far past anything legitimate, keeps one
# copy, and never trips on repetition a real reply contains (choruses, "write it
# ten times", logs, code, lists, tables, quotes, status lines between tool calls).

from __future__ import annotations

import time

from pocketpaw.agents.repetition_guard import RepetitionGuard

_LOOP = "I have everything I need. Let me write the complete site file and deliver it to you."


def _feed(guard: RepetitionGuard, text: str, step: int = 7) -> bool:
    """Feed ``text`` in small chunks the way a stream arrives."""
    tripped = False
    for i in range(0, len(text), step):
        tripped = guard.feed(text[i : i + step]) or tripped
    return tripped


# --- loops that must trip -----------------------------------------------------


def test_the_two_sentence_loop_from_the_report_trips_and_keeps_one_copy():
    guard = RepetitionGuard()
    intro = "Great brief, a wedding site it is.\n\n"
    reply = intro + " ".join([_LOOP] * 60)
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == intro + _LOOP


def test_the_loop_trips_long_before_a_thousand_copies():
    guard = RepetitionGuard()
    copies = 0
    for _ in range(1555):
        copies += 1
        if guard.feed(_LOOP + " "):
            break
    assert copies <= 10


def test_a_loop_with_no_space_between_copies_trips():
    guard = RepetitionGuard()
    reply = _LOOP * 40
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == _LOOP


def test_one_sentence_looping_on_its_own_lines_trips():
    guard = RepetitionGuard()
    line = "Now I'll write the complete site file."
    reply = "\n".join([line] * 40)
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == line


def test_a_three_sentence_paragraph_looping_trips():
    guard = RepetitionGuard()
    para = (
        "I checked the layout and it looks right. The hero needs a stronger line. "
        "Let me update the copy now.\n\n"
    )
    reply = para * 12
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == para.strip()


def test_a_cjk_loop_trips():
    guard = RepetitionGuard()
    sentence = "我已经准备好了所有需要的内容，现在开始为你编写完整的网站文件并交付给你。"
    reply = sentence * 40
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == sentence


def test_a_cyrillic_loop_trips():
    guard = RepetitionGuard()
    sentence = "У меня есть всё необходимое, сейчас я напишу полный файл сайта. "
    reply = sentence * 40
    assert _feed(guard, reply)
    assert guard.kept_text(reply) == sentence.strip()


# --- legitimate repetition that must not trip ---------------------------------


def test_a_line_written_ten_times_on_request_does_not_trip():
    guard = RepetitionGuard()
    reply = "Here you go:\n" + "I will not talk in class.\n" * 10
    assert not _feed(guard, reply)


def test_a_two_line_chorus_sung_four_times_does_not_trip():
    guard = RepetitionGuard()
    chorus = "Oh the tide comes rolling in tonight\nAnd we will dance until the morning light\n"
    verse = "Down by the harbour where the lanterns glow\n"
    reply = (verse + chorus) * 2 + chorus * 4
    assert not _feed(guard, reply)


def test_a_refrain_separated_by_short_lines_does_not_trip():
    guard = RepetitionGuard()
    reply = "".join(f"Verse {i}\nAnd the river keeps on running to the sea\n" for i in range(10))
    assert not _feed(guard, reply)


def test_unfenced_log_lines_do_not_trip():
    guard = RepetitionGuard()
    reply = "The build log says:\n" + "WARN retrying connection to the database\n" * 10
    assert not _feed(guard, reply)


def test_indented_code_does_not_trip():
    guard = RepetitionGuard()
    reply = "Example:\n\n" + "    console.log('rendering the section now');\n" * 40
    assert not _feed(guard, reply)


def test_tilde_fenced_code_does_not_trip():
    guard = RepetitionGuard()
    reply = "~~~\n" + "console.log('rendering the section now');\n" * 40 + "~~~\nDone."
    assert not _feed(guard, reply)


def test_backtick_fenced_code_does_not_trip():
    guard = RepetitionGuard()
    body = "console.log('rendering the section now');\n" * 40 + "}\n" * 40
    reply = "Here is the script:\n```js\n" + body + "```\nThat is all."
    assert not _feed(guard, reply)


def test_a_blockquote_does_not_trip():
    guard = RepetitionGuard()
    reply = "> The customer is always right, even when they are wrong.\n" * 40
    assert not _feed(guard, reply)


def test_a_markdown_list_with_repeated_items_does_not_trip():
    guard = RepetitionGuard()
    reply = "Here is the menu:\n" + "- Seasonal special, price on request\n" * 40
    assert not _feed(guard, reply)


def test_a_table_with_repeated_rows_does_not_trip():
    guard = RepetitionGuard()
    reply = "| Day | Hours |\n|---|---|\n" + "| Closed for the season | Closed |\n" * 40
    assert not _feed(guard, reply)


def test_a_status_line_recurring_across_tool_calls_does_not_trip():
    guard = RepetitionGuard()
    for _ in range(40):
        assert not guard.feed("Let me check the preview again and fix what I find.\n")
        guard.break_run()


def test_ordinary_varied_prose_does_not_trip():
    guard = RepetitionGuard()
    reply = (
        "I built the hero with your tagline. The services section lists all four "
        "treatments. The booking form posts to your inbox. Prices sit under each "
        "service. The footer carries your address and hours. Want a gallery next?"
    )
    assert not _feed(guard, reply)
    assert guard.kept_text(reply) == reply


# --- cost ---------------------------------------------------------------------


def test_a_long_unterminated_stream_stays_linear():
    """One-character chunks with no unit end: rescanning the tail every feed
    would be quadratic; this must finish quickly."""
    guard = RepetitionGuard()
    started = time.perf_counter()
    for _ in range(200_000):
        guard.feed("a")
    assert time.perf_counter() - started < 5
    assert len(guard._pending) <= 4001
