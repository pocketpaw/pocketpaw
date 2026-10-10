# tests/cloud/test_paw_bar_play_widgets.py: ripple's games, habit tracker, focus timer
# and illustration notes on the ripple concierge profile.
#
# memory-match, word-guess, quiz, board-game, habit-tracker and focus-timer hold only
# literal data: plain text, finite numbers, the caps in card_spec, and no handler but a
# game's ``on_complete``. A node written under an alias ripple's chat allowlist refuses
# (``trivia``, ``pomodoro``, ``tic-tac-toe``...) is renamed by the lift, a board-game
# alias filling a missing ``game``; a data row by such a name stays data. An
# illustration may carry up to 8 ``annotations``, each pinned on an svg id the widget's
# rebuild keeps (``svg_ids``) or on a point, and a node-level ``on_select`` that may
# ask; a game's on_complete never may (a quiz timer can end a run unattended), and
# annotations are judged on the final card, never mid-stream. The annotation refusal
# table is ported from ripple feat/play-wave packages/svelte/src/lib/security/
# illustration-annotations.test.ts (ripple has no annotation parity fixture). Also
# pinned: the typed prompt lines, the rule that maps a game and habit tracking to these
# widgets, and the flat-prop lift for each.
# Mutation plan: tests/mutations/concierge_play.json.

from __future__ import annotations

import copy
import json

import pytest

SVG = (
    "<svg viewBox='0 0 100 100'><rect id='box' width='10' height='10'/>"
    "<filter id='dropped'><rect id='inside'/></filter>"
    "<g id='group'><circle id='dot' r='2'/></g>"
    "<animate id='bare' dur='1s'/>"
    "<animateMotion id='motion' dur='1s' path='M0 0L1 1'/></svg>"
)
NOTE = {"id": "a", "label": "Box", "note": "A box.", "target": "box"}


def _ripple(ui: dict, state: dict | None = None) -> str | None:
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    return render_card(json.dumps({"ui": ui, "state": state or {}}), [], profile=RIPPLE_PROFILE)


def _sent(ui: dict) -> dict:
    out = _ripple(ui)
    assert out is not None
    return json.loads(out.split("\n", 1)[1].rsplit("\n", 1)[0])["ui"]


def _memory(**props) -> dict:
    base = {
        "title": "Spanish animals",
        "pairs": [
            {"id": "dog", "a": "perro", "b": "dog"},
            {"id": "cat", "a": "\U0001f431", "b": "icon:fish"},
        ],
        "columns": 4,
        "time_limit_s": 90,
    }
    return {"type": "memory-match", "props": {**base, **props}, "bind": "{state.game}"}


def _word(**props) -> dict:
    base = {"title": "Kitchen words", "answer": "Bread", "hint": "Baked", "max_guesses": 6}
    return {"type": "word-guess", "props": {**base, **props}, "bind": "{state.game}"}


def _question(**fields) -> dict:
    base = {"id": "q1", "prompt": "Closest planet to the sun?", "choices": ["Venus", "Mercury"]}
    return {**base, "answer": 1, "why": "It orbits nearest.", **fields}


def _quiz(**props) -> dict:
    base = {
        "title": "Space trivia",
        "topic": "Astronomy",
        "questions": [_question()],
        "seconds_per_question": 20,
        "shuffle_choices": True,
    }
    return {"type": "quiz", "props": {**base, **props}, "bind": "{state.quiz}"}


def _habits(**props) -> dict:
    base = {
        "title": "My week",
        "habits": [
            {"id": "read", "name": "Read", "icon": "read", "target_per_week": 5},
            {"id": "walk", "name": "Walk", "target_per_week": 7},
        ],
        "week_start": "sun",
        "weeks": 2,
        "seed": {"read": [0, 1, 27], "walk": []},
    }
    return {"type": "habit-tracker", "props": {**base, **props}, "bind": "{state.habits}"}


