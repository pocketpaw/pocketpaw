# The ripple cards paragraph's per-turn widget section (paw_bar/widget_relevance):
# the widget a landing chip prompt needs is picked, follow-ups and earlier cards
# keep their widgets, the prompt is byte-stable per input with the fixed core
# first, and core plus the worst-case section stays under the 32,000 cap.
from __future__ import annotations

import pytest
from pocketpaw_ee.paw_bar import widget_relevance
from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
from pocketpaw_ee.paw_bar.concierge_runtime import _cards_paragraph

_SECTION = "   Widgets for this message, with their props:"

# Landing chip prompts (ripple routes/live/scenarios.ts and routes/pawbar/play-cards.ts)
# and the widget each needs.
_CHIPS = [
    ("I want to order a burger", "menu-order"),
    ("Build me a pizza: pick a size, crust and toppings and show the price", "menu-order"),
    ("Book a table for 4 on Saturday evening", "booking"),
    ("Book a dentist appointment", "booking"),
    ("Help me pick a time for a dentist appointment next week from the open slots", "booking"),
    ("Give me a notepad for ideas with tags, start with two sample notes", "notes"),
    ("Show me a plane in 3D and label its parts", "model-viewer"),
    ("Show me a human heart in 3D and label its chambers and main vessels", "model-viewer"),
    (
        "Show me on a map a walking route from Rossio station to São Jorge Castle in Lisbon",
        "map",
    ),
    ("Make me a focus music player with a few calm tracks", "music-player"),
    ("Guide me through a 15-minute morning yoga session, one pose at a time", "interval-workout"),
    ("Make me an invoice for 3 hours of design work and a logo package", "invoice-layout"),
    ("Make me a due diligence checklist for buying a small online shop", "checklist-layout"),
    ("Make me a packing checklist for a 4-day beach weekend", "checklist-layout"),
    ("Give me a quarterly financial summary: revenue, costs, profit and cash", "exec-dashboard"),
    ("Make me a 5-question customer feedback survey with star ratings", "form-layout"),
    ("Give me a simple drawing pad with a few colors and a brush size", "drawing-canvas"),
    ("Show me a month calendar with a few sample events on it", "calendar"),
    ("Quiz me with 5 world geography questions and show my score at the end", "quiz"),
    ("Find me a quick vegetarian dinner recipe with ingredients and steps", "recipe"),
    ("Let's play tic-tac-toe against you, I'm X, medium difficulty", "board-game"),
    ("Make me a pomodoro focus timer: 25 minutes focus, 5 minute breaks", "focus-timer"),
    ("Track my habits this week: reading, running, water and sleep", "habit-tracker"),
    ("Make me a memory match game pairing Spanish animal words with emoji", "memory-match"),
    ("Make me a 5-letter word guessing game about space, with a hint", "word-guess"),
    ("Show me wireless headphones under $200 side by side so I can compare", "comparison-layout"),
    ("Plan a weekend getaway to the mountains with a rough budget", "itinerary"),
    # The six data widgets from ripple #223 and #224, and the audit's unused widgets.
    ("7-day weather forecast for Lisbon", "weather-forecast"),
    ("coffee shops near me", "place-results"),
    ("find deals on headphones", "product-grid"),
    ("documentaries about the ocean", "media-grid"),
    ("meeting agenda starting at 10", "agenda"),
    ("weekly schedule", "week-schedule"),
    ("create an invoice", "invoice-layout"),
    ("due diligence checklist", "checklist-layout"),
    ("order a pizza", "menu-order"),
]


def _paragraph(message: str = "", history=()) -> str:
    return _cards_paragraph([], profile=RIPPLE_PROFILE, message=message, history=history)


def _section(text: str) -> str:
    return text[text.index(_SECTION) :] if _SECTION in text else ""


