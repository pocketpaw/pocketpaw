# The ripple concierge's per-turn widget picker. The cards paragraph has a fixed
# core (rules, the layout atoms the rules lean on, and a names-only index of every
# widget) and a per-turn section with the full detail line of the few widgets this
# visitor message needs. This module picks those widgets and renders both widget
# parts; concierge_runtime._cards_paragraph stitches them around the rules.
#
# Picking is deterministic keyword overlap, no model call: the visitor message
# (weight 2) and the last two history turns (weight 1) against each widget's name,
# aliases and name parts, its intent keywords, and its description words. The
# intents table (``INTENTS``) is the vendored manifest's ``intents`` (ripple's
# craft.ts: ``ask`` keywords and ``hints``) with ripple_widget_intents.json merged
# on top, a local overlay so a new steering keyword does not wait on a ripple
# release: extra ``intents`` keywords per widget, figure's per-figure keywords
# (``figures``) and a curated ``line`` for map, weather-forecast (now.wind is
# text) and habit-tracker (its icon keys, which the manifest line folds away). An entry's
# ``hint`` is steering appended to the widget's line (a core widget's hint rides
# every turn); a ``line`` replaces the manifest line. figure's and iso-scene's
# lines are built here: figure's from the figure catalog, the sliders and anchors
# only of the figures this turn names (each figure's own intents, ``figures`` in
# the overlay) or an earlier card used, at most MAX_FULL_FIGURES, the rest as ids
# and titles; figure ranks ahead of iso-scene when one is named. iso-scene's line
# is its part shape and a hand water pump example card. Widgets a card earlier in
# the conversation used are always kept, so an edit to that card still has its props.
#
# Invariants: same input, same output (sorted, no set iteration in output); only
# manifest and overlay text reaches the prompt, never visitor text; at most
# MAX_PICKED widgets, so the worst case is bounded (the cap test sums it).
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pocketpaw_ee.paw_bar.card_spec import (
    ISO_KINDS,
    ISO_MOTIONS,
    ISO_ROLES,
    RIPPLE_ALIASES,
    RIPPLE_DATA_WIDGETS,
    RIPPLE_FIGURES,
    RIPPLE_MODELS,
    RIPPLE_PLAY_WIDGETS,
    RIPPLE_PROFILE,
    _widget_line,
)