def _focus(**props) -> dict:
    base = {
        "title": "Deep work",
        "focus_min": 50,
        "short_break_min": 10,
        "long_break_min": 30,
        "rounds_before_long": 3,
        "goal_rounds": 6,
        "task": "Draft the report",
        "auto_start_next": True,
    }
    return {"type": "focus-timer", "props": {**base, **props}, "bind": "{state.focus}"}


def _board(**props) -> dict:
    base = {
        "game": "tic-tac-toe",
        "title": "Beat me",
        "player": "O",
        "first": "computer",
        "difficulty": "hard",
        "best_of": 3,
    }
    return {"type": "board-game", "props": {**base, **props}, "bind": "{state.game}"}


def _illustration(annotations, svg: str = SVG, **node) -> dict:
    props = {"svg": svg, "title": "A box", "annotations": annotations}
    return {"type": "illustration", "props": props, **node}


DONE = {"action": "toast", "message": "Well played"}
ASK = {"action": "emit", "target": "ask", "value": {"text": "Tell me about the box"}}


# --------------------------------------------------------------------------- #
# Good cards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "ui",
    [
        {**_memory(), "on_complete": DONE},
        {**_word(allow_any_word=False), "on_complete": [DONE]},
        {**_quiz(), "on_complete": DONE},
        _quiz(questions=[_question(image="/img/planet.png")] * 12),
        _habits(),
        _focus(),
        {"type": "focus-timer", "bind": "{state.focus}"},
        _focus(focus_min=1, short_break_min=60, long_break_min=1, goal_rounds=24, task="x" * 120),
        {**_board(), "on_complete": DONE},
        _board(game="connect-four", player="yellow", best_of=5, difficulty="easy"),
        {"type": "board-game", "props": {"game": "connect-four"}},
        _memory(pairs=[{"a": str(i), "b": str(i)} for i in range(12)]),
        _illustration([NOTE, {"id": "b", "label": "Corner", "note": "", "at": [0, 100.5]}]),
        _illustration([{**NOTE, "target": "group"}], on_select=ASK),
        _illustration([{**NOTE, "id": "m", "target": "motion"}, {**NOTE, "target": "dot"}]),
        _illustration(None),
        _illustration([]),
    ],
)
def test_each_play_card_passes(ui):
    assert _ripple(ui) is not None


def test_the_cards_go_out_as_written():
    for ui in (_memory(), _word(), _quiz(), _habits(), _focus(), _board(), _illustration([NOTE])):
        assert _sent(copy.deepcopy(ui)) == ui


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

_PAIR = {"id": "x", "a": "uno", "b": "one"}
_HABIT = {"id": "h", "name": "Run", "target_per_week": 3}

