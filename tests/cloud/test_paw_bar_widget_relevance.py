# The ripple cards paragraph's per-turn widget section (paw_bar/widget_relevance):
# the widget a landing chip prompt needs is picked (intents from the vendored
# manifest plus the local overlay), follow-ups and earlier cards keep their
# widgets, the prompt is byte-stable per input with the fixed core first, the
# rules that repeat a ripple craft rule lead with its manifest text, and core plus
# the worst-case section stays no larger than before and under the 32,000 cap.
from __future__ import annotations

import json
import re

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
    ("A standup board for 5 people with blockers flagged", "standup-board"),
    ("create an invoice", "invoice-layout"),
    ("due diligence checklist", "checklist-layout"),
    ("order a pizza", "menu-order"),
    # The drawing widgets (ripple #236, #237): its /live gallery prompts.
    (
        "Show how a bike's chain drive works as a live drawing, with a cadence slider "
        "and how far one pedal turn takes you",
        "iso-scene",
    ),
    ("Show me how a one-cylinder piston engine moves, with an engine speed slider", "iso-scene"),
    ("Draw the gears of a clock as an animated diagram", "iso-scene"),
    (
        "How does the heart pump blood? Show me the two sides and the valves, and let me "
        "change the heart rate.",
        "figure",
    ),
    # The hand-built figures (ripple #248): each subject gets its figure.
    ("How does a rocket engine work?", "figure"),
    ("How does a fusion reactor work?", "figure"),
    ("How does CERN's collider work?", "figure"),
    ("How do bike gears work?", "figure"),
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
    savings = _section(_paragraph("Show me how my savings grow"))
    assert "never add inputs around it" in savings
    invoice = _section(_paragraph("create an invoice"))
    assert "summary rows use value" in invoice


def test_the_drawing_lines_teach_the_shape_and_the_preference():
    """figure's line names every catalog id with its sliders and anchors; iso-scene's
    carries its kinds, the expression rule and an example card that passes the
    checks; both hints steer figure over iso-scene over illustration.
    THE MUTATION THAT BREAKS THIS: drop the figure or iso-scene branch of full_line."""
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_FIGURES, render_card

    heart = _section(_paragraph("How does the heart pump blood?"))
    assert "   - figure {figure" in heart and "   - iso-scene {" in heart
    assert f"heart({RIPPLE_FIGURES['heart']['title']}; " in heart
    assert ",".join(RIPPLE_FIGURES["heart"]["anchors"]) in heart
    for fid, figure in RIPPLE_FIGURES.items():
        if fid != "heart":
            assert f"{fid} ({figure['title']})" in heart
    assert "bpm 40..180" in heart and "it is hand-built and best" in heart
    gears = _section(_paragraph("Show how the gears in a gearbox work"))
    iso = next(line for line in gears.splitlines() if line.startswith("   - iso-scene "))
    assert "arrow|box|chain|cone|cylinder|disc|gear|plate|rod|slab|sphere|tube" in iso
    assert "flow|link|pulse|slide|spin|swing" in iso and "ghost" in iso
    assert "around [id,id]" in iso and "[min,max]" in iso
    assert "no braces, no functions" in iso and "12-30 parts" in iso
    assert "spend no parts on bolts" in iso and "short ids" in iso
    assert "an illustration stays for flat diagrams" in iso
    example = widget_relevance.ISO_SCENE_EXAMPLE
    compact = json.dumps(example, separators=(",", ":"))
    assert compact in iso
    card = {"ui": example, "state": {}}
    assert render_card(json.dumps(card), [], profile=RIPPLE_PROFILE) is not None


@pytest.mark.parametrize(
    ("message", "fid"),
    [
        ("How does a rocket engine work?", "rocket-engine"),
        ("How does a fusion reactor work?", "tokamak"),
        ("How does CERN's collider work?", "particle-collider"),
        ("How do bike gears work?", "bike-gears"),
    ],
)
def test_a_figure_subject_gets_its_figure_first_with_its_anchors_alone(message, fid):
    """The figure ranks first (ahead of iso-scene, which "engine" and "gears" also
    hit), and only the matched figure carries its sliders and anchors; the rest are
    ids and titles, so seven figures do not cost seven anchor lists every turn.
    THE MUTATION THAT BREAKS THIS: drop the figure-first move in pick_widgets, or
    list every figure in full."""
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_FIGURES

    assert widget_relevance.pick_widgets(message)[0] == "figure"
    line = widget_relevance.full_line("figure", message)
    for other, figure in RIPPLE_FIGURES.items():
        full = ",".join(figure["anchors"])
        assert (full in line) == (other == fid), other
        if other != fid:
            assert f"{other} ({figure['title']})" in line
    assert line in _section(_paragraph(message))