def intents_table(
    manifest_intents: Sequence[dict[str, Any]], overlay: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The manifest's intents as ``{widget: {intents, hint}}`` with ``overlay`` on
    top: its ``intents`` keywords add to the manifest's, any other key replaces."""
    table: dict[str, dict[str, Any]] = {}
    for intent in manifest_intents:
        entry = table.setdefault(intent["widget"], {})
        if intent["ask"]:
            entry["intents"] = list(intent["ask"])
        if intent["hints"]:
            entry["hint"] = " ".join(intent["hints"])
    for name, extra in overlay.items():
        entry = table.setdefault(name, {})
        asks = entry.get("intents", [])
        entry.update(extra)
        entry["intents"] = asks + [k for k in extra.get("intents", ()) if k not in asks]
    return table


OVERLAY: dict[str, dict[str, Any]] = json.loads(
    Path(__file__).with_name("ripple_widget_intents.json").read_text(encoding="utf-8")
)
INTENTS = intents_table(RIPPLE_PROFILE.manifest["intents"], OVERLAY)
TOP_K = 8  # widgets picked by score
MAX_EARLIER = 4  # widgets kept because an earlier card used them
MAX_PICKED = TOP_K + MAX_EARLIER
MAX_FULL_FIGURES = 2  # figures listed with sliders and anchors on one turn
_HISTORY_TURNS = 2
# The iso-scene line's example: a hand water pump of 15 parts (its barrel a ghost so
# the piston shows) written the way the model should write one (short ids,
# integers, no restated defaults: axis z, color neutral, rods and gears metal, no
# ``at`` on a rod or tube so their points are world points). Coordinates follow
# ripple's scene model (core/src/iso/scene.ts): x across, y depth, z up, a rod
# swings about its ``from``. It must pass the ripple checks as a card
# (tests/cloud/test_paw_bar_widget_relevance.py).
ISO_SCENE_EXAMPLE: dict[str, Any] = {
    "type": "iso-scene",
    "bind": "{state.pump}",
    "props": {
        "title": "Hand water pump",
        "params": [
            {"key": "rate", "label": "Strokes", "min": 10, "max": 60, "unit": "/min",
             "value": 30}
        ],
        "parts": [
            {"id": "base", "kind": "slab", "at": [10, 0, -2], "w": 180, "d": 90, "h": 4},
            {"id": "well", "kind": "cylinder", "at": [-66, 30, 4], "r": 10, "h": 8,
             "color": "blue"},
            {"id": "pipe", "kind": "tube", "path": [[-60, 30, 4], [0, 30, 4], [0, 20, 6]],
             "r": 3, "color": "blue"},
            {"id": "plinth", "kind": "box", "at": [0, 0, 6], "w": 40, "d": 40, "h": 12},
            {"id": "barrel", "kind": "cylinder", "at": [0, 0, 40], "r": 13, "h": 56,
             "color": "accent", "ghost": True},
            {"id": "cap", "kind": "disc", "at": [0, 0, 70], "r": 15, "h": 4, "color": "metal"},
            {"id": "post", "kind": "rod", "from": [-15, 0, 70], "to": [-15, 0, 94], "r": 3},
            {"id": "lever", "kind": "rod", "from": [-15, 0, 94], "to": [-90, 0, 108], "r": 3},
            {"id": "nose", "kind": "rod", "from": [-15, 0, 94], "to": [0, 0, 94], "r": 3,
             "on": "lever"},
            {"id": "grip", "kind": "cylinder", "at": [-90, 0, 108], "r": 4, "h": 16,
             "axis": "y", "on": "lever"},
            {"id": "shaft", "kind": "rod", "from": [0, 0, 94], "to": [0, 0, 56], "r": 2},
            {"id": "piston", "kind": "cylinder", "at": [0, 0, 54], "r": 11, "h": 6,
             "color": "metal", "on": "shaft"},
            {"id": "spout", "kind": "tube", "path": [[12, 0, 60], [36, 0, 60], [44, 0, 52]],
             "r": 4, "color": "metal"},
            {"id": "jet", "kind": "tube", "path": [[44, 0, 50], [44, 0, 22]], "r": 2,
             "color": "blue"},
            {"id": "pail", "kind": "cylinder", "at": [44, 0, 10], "r": 14, "h": 20},
        ],
        "motions": [
            {"part": "lever", "kind": "swing", "axis": "y", "speed": "rate / 60", "range": 15},
            {"part": "shaft", "kind": "slide", "speed": "rate / 60", "range": 4, "phase": 0.5},
            {"part": "pipe", "kind": "flow", "speed": "rate / 60"},
            {"part": "spout", "kind": "flow", "speed": "rate / 60"},
            {"part": "jet", "kind": "flow", "speed": "rate / 60"},
        ],
        "readouts": [
            {"label": "Flow", "value": "rate * 0.5", "unit": "L/min", "decimals": 1},
            {"label": "10 L pail in", "value": "20 / rate", "unit": "min", "decimals": 1},
        ],
        "labels": [
            {"part": "lever", "label": "Handle", "note": "lifts the piston"},
            {"part": "piston", "label": "Piston", "note": "one-way valve"},
            {"part": "pipe", "label": "Suction pipe", "note": "from the well"},
            {"part": "spout", "label": "Spout"},
        ],
    },
}  # fmt: skip

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
_FIGURE_IN_CARD = re.compile(r'"figure"\s*:\s*"([a-z0-9-]+)"')


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
    if name == "figure":
        phrases |= {p for fid in RIPPLE_FIGURES for p in _figure_phrases(fid)}
    parts = {_stem(p) for n in [name, *aliases] if "-" in n for p in n.split("-")}
    desc = {w for w in _words(str(widget.get("description", ""))) if w not in _STOP}
    return phrases - {""}, parts - {""}, desc


def _figure_phrases(fid: str) -> set[str]:
    """One catalog figure's phrases: its id and its own intents."""
    own = INTENTS.get("figure", {}).get("figures", {}).get(fid, ())
    return {" ".join(_words(p)) for p in [fid.replace("-", " "), *own]} - {""}


_FIGURE_PHRASES = {fid: _figure_phrases(fid) for fid in RIPPLE_FIGURES}


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


def named_figures(message: str, history: Sequence[dict[str, Any]] = ()) -> list[str]:
    """Catalog figures to list in full: those an earlier card used, most recent
    first, then those the message (weight 2) and last turns name, by score then
    catalog order; at most MAX_FULL_FIGURES."""
    seen: list[str] = []
    for turn in reversed(list(history)):
        for fid in reversed(_FIGURE_IN_CARD.findall(str(turn.get("content") or ""))):
            if fid in RIPPLE_FIGURES and fid not in seen:
                seen.append(fid)
    now, past = _phrase(message), _phrase(" ".join(_history_texts(history)))
    scored = []
    for order, (fid, phrases) in enumerate(_FIGURE_PHRASES.items()):
        score = sum(2 * (f" {p} " in now) + (f" {p} " in past) for p in phrases)
        if score and fid not in seen:
            scored.append((-score, order, fid))
    return (seen + [fid for _, _, fid in sorted(scored)])[:MAX_FULL_FIGURES]


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
    # A named figure is hand-built: it goes ahead of iso-scene, which "engine" or
    # "gears" also hit.
    if "figure" in top and "iso-scene" in top and named_figures(message, history):
        top.remove("figure")
        top.insert(top.index("iso-scene"), "figure")
    return earlier + top


def full_line(name: str, message: str = "", history: Sequence[dict[str, Any]] = ()) -> str:
    """A widget's full manifest line (typed for data and play widgets) plus its
    ``hint``, or the intents table's own ``line`` instead (map: its refused tile
    props stay out). model-viewer's line is written here: the model library's ids
    and anchors, and none of the URL props a card may not use. figure's depends on
    the figures ``message`` and ``history`` name (``named_figures``)."""
    if name == "model-viewer":
        return (
            "- model-viewer {asset, annotations?: [{anchor, label?, note?}], focus?, "
            "on_select?}: to show, explore or label a thing in 3D. asset is one of these "
            "ids, each anchor and focus one of its names, up to 12 annotations with your "
            "own plain label and note (an illustration's caps); never a src, poster or "
            "URL. " + " ".join(f"{k}({','.join(v['anchors'])})" for k, v in RIPPLE_MODELS.items())
        )
    if name == "figure":
        return _hinted(name, _figure_line(named_figures(message, history)))
    if name == "iso-scene":
        return _hinted(name, _iso_scene_line())
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
    return _hinted(name, line)


def _figure_line(full: Sequence[str]) -> str:
    """figure's line from the vendored catalog: the ``full`` figures with their
    sliders and anchors, every other id with its title only."""
    detailed = " ".join(
        f"{fid}({f['title']}; "
        + ", ".join(f"{p['key']} {p['min']}..{p['max']}" for p in f["params"])
        + f"; anchors {','.join(f['anchors'])})"
        for fid, f in RIPPLE_FIGURES.items()
        if fid in full
    )
    others = ", ".join(
        f"{fid} ({f['title']})" for fid, f in RIPPLE_FIGURES.items() if fid not in full
    )
    listed = f"{detailed}. Also (no params or labels): {others}" if detailed else others
    return (
        "- figure {figure, params?, labels?: [{anchor, label?, note?}], caption?}, bind "
        '"{state.<key>}": a hand-built live isometric figure with sliders, readouts and '
        "numbered labels. figure is one of these ids, params a start value per slider "
        "key within its range, up to 10 labels each on one of its anchors with your own "
        f"plain label and note. {listed}."
    )


def _iso_scene_line() -> str:
    """iso-scene's line: the parts, motions and params shape, the caps card_spec
    holds it to, and ``ISO_SCENE_EXAMPLE``."""
    return (
        "- iso-scene {title?, caption?, params?, parts, motions?, readouts?, labels?}, "
        'bind "{state.<key>}": a live isometric drawing of a machine or mechanism that '
        "ripple draws and animates from your parts list (never SVG). parts (up to 150): "
        f"{{id, kind: {'|'.join(sorted(ISO_KINDS))}, at: [x,y,z] (centre; x across, y "
        "depth, z up, the scene about 200 across), w,d,h (slab, box, plate) or r,h "
        "(cylinder, disc, gear with teeth, cone; axis x|y|z, default z) or path "
        "[[x,y,z]...] and r (tube) or from, to and r (rod, arrow; it swings about from) "
        "or around [id,id] of two gears or discs and r? (chain), color?: "
        f"{'|'.join(sorted(ISO_ROLES))} (default neutral, rod and gear metal), on?: a "
        "parent part id it moves with, ghost?: true to see inside}. motions (up to 20): "
        f"{{part, kind: {'|'.join(sorted(ISO_MOTIONS))}, axis?, speed (turns a second), "
        "range? (a number or [min,max]), phase?}; a slide may add driven (the part id "
        "driving it) and throw; a link is {kind: link, from, to}, two part ids it "
        "keeps a rod between. params (up to 6): {key, label, min, max, step?, unit?, value}. "
        "readouts (up to 4): {label, value, unit?, decimals?}. labels (up to 10): "
        "{part, label, note?}. A speed or readout value is a number or a plain "
        "expression over param keys with numbers and + - * / ( ): no braces, no "
        "functions, no other names. Example: "
        + json.dumps(ISO_SCENE_EXAMPLE, separators=(",", ":"))
    )


def _hinted(name: str, line: str) -> str:
    """``line`` plus the widget's ``hint`` from the intents table, if it has one."""
    hint = INTENTS.get(name, {}).get("hint")
    return f"{line} {hint}" if hint else line


def core_lines() -> list[str]:
    """The always-on full lines, in manifest order, each with its ``hint``."""
    typed = RIPPLE_PROFILE.typed
    return [
        _hinted(
            w["type"],
            _widget_line(w, typed=True, shown=typed[w["type"]])
            if w["type"] in typed
            else _widget_line(w),
        )
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