REFUSED: list[tuple[str, dict]] = [
    # memory-match
    ("one pair", _memory(pairs=[_PAIR])),
    ("13 pairs", _memory(pairs=[_PAIR] * 13)),
    ("pairs from state", _memory(pairs="{state.pairs}")),
    ("a pair without b", _memory(pairs=[_PAIR, {"a": "dos"}])),
    ("a numeric side", _memory(pairs=[_PAIR, {"a": 2, "b": "two"}])),
    ("an expression side", _memory(pairs=[_PAIR, {"a": "{state.x}", "b": "two"}])),
    ("a pair that is a string", _memory(pairs=[_PAIR, "dos"])),
    ("a pair id that is a number", _memory(pairs=[_PAIR, {**_PAIR, "id": 3}])),
    ("columns as text", _memory(columns="4")),
    ("an infinite time limit", _memory(time_limit_s=float("inf"))),
    ("a memory title expression", _memory(title="{state.t}")),
    ("an on_click on memory-match", {**_memory(), "on_click": DONE}),
    ("a memory on_complete that asks", {**_memory(), "on_complete": ASK}),
    ("a memory on_complete that navigates", {**_memory(), "on_complete": {"action": "navigate"}}),
    ("a memory on_complete from state", {**_memory(), "on_complete": "{state.h}"}),
    # word-guess
    ("a 3-letter answer", _word(answer="Pie")),
    ("an 8-letter answer", _word(answer="Pancakes")),
    ("an answer with a digit", _word(answer="Br3ad")),
    ("an accented answer", _word(answer="Crème")),
    ("an answer from state", _word(answer="{state.word}")),
    ("no answer", {"type": "word-guess", "props": {"hint": "x"}}),
    ("zero guesses", _word(max_guesses=0)),
    ("eleven guesses", _word(max_guesses=11)),
    ("fractional guesses", _word(max_guesses=5.5)),
    ("a bool max_guesses", _word(max_guesses=True)),
    ("allow_any_word as text", _word(allow_any_word="yes")),
    ("a hint expression", _word(hint="{state.hint}")),
    ("an on_change on word-guess", {**_word(), "on_change": DONE}),
    ("a word-guess on_complete that asks", {**_word(), "on_complete": ASK}),
    # quiz
    ("no questions", _quiz(questions=[])),
    ("13 questions", _quiz(questions=[_question()] * 13)),
    ("questions from state", _quiz(questions="{state.q}")),
    ("a question that is text", _quiz(questions=["What?"])),
    ("no prompt", _quiz(questions=[_question(prompt=None)])),
    ("a prompt expression", _quiz(questions=[_question(prompt="{state.p}")])),
    ("one choice", _quiz(questions=[_question(choices=["Venus"], answer=0)])),
    ("six choices", _quiz(questions=[_question(choices=list("abcdef"))])),
    ("a numeric choice", _quiz(questions=[_question(choices=["a", 2])])),
    ("a choice expression", _quiz(questions=[_question(choices=["a", "{state.c}"])])),
    ("an answer past the choices", _quiz(questions=[_question(answer=2)])),
    ("a negative answer", _quiz(questions=[_question(answer=-1)])),
    ("a fractional answer", _quiz(questions=[_question(answer=0.5)])),
    ("a bool answer", _quiz(questions=[_question(answer=True)])),
    ("an answer as text", _quiz(questions=[_question(answer="1")])),
    ("a why expression", _quiz(questions=[_question(why="{state.w}")])),
    ("an off-site image", _quiz(questions=[_question(image="https://cdn.example.com/a.png")])),
    ("a script image", _quiz(questions=[_question(image="javascript:alert(1)")])),
    ("an image expression", _quiz(questions=[_question(image="{state.img}")])),
    ("a numeric image", _quiz(questions=[_question(image=3)])),
    ("a 2-second timer", _quiz(seconds_per_question=2)),
    ("a 601-second timer", _quiz(seconds_per_question=601)),
    ("shuffle as text", _quiz(shuffle_choices="true")),
    ("a topic expression", _quiz(topic="{state.t}")),
    ("an on_select on quiz", {**_quiz(), "on_select": DONE}),
    # A quiz timer can end a run unattended: on_complete never asks, alone or in a list.
    ("a quiz on_complete that asks", {**_quiz(), "on_complete": ASK}),
    ("a quiz on_complete list that asks", {**_quiz(), "on_complete": [DONE, ASK]}),
    (
        "a quiz on_complete flow that asks",
        {**_quiz(), "on_complete": {"action": "flow", "steps": [ASK]}},
    ),
    # habit-tracker
    ("no habits", _habits(habits=[], seed=None)),
    ("nine habits", _habits(habits=[{**_HABIT, "id": f"h{i}"} for i in range(9)], seed=None)),
    ("habits from state", _habits(habits="{state.h}", seed=None)),
    ("a habit without an id", _habits(habits=[{"name": "Run", "target_per_week": 3}], seed=None)),
    ("a repeated habit id", _habits(habits=[_HABIT, _HABIT], seed=None)),
    ("a habit name expression", _habits(habits=[{**_HABIT, "name": "{state.n}"}], seed=None)),
    (
        "a habit icon URL",
        _habits(habits=[{**_HABIT, "icon": "https://x.example/i.png"}], seed=None),
    ),
    ("a habit icon too long", _habits(habits=[{**_HABIT, "icon": "a" * 25}], seed=None)),
    ("a target of 0", _habits(habits=[{**_HABIT, "target_per_week": 0}], seed=None)),
    ("a target of 8", _habits(habits=[{**_HABIT, "target_per_week": 8}], seed=None)),
    ("no target", _habits(habits=[{"id": "h", "name": "Run"}], seed=None)),
    ("a week starting on Monday by name", _habits(week_start="monday")),
    ("five weeks", _habits(weeks=5)),
    ("a seed for an unknown habit", _habits(seed={"swim": [0]})),
    ("a seed day of 28", _habits(seed={"read": [28]})),
    ("a negative seed day", _habits(seed={"read": [-1]})),
    ("a fractional seed day", _habits(seed={"read": [1.5]})),
    ("a seed of text", _habits(seed={"read": "0,1"})),
    ("a seed list", _habits(seed=[0, 1])),
    ("a seed too long", _habits(seed={"read": [0] * 29})),
    ("an on_complete on habit-tracker", {**_habits(), "on_complete": DONE}),
    # focus-timer
    ("focus of 0 minutes", _focus(focus_min=0)),
    ("focus of 121 minutes", _focus(focus_min=121)),
    ("focus of 25.5 minutes", _focus(focus_min=25.5)),
    ("focus minutes as text", _focus(focus_min="25")),
    ("focus minutes from state", _focus(focus_min="{state.m}")),
    ("a bool focus", _focus(focus_min=True)),
    ("a 61-minute short break", _focus(short_break_min=61)),
    ("a 0-minute short break", _focus(short_break_min=0)),
    ("a 61-minute long break", _focus(long_break_min=61)),
    ("an infinite long break", _focus(long_break_min=float("inf"))),
    ("13 rounds before long", _focus(rounds_before_long=13)),
    ("0 rounds before long", _focus(rounds_before_long=0)),
    ("a goal of 25 rounds", _focus(goal_rounds=25)),
    ("a goal of 0 rounds", _focus(goal_rounds=0)),
    ("a task over 120", _focus(task="x" * 121)),
    ("a task of emoji over 120 in the browser", _focus(task="\U0001f600" * 61)),
    ("a task expression", _focus(task="{state.task}")),
    ("a numeric task", _focus(task=3)),
    ("a focus title expression", _focus(title="{state.t}")),
    ("auto_start_next as text", _focus(auto_start_next="yes")),
    ("an on_complete on focus-timer", {**_focus(), "on_complete": DONE}),
    ("an on_change on focus-timer", {**_focus(), "on_change": DONE}),
    # board-game
    ("no game", {"type": "board-game", "props": {"difficulty": "easy"}}),
    ("an unknown game", _board(game="chess")),
    ("a game from state", _board(game="{state.g}")),
    ("a game list", _board(game=["tic-tac-toe"])),
    ("red on tic-tac-toe", _board(player="red")),
    ("X on connect-four", _board(game="connect-four", player="X")),
    ("a lowercase mark", _board(player="x")),
    ("a player list", _board(player=["X"])),
    ("first as anyone", _board(first="anyone")),
    ("difficulty impossible", _board(difficulty="impossible")),
    ("best of 2", _board(best_of=2)),
    ("best of 7", _board(best_of=7)),
    ("best of true", _board(best_of=True)),
    ("best of as text", _board(best_of="3")),
    ("a board title expression", _board(title="{state.t}")),
    ("an on_click on board-game", {**_board(), "on_click": DONE}),
    ("a board-game on_complete that asks", {**_board(), "on_complete": ASK}),
    ("a board-game on_complete list that asks", {**_board(), "on_complete": [DONE, ASK]}),
    # illustration
    ("an on_click on an illustration", _illustration([NOTE], on_click=ASK)),
    (
        "an on_select inside props",
        {**_illustration([NOTE]), "props": {**_illustration([NOTE])["props"], "on_select": ASK}},
    ),
    ("a bind on an illustration", _illustration([NOTE], bind="{state.note}")),
    ("an on_select that navigates", _illustration([NOTE], on_select={"action": "navigate"})),
    (
        "an on_select ask with an expression",
        _illustration([NOTE], on_select={**ASK, "value": {"text": "About {state.x}"}}),
    ),
]