def test_a_message_naming_two_figures_lists_both_in_full():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_FIGURES

    line = widget_relevance.full_line(
        "figure", "How is a fission reactor different from a fusion reactor?"
    )
    for fid in ("fission-reactor", "tokamak"):
        assert ",".join(RIPPLE_FIGURES[fid]["anchors"]) in line
    assert ",".join(RIPPLE_FIGURES["heart"]["anchors"]) not in line


def test_a_figure_an_earlier_card_used_stays_in_full():
    """THE MUTATION THAT BREAKS THIS: drop earlier_figures from _figure_line."""
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_FIGURES

    card = {"ui": {"type": "figure", "props": {"figure": "heart"}}, "state": {}}
    history = [
        {"role": "user", "content": "Show me the heart"},
        {"role": "assistant", "content": "```pawbar-card\n" + json.dumps(card) + "\n```"},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "Any time."},
    ]
    message = "Now how does a rocket engine work?"
    line = widget_relevance.full_line("figure", message, history)
    for fid in ("heart", "rocket-engine"):
        assert ",".join(RIPPLE_FIGURES[fid]["anchors"]) in line
    assert line in _section(_paragraph(message, history))


def test_the_overlay_holds_only_what_the_manifest_does_not_ship():
    """Keywords and hints come from the manifest's intents; the local overlay keeps
    extra keywords, figure's per-figure keywords and curated lines, never a keyword
    the manifest already carries for that widget."""
    manifest = {i["widget"]: i for i in RIPPLE_PROFILE.manifest["intents"]}
    for name, entry in widget_relevance.OVERLAY.items():
        assert set(entry) <= {"intents", "figures", "line"}, name
        ask = set(manifest.get(name, {}).get("ask", ()))
        assert not ask & set(entry.get("intents", ())), name


def test_a_widget_out_of_the_overlay_still_matches_through_the_manifest():
    """THE MUTATION THAT BREAKS THIS: build INTENTS from the overlay alone, or drop
    the overlay's extra keywords in the merge."""
    assert "menu-order" not in widget_relevance.OVERLAY
    assert "menu-order" in widget_relevance.pick_widgets("I want a burger")
    intents = RIPPLE_PROFILE.manifest["intents"]
    alone = widget_relevance.intents_table(intents, {})
    assert "burger" in alone["menu-order"]["intents"]
    assert alone["growth-projection"]["hint"].startswith("It has its own deposit")
    merged = widget_relevance.intents_table(
        intents, {"menu-order": {"intents": ["burger", "kebab"]}, "map": {"line": "x"}}
    )
    assert merged["menu-order"]["intents"].count("burger") == 1
    assert merged["menu-order"]["intents"][-1] == "kebab"
    assert merged["map"]["line"] == "x" and "directions" in merged["map"]["intents"]


@pytest.mark.parametrize(
    "rule_id",
    [
        "keys-in-order",
        "bind-derive",
        "real-actions",
        "same-site-links",
        "lists-fresh",
        "numbers-in-stat",
        "reads-at-phone",
        "fictional-names",
    ],
)
def test_a_rule_that_repeats_a_craft_rule_leads_with_the_manifest_text(rule_id):
    """The authoring rules that duplicate ripple's craft rules say them in the
    manifest's words, so one rule source feeds the prompt.
    THE MUTATION THAT BREAKS THIS: drop the manifest text from _craft."""
    from pocketpaw_ee.paw_bar.card_spec import CRAFT_RULES
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_RULES

    craft = {r["id"]: r for r in RIPPLE_PROFILE.manifest["craft"]["rules"]}
    assert CRAFT_RULES[rule_id] == craft[rule_id]["text"]
    lead = f"   - {craft[rule_id]['text']} "
    assert sum(line.startswith(lead) for line in _RIPPLE_RULES) == 1
    assert lead in _paragraph()