@pytest.mark.parametrize(("message", "widget"), _CHIPS)
def test_a_chip_prompt_gets_its_widget_with_props(message, widget):
    """THE MUTATION THAT BREAKS THIS: drop the intents table (INTENTS = {})."""
    assert widget in widget_relevance.pick_widgets(message)
    assert f"   - {widget} {{" in _section(_paragraph(message)) or (
        f"   - {widget}: " in _section(_paragraph(message))
    )


def test_the_steering_hints_reach_the_section():
    """THE MUTATION THAT BREAKS THIS: drop the hint from full_line."""
    checklist = _section(_paragraph("due diligence checklist"))
    assert "never a select per row" in checklist
    assert "Food orders always use menu-order" in _section(_paragraph("order a pizza"))
    weather = _section(_paragraph("7-day weather forecast for Lisbon"))
    assert "rain|heavy-rain|thunder|snow|fog|wind|night-clear" in weather
    dentist = _section(_paragraph("Book a dentist appointment"))
    assert "only when it is not a table" in dentist


def test_the_3d_line_carries_the_asset_library():
    section = _section(_paragraph("Show me a plane in 3D"))
    assert "plane(nose,cockpit,left-wing,right-wing,engine,tail)" in section
    assert "never a src, poster or URL" in section


def test_a_follow_up_keeps_the_widget_from_the_last_turns():
    """THE MUTATION THAT BREAKS THIS: score the message alone, not the history."""
    history = [
        {"role": "user", "content": "Book a table for 4 on Saturday evening"},
        {"role": "assistant", "content": "Here are the open times."},
    ]
    assert "booking" in widget_relevance.pick_widgets("make it 6 people", history)


def test_a_widget_an_earlier_card_used_stays_picked():
    """THE MUTATION THAT BREAKS THIS: leave earlier_widgets out of pick_widgets."""
    card = '```pawbar-card\n{"ui":{"type":"notes","props":{"notes":[]}},"state":{}}\n```'
    history = [
        {"role": "user", "content": "something to write in"},
        {"role": "assistant", "content": "Here you go. " + card},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "Anytime."},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "Sure."},
    ]
    picked = widget_relevance.pick_widgets("change the colour of the second one", history)
    assert picked[0] == "notes"


def test_picks_are_capped_and_never_echo_the_visitor():
    """THE MUTATION THAT BREAKS THIS: drop the [:TOP_K] slice."""
    every = " ".join(widget_relevance.CANDIDATES) + " zqxjv"
    cards = [{"role": "assistant", "content": f'"type":"{n}"'} for n in widget_relevance.CANDIDATES]
    picked = widget_relevance.pick_widgets(every, cards)
    assert len(picked) <= widget_relevance.MAX_PICKED
    assert "zqxjv" not in _paragraph(every, cards)


def test_a_message_that_needs_no_widget_gets_the_core_alone():
    assert widget_relevance.pick_widgets("hi") == []
    assert _SECTION not in _paragraph("hi")


def test_the_prompt_is_byte_stable_and_the_core_comes_first():
    message = "Make me a pomodoro focus timer with a few calm tracks"
    history = [{"role": "user", "content": "I need to study"}]
    first = _paragraph(message, history)
    assert first == _paragraph(message, [dict(t) for t in history])
    core = _paragraph()
    assert first.startswith(core) and first != core
    # A different message changes only what follows the core.
    assert _paragraph("Show me a plane in 3D").startswith(core)


def test_core_plus_the_worst_case_section_stays_under_the_cap():
    """The fixed core is about 17,700 chars; the worst per-turn section is the
    MAX_PICKED longest candidate lines (about 6,700), so the paragraph peaks near
    24,400, against 31,855 when every widget rode every turn."""
    core = _paragraph()
    longest = sorted(
        (len(widget_relevance.full_line(n)) + 4 for n in widget_relevance.CANDIDATES),
        reverse=True,
    )
    worst = len(core) + 1 + len(_SECTION) + sum(longest[: widget_relevance.MAX_PICKED])
    assert len(core) < 20_000
    assert worst < 32_000
    typical = sorted(len(_paragraph(m)) for m, _ in _CHIPS)[len(_CHIPS) // 2]
    assert len(core) < typical < worst