@pytest.mark.parametrize("name,ui", REFUSED, ids=[n for n, _ in REFUSED])
def test_each_bad_play_card_is_refused(name, ui):
    assert _ripple(ui) is None, name


# Ported from ripple's illustration-annotations.test.ts.
ANNOTATION_REFUSALS: list[tuple[str, object]] = [
    ("not a list", {"a": NOTE}),
    ("more than 8", [{**NOTE, "id": f"n{i}"} for i in range(9)]),
    ("an entry that is not an object", ["x"]),
    ("no id", [{**NOTE, "id": ""}]),
    ("a blank id", [{**NOTE, "id": "  "}]),
    ("a duplicate id", [NOTE, NOTE]),
    ("no label", [{**NOTE, "label": " "}]),
    ("an overlong label", [{**NOTE, "label": "x" * 41}]),
    ("an overlong emoji label", [{**NOTE, "label": "\U0001f600" * 21}]),
    ("no note", [{"id": "a", "label": "Box", "target": "box"}]),
    ("an overlong note", [{**NOTE, "note": "x" * 281}]),
    ("both target and at", [{**NOTE, "at": [1, 2]}]),
    ("neither target nor at", [{"id": "a", "label": "Box", "note": ""}]),
    ("a null target beside at", [{**NOTE, "target": None, "at": [1, 2]}]),
    ("a non-finite at", [{"id": "a", "label": "Box", "note": "", "at": [1, float("inf")]}]),
    ("a NaN at", [{"id": "a", "label": "Box", "note": "", "at": [float("nan"), 1]}]),
    ("an at with three numbers", [{"id": "a", "label": "Box", "note": "", "at": [1, 2, 3]}]),
    ("a string at", [{"id": "a", "label": "Box", "note": "", "at": ["1", "2"]}]),
    ("a bool at", [{"id": "a", "label": "Box", "note": "", "at": [True, 2]}]),
    ("a target id not in the svg", [{**NOTE, "target": "nope"}]),
    ("a target on an element the rebuild drops", [{**NOTE, "target": "dropped"}]),
    ("a target inside a dropped element", [{**NOTE, "target": "inside"}]),
    ("a target on an animate with no attributeName", [{**NOTE, "target": "bare"}]),
    ("an empty target", [{**NOTE, "target": ""}]),
    ("a null target", [{**NOTE, "target": None}]),
    ("a label expression", [{**NOTE, "label": "{state.l}"}]),
    ("a note expression", [{**NOTE, "note": "See {state.n}"}]),
    ("a numeric note", [{**NOTE, "note": 3}]),
]