def test_every_craft_rule_the_prompt_cites_is_in_the_manifest():
    """concierge_runtime reads CRAFT_RULES at import, so a re-vendor that drops a
    cited id would break the import; this names the missing id instead. It reads
    the source, not the module, and the manifest file, not card_spec.
    THE MUTATION THAT BREAKS THIS: cite an id the manifest does not have."""
    from pathlib import Path

    import pocketpaw_ee.paw_bar as paw_bar

    here = Path(paw_bar.__file__).parent
    source = (here / "concierge_runtime.py").read_text()
    cited = set(re.findall(r'_craft\(\s*"([\w-]+)"', source))
    cited |= set(re.findall(r"CRAFT_RULES\[[\"']([\w-]+)[\"']\]", source))
    manifest = json.loads((here / "ripple-manifest.json").read_text())
    known = {r["id"] for r in manifest["craft"]["rules"]}
    assert {"reads-at-phone", "tile-rows-wrap", "inputs-full-row"} <= cited
    assert cited <= known, sorted(cited - known)


def test_every_figure_has_its_own_intents():
    """A figures key that is not a catalog id would steer nothing."""
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_FIGURES

    assert list(widget_relevance.INTENTS["figure"]["figures"]) == list(RIPPLE_FIGURES)


@pytest.mark.parametrize(
    "message",
    [
        "How does a pulley system make lifting easier?",
        "How does a car jack work?",
        "Show how a crane lifts a load",
        "How does a lever work?",
        "Show how a lock works",
        "How does a clock work?",
        "How does a hand water pump work?",
    ],
)
def test_a_how_does_it_work_question_about_a_machine_gets_the_drawing(message):
    """A live Haiku eval answered the pulley question with a number-input calculator.
    The drawing must be offered for every such question, and its hint must say the
    card is the drawing, a calculator only after it and only when asked.
    THE MUTATION THAT BREAKS THIS: drop the machine intents or the never-a-calculator
    sentence from the manifest's iso-scene intent."""
    assert "iso-scene" in widget_relevance.pick_widgets(message)
    iso = widget_relevance.full_line("iso-scene")
    assert "never a calculator, form or number inputs" in iso
    assert "only if they ask for one" in iso


def test_the_iso_scene_example_is_a_rich_compact_machine():
    """The example is the shape the model copies: a real machine of 12-30 parts with
    a frame, moving parts tied to a param, connectors, 2-3 readouts and 3-6 labels,
    written without restating ripple's defaults so the model's card stays short.
    THE MUTATION THAT BREAKS THIS: swap the example back to the two-gear card."""
    props = widget_relevance.ISO_SCENE_EXAMPLE["props"]
    parts, motions = props["parts"], props["motions"]
    assert 12 <= len(parts) <= 30
    kinds = {p["kind"] for p in parts}
    assert {"slab", "rod", "tube"} <= kinds
    assert any(p.get("ghost") for p in parts)  # a housing the inside shows through
    keys = {p["key"] for p in props["params"]}
    assert 1 <= len(keys) <= 3 and len(motions) >= 3
    assert all(any(k in str(m["speed"]) for k in keys) for m in motions)
    assert {m["kind"] for m in motions} >= {"swing", "slide", "flow"}
    assert 2 <= len(props["readouts"]) <= 3 and 3 <= len(props["labels"]) <= 6
    for p in parts:
        assert len(p["id"]) <= 6
        assert p.get("color") != "neutral" and p.get("axis") != "z"
        assert not (p["kind"] in ("rod", "gear") and p.get("color") == "metal")
        assert "at" not in p or p["kind"] not in ("rod", "arrow", "tube")
        for v in (p.get("at"), p.get("from"), p.get("to"), *(p.get("path") or [])):
            assert v is None or all(isinstance(c, int) for c in v)
    assert len(json.dumps(widget_relevance.ISO_SCENE_EXAMPLE, separators=(",", ":"))) < 2_000


def test_the_core_widgets_carry_their_steering_hints_on_every_turn():
    """stat and table are core lines, never in the per-turn section, so their hints
    ride the core. THE MUTATION THAT BREAKS THIS: drop the hint from _hinted."""
    core = _paragraph()
    stat = next(line for line in core.splitlines() if line.startswith("   - stat "))
    assert "round numbers before writing them" in stat.lower()
    table = next(line for line in core.splitlines() if line.startswith("   - table "))
    assert "short phrases" in table
    assert "short phrases" not in _section(_paragraph("review this NDA as a table"))


_PICKED = "I'd like to book Dental cleaning on Tue, Oct 20 at 10:00 AM."
_NOTE = "already picked and confirmed this slot on the card"


