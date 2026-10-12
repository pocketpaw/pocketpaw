# The ripple concierge's per-turn widget picker. The cards paragraph has a fixed
# core (rules, the layout atoms the rules lean on, and a names-only index of every
# widget) and a per-turn section with the full detail line of the few widgets this
# visitor message needs. This module picks those widgets and renders both widget
# parts; concierge_runtime._cards_paragraph stitches them around the rules.
#
# Picking is deterministic keyword overlap, no model call: the visitor message
# (weight 2) and the last two history turns (weight 1) against each widget's name,
# aliases and name parts, its intent keywords from ripple_widget_intents.json (kept
# here so a new steering keyword does not wait on a ripple release), and its
# description words. A table entry may also carry a ``hint`` (steering appended to
# the widget's line) or a ``line`` (a curated line in its place). Widgets a card
# earlier in the conversation used are always kept, so an edit to that card still
# has its props.
#
# Invariants: same input, same output (sorted, no set iteration in output); only
# manifest and intents-table text reaches the prompt, never visitor text; at most
# MAX_PICKED widgets, so the worst case is bounded (the cap test sums it).
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pocketpaw_ee.paw_bar.card_spec import (
    RIPPLE_ALIASES,
    RIPPLE_DATA_WIDGETS,
    RIPPLE_MODELS,
    RIPPLE_PLAY_WIDGETS,
    RIPPLE_PROFILE,
    _widget_line,
)

INTENTS: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("ripple_widget_intents.json").read_text(encoding="utf-8")
)
TOP_K = 8  # widgets picked by score
MAX_EARLIER = 4  # widgets kept because an earlier card used them
MAX_PICKED = TOP_K + MAX_EARLIER
_HISTORY_TURNS = 2

# Always listed with full lines: the layout atoms and inputs the authoring rules
# lean on (the profile's detailed set less the data and play widgets).
CORE: frozenset[str] = (
    (RIPPLE_PROFILE.detailed or frozenset())
    - frozenset(RIPPLE_DATA_WIDGETS)
    - frozenset(RIPPLE_PLAY_WIDGETS)
)
CANDIDATES: tuple[str, ...] = tuple(sorted(RIPPLE_PROFILE.widget_types - CORE))

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "the and for with that this from your you are can use one each into when its "
    "not but any all has have was will what which who how why where them they their "
    "then than more most only also make show give like want need some such use used "
    "item items list value values data card cards widget widgets default optional "
    "string number boolean".split()
)
_TYPE_IN_CARD = re.compile(r'"type"\s*:\s*"([a-z0-9-]+)"')


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if len(w) > 2 or w in ("3d", "vs")]


def _phrase(text: str) -> str:
    """Lowercased text as space-joined stemmed words, padded for whole-word finds."""
    return " " + " ".join(_words(text)) + " "


def _keywords(widget: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """(phrases, name parts, description words) for one widget. Phrases are its
    whole name, its aliases and its intents; a lone part of a hyphenated name
    ("table" of pricing-table) counts for less, so generic parts add noise only."""
    name = widget["type"]
    aliases = [a for a, target in RIPPLE_ALIASES.items() if target == name]
    phrases = {" ".join(_words(n.replace("-", " "))) for n in [name, *aliases]}
    phrases |= {" ".join(_words(i)) for i in INTENTS.get(name, {}).get("intents", ())}
    if name == "model-viewer":
        phrases |= {" ".join(_words(k)) for k in RIPPLE_MODELS}
    parts = {_stem(p) for n in [name, *aliases] if "-" in n for p in n.split("-")}
    desc = {w for w in _words(str(widget.get("description", ""))) if w not in _STOP}
    return phrases - {""}, parts - {""}, desc


_WIDGETS: dict[str, dict[str, Any]] = {
    w["type"]: w for w in RIPPLE_PROFILE.manifest["widgets"] if w["type"] in CANDIDATES
}
_KEYWORDS = {name: _keywords(w) for name, w in _WIDGETS.items()}


def _score(name: str, text: str, words: set[str]) -> float:
    phrases, parts, desc = _KEYWORDS[name]
    score = 3.0 * sum(1 for phrase in phrases if f" {phrase} " in text)
    score += 1.0 * len(parts & words)
    score += 0.5 * len(desc & words)
    return score


def _history_texts(history: Sequence[dict[str, Any]]) -> list[str]:
    return [str(t.get("content") or "") for t in list(history)[-_HISTORY_TURNS:]]


def earlier_widgets(history: Sequence[dict[str, Any]]) -> list[str]:
    """Candidate widgets a card in this conversation used, most recent first."""
    seen: list[str] = []
    for turn in reversed(list(history)):
        for kind in reversed(_TYPE_IN_CARD.findall(str(turn.get("content") or ""))):
            kind = RIPPLE_ALIASES.get(kind, kind)
            if kind in _WIDGETS and kind not in seen:
                seen.append(kind)
    return seen[:MAX_EARLIER]


def pick_widgets(message: str, history: Sequence[dict[str, Any]] = ()) -> list[str]:
    """The widgets whose full lines this turn gets: earlier-used ones first, then
    up to TOP_K by score (message counts double), ties broken by name."""
    now, past = _phrase(message), _phrase(" ".join(_history_texts(history)))
    now_words, past_words = set(now.split()), set(past.split())
    scored = []
    for name in CANDIDATES:
        score = 2 * _score(name, now, now_words) + _score(name, past, past_words)
        if score >= 3:  # one name or intent hit, or several description words
            scored.append((-score, name))
    earlier = earlier_widgets(history)
    top = [name for _, name in sorted(scored) if name not in earlier][:TOP_K]
    return earlier + top


def full_line(name: str) -> str:
    """A widget's full manifest line (typed for data and play widgets) plus its
    ``hint``, or the intents table's own ``line`` instead (map: its refused tile
    props stay out). model-viewer's line is written here: the model library's ids
    and anchors, and none of the URL props a card may not use."""
    if name == "model-viewer":
        return (
            "- model-viewer {asset, annotations?: [{anchor, label?, note?}], focus?, "
            "on_select?}: to show, explore or label a thing in 3D. asset is one of these "
            "ids, each anchor and focus one of its names, up to 12 annotations with your "
            "own plain label and note (an illustration's caps); never a src, poster or "
            "URL. " + " ".join(f"{k}({','.join(v['anchors'])})" for k, v in RIPPLE_MODELS.items())
        )
    entry = INTENTS.get(name, {})
    if entry.get("line"):
        return f"- {name}: {entry['line']}"
    typed = RIPPLE_PROFILE.typed
    widget = _WIDGETS[name]
    line = (
        _widget_line(widget, typed=True, shown=typed[name])
        if name in typed
        else _widget_line(widget)
    )
    return f"{line} {entry['hint']}" if entry.get("hint") else line


def core_lines() -> list[str]:
    """The always-on full lines, in manifest order."""
    typed = RIPPLE_PROFILE.typed
    return [
        _widget_line(w, typed=True, shown=typed[w["type"]])
        if w["type"] in typed
        else _widget_line(w)
        for w in RIPPLE_PROFILE.manifest["widgets"]
        if w["type"] in CORE
    ]


def index_lines() -> list[str]:
    """Every widget's name, grouped by category (both sorted)."""
    groups: dict[str, list[str]] = {}
    for w in RIPPLE_PROFILE.manifest["widgets"]:
        if w["type"] in RIPPLE_PROFILE.widget_types:
            groups.setdefault(str(w.get("category") or "other"), []).append(w["type"])
    return [f"- {cat}: {', '.join(sorted(names))}" for cat, names in sorted(groups.items())]