@pytest.mark.parametrize(
    "name,annotations", ANNOTATION_REFUSALS, ids=[n for n, _ in ANNOTATION_REFUSALS]
)
def test_each_bad_annotation_set_is_refused(name, annotations):
    assert _ripple(_illustration(annotations)) is None, name


def test_annotation_caps_are_inclusive():
    eight = [{**NOTE, "id": f"n{i}", "label": "x" * 40, "note": "y" * 280} for i in range(8)]
    assert _ripple(_illustration(eight)) is not None


def test_a_hostile_svg_is_refused_before_its_annotations_are_read():
    hostile = "<svg viewBox='0 0 9 9'><script/><rect id='box'/></svg>"
    assert _ripple(_illustration([NOTE], svg=hostile)) is None


def test_svg_ids_are_the_ids_the_rebuild_keeps():
    from pocketpaw_ee.paw_bar.illustration_svg import KEPT_ELEMENTS, svg_ids, svg_violation

    assert svg_violation(SVG) is None
    assert svg_ids(SVG) == {"box", "group", "dot", "motion"}
    rooted = (
        "<svg id='art' xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1 1'>"
        "<set id='s' attributeName='opacity' to='1' dur='1s'/></svg>"
    )
    assert svg_ids(rooted) == {"art", "s"}
    # Mirrors ripple core ILLUSTRATION_ELEMENTS (26 names).
    assert len(KEPT_ELEMENTS) == 26 and "filter" not in KEPT_ELEMENTS