@pytest.mark.parametrize(
    "message",
    [
        _PICKED,
        "I'd like to book Teeth cleaning on Tue 14 Oct at 10:30 (3 people). Note: first visit",
        "I\u2019d like to book Haircut at 4:00 PM.",
    ],
)
def test_a_picked_slot_turn_gets_a_text_only_instruction_and_no_card_grammar(message):
    """The landing sends this when a visitor confirms a slot on a booking card the
    store does not back; the concierge answered with the same booking card again,
    even with a note telling it not to. That turn's cards paragraph is now the
    text-only instruction alone. THE MUTATION THAT BREAKS THIS: drop the
    picked-slot early return from _cards_paragraph."""
    text = _paragraph(message)
    assert _NOTE in text and "never send them back to the card" in text
    assert "a reminder note" in text
    for card_grammar in (_SECTION, "Every widget by category", '{"ui"', "   - stat ", "   - "):
        assert card_grammar not in text
    assert "Dental" not in text and "Teeth" not in text and "Haircut" not in text


@pytest.mark.parametrize(
    "message",
    [
        "Book a dentist appointment",
        "book a dentist appointment",
        "I'd like to book",
        "I'd like to book a table at Nobu",
    ],
)
def test_a_booking_request_gets_no_picked_slot_note(message):
    assert _NOTE not in _paragraph(message)


def test_the_illustration_rule_asks_for_a_compact_svg_and_a_node_level_on_select():
    """The bike gears turn: the first SVG was cut off mid-JSON and the restart was
    refused for an on_select written inside the illustration's props."""
    core = _paragraph()
    rule = next(line for line in core.splitlines() if "add an illustration" in line)
    assert "fewer than about 40 elements" in rule
    assert "on_select on the node" in rule
    assert "24,000" not in rule and "400 elements" not in rule


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


# The chat prompt as it stood before its intents and craft lines came from the
# ripple manifest: the net prompt may not grow past it (build-loop GC 9), and
# the worst case keeps the 800-char margin under the 32,000 cap.
_CORE_CHARS = 18_630
_WORST_CHARS = 31_056


def test_core_plus_the_worst_case_section_stays_under_the_cap():
    """The fixed core is at most _CORE_CHARS; the worst per-turn section is the
    MAX_PICKED longest candidate lines, figure's at its longest (MAX_FULL_FIGURES
    figures in full, the rest ids and titles), so the paragraph is no larger than
    _WORST_CHARS and stays 800 chars under the 32,000 cap. THE MUTATION THAT
    BREAKS THIS: lengthen any rule or hint the prompt carries."""
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_FIGURES

    core = _paragraph()
    every_figure = " ".join(fid.replace("-", " ") for fid in RIPPLE_FIGURES)
    longest = sorted(
        (
            len(widget_relevance.full_line(n, every_figure if n == "figure" else "")) + 4
            for n in widget_relevance.CANDIDATES
        ),
        reverse=True,
    )
    worst = len(core) + 1 + len(_SECTION) + sum(longest[: widget_relevance.MAX_PICKED])
    assert len(core) <= _CORE_CHARS
    assert worst <= _WORST_CHARS < 32_000 - 800
    typical = sorted(len(_paragraph(m)) for m, _ in _CHIPS)[len(_CHIPS) // 2]
    assert len(core) < typical < worst


def test_the_free_model_eval_misses_are_spelled_out():
    """The props DeepSeek guessed wrong in the 2026-10-19 free-model eval: wind is
    text, a habit icon is one of the manifest's keys (never an emoji), an if
    condition sits whole in one brace pair, money takes the ask's currency."""
    weather = widget_relevance.full_line("weather-forecast")
    assert 'wind?: text like "14 km/h"' in weather
    habit = widget_relevance.full_line("habit-tracker")
    manifest = next(w for w in RIPPLE_PROFILE.manifest["widgets"] if w["type"] == "habit-tracker")
    keys = set(re.findall(r'"([a-z-]+)"', manifest["props"]["habits"]["type"]))
    named = re.search(r"icon is ([a-z|-]+) or left out, never an emoji", habit)
    assert named is not None and set(named.group(1).split("|")) == keys
    core = _paragraph()
    assert "one brace pair holds the whole expression" in core
    assert "\"{state.cycle === 'yearly'}\"" in core
    assert "currency? (the ask's ISO code, e.g. EUR;" in core