# --------------------------------------------------------------------------- #
# Aliases and the flat-prop lift
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("alias", ["trivia", "trivia-quiz"])
def test_a_quiz_alias_is_checked_and_sent_as_a_quiz(alias):
    good = {**_quiz(), "type": alias}
    assert _sent(good)["type"] == "quiz"
    bad = {**_quiz(questions=[_question(answer=5)]), "type": alias}
    assert _ripple(bad) is None


@pytest.mark.parametrize("alias", ["pomodoro", "pomodoro-timer"])
def test_a_pomodoro_is_checked_and_sent_as_a_focus_timer(alias):
    assert _sent({**_focus(), "type": alias}) == _focus()
    assert _ripple({**_focus(focus_min=0), "type": alias}) is None


@pytest.mark.parametrize("game", ["tic-tac-toe", "connect-four"])
def test_a_board_game_alias_is_sent_as_a_board_game_with_its_game(game):
    sent = _sent({"type": game, "props": {"difficulty": "hard"}, "bind": "{state.g}"})
    assert sent == {
        "type": "board-game",
        "props": {"difficulty": "hard", "game": game},
        "bind": "{state.g}",
    }
    # A flat game is lifted, not overwritten; a given one is kept as written.
    assert _sent({"type": game, "game": "connect-four"})["props"] == {"game": "connect-four"}
    assert _sent({**_board(), "type": game})["props"]["game"] == "tic-tac-toe"


def test_an_alias_game_is_checked_against_the_player():
    assert _ripple({"type": "connect-four", "props": {"player": "X"}}) is None
    assert _ripple({"type": "tic-tac-toe", "props": {"player": "X"}}) is not None
    assert _ripple({"type": "tic-tac-toe", "player": "red"}) is None


@pytest.mark.parametrize(
    "row",
    [
        {"type": "trivia", "label": "Pub quiz"},
        {"type": "tic-tac-toe", "label": "Board games"},
        {"type": "pomodoro", "label": "Focus", "minutes": 25},
    ],
)
def test_a_data_row_named_like_an_alias_stays_data(row):
    ui = {"type": "table", "props": {"rows": [row], "columns": [{"accessorKey": "label"}]}}
    assert _sent(copy.deepcopy(ui))["props"]["rows"] == [row]


@pytest.mark.parametrize(
    "ui,key",
    [
        ({"type": "memory-match", "pairs": _memory()["props"]["pairs"]}, "pairs"),
        ({"type": "word-guess", "answer": "Bread", "max_guesses": 4}, "answer"),
        ({"type": "quiz", "title": "Space", "questions": [_question()]}, "questions"),
        ({"type": "trivia", "questions": [_question()]}, "questions"),
        ({"type": "habit-tracker", "habits": _habits()["props"]["habits"]}, "habits"),
        ({"type": "focus-timer", "focus_min": 45, "task": "Read"}, "focus_min"),
        ({"type": "pomodoro", "focus_min": 45}, "focus_min"),
        ({"type": "board-game", "game": "connect-four", "difficulty": "easy"}, "difficulty"),
        (
            {"type": "illustration", "svg": SVG, "title": "Box", "annotations": [NOTE]},
            "annotations",
        ),
    ],
)
def test_flat_props_are_lifted_on_the_play_widgets(ui, key):
    sent = _sent(copy.deepcopy(ui))
    assert key in sent["props"] and key not in sent


def test_a_lifted_card_is_still_checked():
    assert _ripple({"type": "word-guess", "answer": "No"}) is None
    flat = {
        "type": "illustration",
        "svg": SVG,
        "title": "Box",
        "annotations": [{**NOTE, "target": "dropped"}],
    }
    assert _ripple(flat) is None


# --------------------------------------------------------------------------- #
# The prompt
# --------------------------------------------------------------------------- #


def _listing() -> dict[str, str]:
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, compact_manifest

    lines = compact_manifest(RIPPLE_PROFILE).splitlines()
    return {line[2:].split(" ", 1)[0].rstrip(":"): line for line in lines}


def test_the_play_widgets_have_typed_lines():
    lines = _listing()
    fields = {
        w: lines[w][: lines[w].index("}: ") + 1]
        for w in (
            "memory-match",
            "word-guess",
            "quiz",
            "habit-tracker",
            "focus-timer",
            "board-game",
            "illustration",
        )
    }
    assert fields == {
        "memory-match": "- memory-match {title?, pairs: [{id,a,b}], columns?: number, "
        "time_limit_s?: number}",
        "word-guess": "- word-guess {title?, answer, hint?, max_guesses?: number, "
        "allow_any_word?: boolean}",
        "quiz": "- quiz {title?, topic?, questions: [{id?,prompt,choices:string[],"
        "answer:number,why?,image?}], seconds_per_question?: number, shuffle_choices?: boolean}",
        "habit-tracker": "- habit-tracker {title?, habits: [{id,name,icon?,"
        "target_per_week:number}], week_start?: mon|sun, weeks?: number, "
        "seed?: Record<string,number[]>}",
        "focus-timer": "- focus-timer {title?, focus_min?: number, short_break_min?: number, "
        "long_break_min?: number, rounds_before_long?: number, goal_rounds?: number, task?, "
        "auto_start_next?: boolean}",
        "board-game": "- board-game {game: tic-tac-toe|connect-four, title?, "
        "player?: X|O|red|yellow, first?: player|computer, difficulty?: easy|medium|hard, "
        "best_of?: 1|3|5}",
        "illustration": "- illustration {svg, title, caption?, max_height?, annotations?, "
        "on_select}",
    }
    # Each game's description names its on_complete, so the field list leaves it out.
    for game in ("memory-match", "word-guess", "quiz", "board-game"):
        assert "on_complete" in lines[game], game
    # A pomodoro is a focus-timer now, never a timer.
    assert "omodoro" not in lines["timer"] and "Pomodoro timer" in lines["focus-timer"]
    # A row shape the field list gives is not recapped; one with a note stays.
    assert "habits[{" not in lines["habit-tracker"] and "answer (index)" in lines["quiz"]


def test_the_rules_map_games_habits_and_annotations():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import _RIPPLE_RULES, _cards_paragraph

    rule = next(r for r in _RIPPLE_RULES if "Match the card to the answer" in r)
    assert (
        "A game is a memory-match, word-guess or quiz; tic-tac-toe or connect four "
        "against the computer, a board-game (game, difficulty). Tracking habits is a "
        "habit-tracker; a focus or pomodoro timer, a focus-timer." in rule
    )
    assert "(a converter)" in rule and "a quiz)" not in rule and "is a timer" not in rule
    art = next(r for r in _RIPPLE_RULES if "add an illustration" in r)
    assert "Up to 8 annotations [{id, label (at most 40 chars), note (at most 280), target}]" in art
    assert "give each drawing part an id and point at it" in art
    assert "a game" in _cards_paragraph([], profile=RIPPLE_PROFILE).splitlines()[0]


# --------------------------------------------------------------------------- #
# Streaming: annotations are judged on the final card only
# --------------------------------------------------------------------------- #


def test_annotations_are_not_flagged_while_streaming():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, PartialScan

    good = {"ui": _illustration([NOTE], on_select=ASK), "state": {}}
    # Bad only once whole: the target is not an id the rebuild keeps.
    bad = {"ui": _illustration([{**NOTE, "target": "dropped"}]), "state": {}}
    for card in (good, bad):
        scan = PartialScan(RIPPLE_PROFILE)
        assert not any(scan.feed(ch) for ch in json.dumps(card))
    assert _ripple(good["ui"]) is not None and _ripple(bad["ui"]) is None


def test_a_game_with_a_harder_round_button_may_ask_on_click():
    harder = {"type": "button", "props": {"label": "Harder round"}, "on_click": ASK}
    ui = {"type": "flex", "children": [{**_quiz(), "on_complete": DONE}, harder]}
    assert _ripple(ui) is not None
