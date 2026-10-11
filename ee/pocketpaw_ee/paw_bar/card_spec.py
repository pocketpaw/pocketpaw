# ee/pocketpaw_ee/paw_bar/card_spec.py: bound and hydrate the cards a v2 concierge writes.
#
# A v2 reply can carry a ```pawbar-card fence whose JSON ``ui`` is a Ripple node tree,
# drawn on a customer's page, so the server checks it first. Every rule reads a
# ``CardProfile``. ``PAWBAR_PROFILE`` (the default) mirrors paw-bar's lib/spec-card.ts:
# small bounds, the vendored pawbar-manifest.json widgets, the manifest's actions, host
# events limited to add_to_cart / checkout, and the ``send_to_team`` lead form only
# with lead capture on. ``RIPPLE_PROFILE`` (ops sites) takes ripple-manifest.json
# minus deferred widgets and page chrome (its ``illustration`` SVG is held to
# illustration_svg's policy and its annotations to ids the widget keeps, ``bill-split``
# to plain numbers, the games, habit-tracker and focus-timer to literal data (``_PLAY_CHECKS``), a
# button's choice-card ``icon`` and ``description`` to a key and short plain text), and
# is ``strict``:
# ``_check_strict`` walks all of ``ui`` and ``state`` iteratively, holding nodes,
# actions, URLs, CSS, expressions, flow cards and ``ask`` to the rules its docstrings
# name. Any error the walk did not foresee is a logged refusal, never an exception.
# A ripple body that is not JSON only for missing closers, or one surplus closer
# before its state, is repaired once and then checked like any body; a ripple
# node's declared props written flat on the node are moved under ``props``
# (``_lift_flat_props``, which also renames a widget alias, ``RIPPLE_ALIASES``) before the checks,
# and the moved card is the one sent.
#
# Hydration runs after the checks and is not checked again: product data comes only
# from the site catalog, and on the ripple profile ``_fill_store`` fills menu-order,
# booking and comparison-layout nodes from the site's store and attaches the one
# server-wired handler each may fire (a model-written one is refused). ``card_ids``
# and ``has_lead_form`` read a body for the runner; ``PartialScan`` judges a body
# still streaming, flagging only what the finished card is sure to fail.
#
# Both manifests are vendored (``.source`` beside ripple-manifest.json says how);
# the drift tests in tests/cloud/test_paw_bar_concierge_v2_output.py and
# tests/cloud/test_paw_bar_ripple_profile.py pin their hashes, and the shared
# parity fixtures live in tests/fixtures/card_parity/.

from __future__ import annotations

import html
import json
import logging
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pocketpaw_ee.paw_bar.illustration_svg import svg_ids, svg_violation

logger = logging.getLogger(__name__)

MAX_SPEC_CHARS = 32_000
MAX_SPEC_NODES = 80
MAX_SPEC_DEPTH = 8
# Most catalog ids one card may name (a lookup reads no more).
MAX_CARD_IDS = 200
# The only host events a card may emit; SpecCard.svelte ignores every other one.
HOST_EVENTS: tuple[str, ...] = ("add_to_cart", "checkout")

# The built-in lead verb (paw_bar.actions.SEND_TO_TEAM_VERB) and the only fields
# its form may carry; one of LEAD_CONTACT_FIELDS must be among them.
LEAD_VERB = "send_to_team"
LEAD_FIELDS: frozenset[str] = frozenset({"name", "email", "phone", "message"})
LEAD_CONTACT_FIELDS: frozenset[str] = frozenset({"email", "phone"})
# A form field's prefill ``value``, at most this many characters (paw-bar clips
# to the same length; the server refuses rather than clips).
FORM_PREFILL_MAX = 500
# Widgets the vendored manifest documents but the server doesn't back yet
# (``book_slot`` needs the booking slots hydrated). Not offered to the model and
# refused in a card until they are.
DEFERRED_WIDGETS: frozenset[str] = frozenset({"book_slot"})

MANIFEST_PATH = Path(__file__).with_name("pawbar-manifest.json")
MANIFEST: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
WIDGET_TYPES: frozenset[str] = frozenset(w["type"] for w in MANIFEST["widgets"]) - DEFERRED_WIDGETS
SPEC_ACTIONS: frozenset[str] = frozenset(MANIFEST["actions"])


@dataclass(frozen=True)
class CardProfile:
    """What one site's cards may hold. ``detailed`` names the widgets the prompt
    lists with their props (None: all of them; the rest get a one-line summary);
    ``typed`` names the detailed widgets whose props are listed with a short type,
    each with the props to list (None: all of them); ``strict`` runs
    ``_check_strict`` (nodes in props, actions anywhere, URLs) instead of the
    children-only walk; ``url_hosts`` are the https hosts its URLs may name;
    ``host_events`` are the host events its cards may carry. A model may emit only
    the widget's declared ones of ``HOST_EVENTS``; the rest (ripple's ``book``)
    only ever arrive in a handler the server attaches (``SERVER_WIRED``)."""

    name: str
    widget_types: frozenset[str]
    actions: frozenset[str]
    max_nodes: int
    max_depth: int
    max_chars: int
    manifest: dict[str, Any]
    detailed: frozenset[str] | None = None
    typed: dict[str, frozenset[str] | None] = field(default_factory=dict)
    strict: bool = False
    url_hosts: frozenset[str] = frozenset()
    host_events: tuple[str, ...] = HOST_EVENTS


PAWBAR_PROFILE = CardProfile(
    name="pawbar",
    widget_types=WIDGET_TYPES,
    actions=SPEC_ACTIONS,
    max_nodes=MAX_SPEC_NODES,
    max_depth=MAX_SPEC_DEPTH,
    max_chars=MAX_SPEC_CHARS,
    manifest=MANIFEST,
)

RIPPLE_MANIFEST_PATH = Path(__file__).with_name("ripple-manifest.json")
RIPPLE_MANIFEST: dict[str, Any] = json.loads(RIPPLE_MANIFEST_PATH.read_text(encoding="utf-8"))
# Kept out of ripple cards: ``ripple-frame`` mounts a whole nested spec the bounds
# don't see, ``embed`` frames any third-party URL, ``richtext`` renders trusted HTML
# and ``rich-text`` (a Tiptap editor) seeds its ``value`` into the editor as HTML.
RIPPLE_DEFERRED: frozenset[str] = frozenset({"ripple-frame", "embed", "richtext", "rich-text"})
# Page chrome a chat card never needs: site navigation, page heroes and footers,
# email capture (a lead path outside lead capture), logo walls and testimonials
# (claims about real brands), app shells, off-canvas panels, scroll effects, and the
# global palette, tour and inbox overlays. Not offered to the model, refused in a card.
RIPPLE_CHROME: frozenset[str] = frozenset(
    {
        "navbar",
        "footer",
        "hero",
        "marketing-hero",
        "newsletter",
        "logo-cloud",
        "testimonial",
        "app-shell",
        "sidebar",
        "breadcrumb",
        "sheet",
        "parallax",
        "reveal",
        "command-palette",
        "coachmark",
        "notification-center",
    }
)
# The data widgets: the model writes a few lines of data and the widget draws the
# card. Listed with typed props, minus the ones every data widget shares (the rules
# teach those once). booking and menu-order list only what the model writes (the
# server fills the rest); exec-dashboard its rows mode only, so the model writes
# rows instead of prebuilt KPI tiles and charts.
RIPPLE_DATA_WIDGETS: dict[str, frozenset[str] | None] = {
    "itinerary": None,
    "booking": frozenset({"party", "preferred"}),
    "menu-order": frozenset({"items", "featured", "preset"}),
    "growth-projection": None,
    "recipe": None,
    "meal-plan": None,
    "interval-workout": None,
    "flashcard-deck": None,
    "comparison-layout": None,
    "bill-split": None,
    "exec-dashboard": frozenset(
        {"rows", "measures", "dimensions", "x", "split", "compare", "compareLabel", "filters"}
    ),
}
# Ripple's ``illustration`` (model-written animated SVG): display only, no handlers or
# bind. Its ``svg`` is held to illustration_svg's policy instead of the text checks
# (the xmlns URL is fine there).
ILLUSTRATION = "illustration"
ILLUSTRATION_HEIGHT = (80, 640)
# Ripple's ``bill-split``: plain finite numbers, 2 to 12 people, no handlers.
BILL_SPLIT = "bill-split"
BILL_SPLIT_PEOPLE = (2, 12)
# Ripple's games and habit tracker: literal data only, plain text, the caps below,
# ``bind`` value, and no handler but a game's optional ``on_complete``. Listed with
# typed props, their title included (the data widgets' shared props are not theirs)
# and on_complete left out (each description names it).
RIPPLE_PLAY_WIDGETS: dict[str, frozenset[str] | None] = {
    "memory-match": frozenset({"title", "pairs", "columns", "time_limit_s"}),
    "word-guess": frozenset({"title", "answer", "hint", "max_guesses", "allow_any_word"}),
    "quiz": frozenset({"title", "topic", "questions", "seconds_per_question", "shuffle_choices"}),
    "habit-tracker": frozenset({"title", "habits", "week_start", "weeks", "seed"}),
    "focus-timer": frozenset(
        {"title", "focus_min", "short_break_min", "long_break_min", "rounds_before_long"}
        | {"goal_rounds", "task", "auto_start_next"}
    ),
    "board-game": frozenset({"game", "title", "player", "first", "difficulty", "best_of"}),
}
MEMORY_PAIRS = (2, 12)
WORD_GUESS_ANSWER = re.compile(r"[A-Za-z]{4,7}")
WORD_GUESS_ROWS = (1, 10)
QUIZ_QUESTIONS = (1, 12)
QUIZ_CHOICES = (2, 5)
QUIZ_SECONDS = (3, 600)
HABITS = (1, 8)
HABIT_TARGET = (1, 7)
HABIT_WEEKS = (1, 4)
HABIT_SEED_DAY = 27  # the most days back a seed tick may sit (4 weeks)
FOCUS_MIN = (1, 120)
BREAK_MIN = (1, 60)  # both breaks
FOCUS_ROUNDS = (1, 12)
FOCUS_GOAL = (1, 24)
FOCUS_TASK_MAX = 120
# A board-game's two marks per game: the visitor's ``player`` is one of them.
BOARD_MARKS: dict[str, tuple[str, str]] = {
    "tic-tac-toe": ("X", "O"),
    "connect-four": ("red", "yellow"),
}
BOARD_BEST_OF = (1, 3, 5)
# Names ripple's registry also draws as one of these widgets, which its chat allowlist
# refuses: a node by one is checked and sent under the widget's own name, a board-game
# alias filling a missing ``game``.
RIPPLE_ALIASES: dict[str, str] = {
    "trivia": "quiz",
    "trivia-quiz": "quiz",
    "pomodoro": "focus-timer",
    "pomodoro-timer": "focus-timer",
    "tic-tac-toe": "board-game",
    "connect-four": "board-game",
}
# An illustration's ``annotations``: numbered notes, each pinned on an svg id or a point.
ANNOTATIONS_MAX = 8
ANNOTATION_LABEL_MAX = 40
ANNOTATION_NOTE_MAX = 280
# A button's choice-card ``icon`` (a flow option tile): a key shaped like ripple's icon
# names. An unknown key passes (the widget ignores it); markup or a URL does not.
# Its ``description`` hint: plain text, at most this long.
CHOICE_ICON = re.compile(r"[a-z0-9-]{1,24}")
CHOICE_DESCRIPTION_MAX = 120
# A ripple card's actions: paw-bar's, plus the client-side flow ones (no network,
# no navigation). Every step inside a flow or branch is held to the same set.
RIPPLE_ACTIONS: frozenset[str] = SPEC_ACTIONS | {"flow", "branch", "validate", "toast"}
RIPPLE_PROFILE = CardProfile(
    name="ripple",
    widget_types=frozenset(w["type"] for w in RIPPLE_MANIFEST["widgets"])
    - RIPPLE_DEFERRED
    - RIPPLE_CHROME,
    actions=RIPPLE_ACTIONS,
    max_nodes=400,
    max_depth=16,
    max_chars=64_000,
    manifest=RIPPLE_MANIFEST,
    # The widgets the authoring rules lean on (the inputs and layouts, the
    # widgets whose item shapes _RIPPLE_EXAMPLE shows, and the data widgets),
    # listed with their props. Only a data widget's line gives the props' types,
    # so a composite whose shapes nothing shows (analytics-dashboard) stays brief.
    detailed=frozenset(RIPPLE_DATA_WIDGETS)
    | frozenset(RIPPLE_PLAY_WIDGETS)
    | frozenset(
        {
            "flex",
            "grid",
            "card",
            "each",
            "if",
            "text",
            "heading",
            "stat",
            "chart",
            "button",
            "number-input",
            "slider",
            "segmented",
            "switch",
            "checkbox",
            "select",
            "input",
            "progress",
            "table",
            "badge",
            "separator",
            "tabs",
            "entity-detail",
            "timeline",
            "kv-table",
            ILLUSTRATION,
            "alert",
            "callout",
        }
    ),
    typed={**RIPPLE_DATA_WIDGETS, **RIPPLE_PLAY_WIDGETS},
    strict=True,
    host_events=(*HOST_EVENTS, "book"),
)
# The widgets whose one handler the server attaches after validation (``_fill_store``):
# the handler key, the host event it emits. A model-written handler on them is refused.
SERVER_WIRED: dict[str, tuple[str, str]] = {
    "menu-order": ("on_checkout", "checkout"),
    "booking": ("on_book", "book"),
}

# Every widget name and action name the Ripple manifest knows (deferred ones too).
_RIPPLE_TYPES: frozenset[str] = frozenset(w["type"] for w in RIPPLE_MANIFEST["widgets"])
_RIPPLE_ACTIONS: frozenset[str] = frozenset(RIPPLE_MANIFEST["actions"])
# Props the engine draws as a node (NodeRenderer), by widget: the manifest's
# ``string | UISpec`` props, plus the ones only the engine reads.
_ENGINE_NODE_PROPS: dict[str, frozenset[str]] = {
    "split": frozenset({"start", "end"}),
    "master-detail": frozenset({"detail"}),
    "kanban": frozenset({"cardTemplate"}),
    "virtual-list": frozenset({"item"}),
}
_NODE_PROPS: dict[str, frozenset[str]] = {
    w["type"]: frozenset(
        name
        for name, spec in (w.get("props") or {}).items()
        if "UISpec" in str(spec.get("type", "")) and not str(spec["type"]).startswith("Array")
    )
    | _ENGINE_NODE_PROPS.get(w["type"], frozenset())
    for w in RIPPLE_MANIFEST["widgets"]
}
# Props whose rows the engine draws as nodes: (widget, prop) -> the row key that
# holds the node ("" when the row is the node). The manifest's
# ``Array<{ key?: UISpec }>`` rows, plus tabs ``panels`` and the grids' column
# ``formatter``.
_NODE_ROWS: dict[tuple[str, str], str] = {
    ("tabs", "panels"): "",
    ("data-grid", "columns"): "formatter",
    ("tree-table", "columns"): "formatter",
    **{
        (w["type"], name): m.group(1)
        for w in RIPPLE_MANIFEST["widgets"]
        for name, spec in (w.get("props") or {}).items()
        if str(spec.get("type", "")).startswith("Array")
        and (m := re.search(r"(\w+)\??:\s*UISpec", str(spec["type"])))
    },
}
# Props that carry event handlers (a ``*Actions`` slot, or rows such as
# comparison ``items`` whose ``actions`` and ``learn_more`` are handlers), by
# widget. Never an expression: the engine would dispatch whatever it resolves to.
_HANDLER_PROPS: frozenset[tuple[str, str]] = frozenset(
    (w["type"], name)
    for w in RIPPLE_MANIFEST["widgets"]
    for name, spec in (w.get("props") or {}).items()
    if "EventAction" in str(spec.get("type", ""))
)
# A follow-up emits ``props.event`` (this when unset) with the typed text, unless
# its node has an ``on_submit``.
_FOLLOW_UP_EVENT = "follow-up"
# Lists whose rows carry their own ``action`` field as data, not as a handler.
_DATA_ACTION_ROWS: frozenset[tuple[str, str]] = frozenset({("audit-log", "entries")})
# Keys whose string value is a URL a browser loads or follows: these, and any key
# ending in one of the suffixes (``ctaHref``, ``environmentImage``). Held to the
# URL policy, so an expression there is refused too.
_URL_KEYS: frozenset[str] = frozenset({"cover", "link"})
_URL_SUFFIXES: tuple[str, ...] = (
    "href",
    "src",
    "srcset",
    "url",
    "uri",
    "image",
    "img",
    "avatar",
    "favicon",
    "poster",
    "logo",
)
# Link keys, where a mailto: or tel: link is fine too.
_LINK_SUFFIXES: tuple[str, ...] = ("href", "link", "url")
# Keys a renderer may read a URL from: an expression ("{...}") under one is
# refused, since what it resolves to is never seen here. ``target`` is not one:
# in an action it is a state path ("rows.{index}.done").
_URL_ISH_KEY = re.compile(
    r"(href|url|uri|src|srcset|link|image|img|avatar|icon|favicon|poster|cover|background|action)$",
    re.IGNORECASE,
)
_TILE_PRESETS: frozenset[str] = frozenset(
    {"osm", "carto-voyager", "carto-light", "carto-dark", "osm-hot"}
)
# A form's own submission target (a ripple form posts natively when it has one).
_FORM_SUBMIT_PROPS: frozenset[str] = frozenset({"action", "method", "target", "enctype"})
# A script link anywhere, or a data/file/blob target of a markdown link, a
# reference definition or an autolink. Run on ``_normalized`` text (no spaces).
_BAD_SCHEME_TEXT = re.compile(r"(?:javascript|vbscript):|(?:\]\(|\]:|<)(?:data|file|blob):")
_ABSOLUTE = ("http:", "https:", "data:", "blob:", "file:")
# The keys inside a flow or branch whose lists are more actions (``on_*`` are
# handler keys already).
_STEP_KEYS: frozenset[str] = frozenset({"steps", "then", "else"})
# A flow card (Ripple's chain, ripple only): a ``ui`` holding any of these is its
# first step. A step may hold only ``FLOW_STEP_KEYS``; a card at most
# ``MAX_FLOW_STEPS`` steps, counting every ``chain`` and ``chain_map`` value.
_FLOW_FIELDS: tuple[str, ...] = ("chain", "chain_map", "flowId", "onComplete")
FLOW_STEP_KEYS: frozenset[str] = frozenset(
    {
        "version",
        "id",
        "flowId",
        "intent",
        "title",
        "description",
        "ui",
        "chain",
        "chain_map",
        "onComplete",
        "form_fields",
    }
)
MAX_FLOW_STEPS = 8
# The emit targets that move a flow (Ripple's FlowRunner takes them); flow cards only.
FLOW_EVENTS: tuple[str, ...] = ("flow.next", "flow.back", "flow.forward", "flow.submit")
# The flow emit that fires a terminal step's ``onComplete`` (a visitor message):
# held to ``ASK_HANDLERS`` like ``ask``.
FLOW_SUBMIT = "flow.submit"
# The host event a click sends a visitor message with (ripple only), and the most
# text it, or a flow's chat ``onComplete``, may send.
ASK_EVENT = "ask"
ASK_MAX = 500
# The handler keys an ``ask`` (or ``flow.submit``) may fire from: a click, a
# submit, a pick, a comparison's Choose click (``on_choose``), and a composite's
# button list (comparison ``items[].actions``, entity-detail ``actions[].actions``).
# Focus, input, change, timers and the wizard's ``*Actions`` fire without one, so
# they may not. Keep in parity with ripple's ``routes/pawbar/card-policy.ts``.
ASK_HANDLERS: frozenset[str] = frozenset(
    {"on_click", "on_submit", "on_select", "on_choose", "actions"}
)
# CSS that loads something or runs script. Run on ``_css_text``.
_CSS_LOADS: tuple[str, ...] = (
    "url(",
    "image-set(",
    "src(",
    "element(",
    "//",
    "expression(",
    "@import",
    "-moz-binding",
    "behavior:",
)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\r\n\f]?|\\(.)", re.DOTALL)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*:")
_CSS_DECL_SCHEME = re.compile(r"(?:javascript|vbscript|data|file|blob|https?):")
# Props rendered as markdown (the manifest says so, plus the two text props a
# markdown renderer reads) and the toast-shown messages: their expressions may
# only be plain paths, never operators, calls or concatenation.
_MARKDOWN_PROPS: frozenset[tuple[str, str]] = frozenset(
    (w["type"], name)
    for w in RIPPLE_MANIFEST["widgets"]
    for name, spec in (w.get("props") or {}).items()
    if "markdown" in str(spec.get("description", "")).lower()
) | {("markdown", "text"), ("stream-text", "text")}
_TOAST_ACTIONS: frozenset[str] = frozenset({"toast", "validate"})
_EXPRESSION = re.compile(r"\{([^{}]*)\}")
_PLAIN_PATH = re.compile(r"\s*[A-Za-z_$][\w$]*(?:\.[\w$]+|\[\d+\])*\s*")
# How deep the strict walk follows plain JSON nesting (a 16-deep node tree with
# props is well under it).
_MAX_SCAN_LEVELS = 96

_PRODUCT_CARD = "product-card"
_FENCE = "```"


class _Reject(Exception):
    """The card breaks a rule; it is dropped whole."""


def _serialize(value: Any) -> str:
    # ASCII-only, so Python's len() equals the JS .length paw-bar measures.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #


def _field(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _catalog_index(catalog: Iterable[Any] | None) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for item in catalog or ():
        pid = str(_field(item, "id") or "").strip()
        if pid and pid not in index:
            index[pid] = item
    return index


def _card_verbs(verbs: Iterable[str] | None) -> list[str]:
    declared = set(verbs or ())
    return [v for v in HOST_EVENTS if v in declared]


def _items(ids: Any, index: dict[str, Any], verbs: list[str]) -> list[dict[str, Any]]:
    """Catalog items for the model's ids, in the model's order, unknown and
    repeated ids dropped. Every field comes from the catalog."""
    if not isinstance(ids, list):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in ids:
        pid = raw.strip() if isinstance(raw, str) else ""
        product = index.get(pid)
        if product is None or pid in seen:
            continue
        seen.add(pid)
        items.append(
            {
                "id": pid,
                "name": str(_field(product, "name") or pid),
                "price_cents": int(_field(product, "price_cents", 0) or 0),
                "currency": str(_field(product, "currency") or "USD"),
                "image_url": str(_field(product, "image_url") or ""),
                "url": str(_field(product, "url") or ""),
                "description": str(_field(product, "description") or ""),
                "actions": list(verbs),
            }
        )
    return items


# --------------------------------------------------------------------------- #
# Ripple specs
# --------------------------------------------------------------------------- #


def _check_actions(
    value: Any, events: list[str], allowed: frozenset[str] = SPEC_ACTIONS, ask: bool = False
) -> None:
    """``ask`` (ripple, under one of ``ASK_HANDLERS``): an ``emit`` to
    ``ASK_EVENT`` may pass too, its value exactly ``{"text": <plain text of at
    most ASK_MAX chars>}``, and only then may one emit ``FLOW_SUBMIT``."""
    for action in value if isinstance(value, list) else [value]:
        # The engine looks an action up by its verb name; an object or list there
        # (``{label, action: {...}}``) is nothing it runs, and is unhashable.
        named = action.get("action") if isinstance(action, dict) else None
        if not isinstance(named, str) or named not in allowed:
            raise _Reject("an event runs an action the bar does not honour")
        if action["action"] == "emit" and action.get("target") not in events:
            if not (ask and action.get("target") == ASK_EVENT):
                raise _Reject("an emit names a host event this widget does not declare")
            said = action.get("value")
            if not (
                isinstance(said, dict)
                and said.keys() == {"text"}
                and isinstance(said["text"], str)
                and len(said["text"]) <= ASK_MAX
                and "{" not in said["text"]
            ):
                raise _Reject(f"an ask that is not plain {{text}} of at most {ASK_MAX} characters")
        if action["action"] == "emit" and action.get("target") == FLOW_SUBMIT and not ask:
            raise _Reject("a flow.submit not from an explicit visitor action")


def _check_events(
    node: dict[str, Any],
    events: list[str],
    allowed: frozenset[str] = SPEC_ACTIONS,
    ask: bool = False,
) -> None:
    props = node.get("props")
    for holder in (node, props if isinstance(props, dict) else {}):
        for key, value in holder.items():
            if isinstance(key, str) and key.startswith("on_"):
                _check_actions(value, events, allowed, ask)


def _is_handler_key(key: Any) -> bool:
    """A key whose value the engine dispatches: ``on_*``, ``actions``,
    ``*Actions``, comparison ``learn_more`` and table ``onRowClick``."""
    return isinstance(key, str) and (
        key.startswith("on_")
        or key == "actions"
        or key.endswith("Actions")
        or key in ("learn_more", "onRowClick")
    )


def _ask_from(ask: bool | None, key: str) -> bool | None:
    """Whether an ``ask`` may fire below ``key``: None until a handler key is
    met, then fixed by the OUTERMOST one (an ``actions`` nested in an
    ``on_focus`` handler stays refused)."""
    if ask is not None or not _is_handler_key(key):
        return ask
    return key in ASK_HANDLERS


def _normalized(text: str) -> str:
    """``text`` as a browser would resolve it: entities and %-escapes undone and
    NFKC-folded (three rounds, so fullwidth letters read as ASCII), whitespace,
    control and invisible format characters (zero-width, soft hyphen, BOM: Cf)
    dropped, lowercased, backslashes read as slashes."""
    out = text
    for _ in range(3):
        nxt = unicodedata.normalize("NFKC", unquote(html.unescape(out)))
        if nxt == out:
            break
        out = nxt
    return "".join(_visible(out)).lower().replace("\\", "/")


def _visible(text: str) -> Iterable[str]:
    return (ch for ch in text if ch > " " and ch != "\x7f" and unicodedata.category(ch) != "Cf")


def _check_plain_expressions(text: str) -> None:
    """Markdown and toast text: every ``{...}`` is a plain path (``state.a.b``,
    ``item.x``, ``list[0]``), and no brace is left over."""
    rest = _EXPRESSION.sub(lambda m: "" if _PLAIN_PATH.fullmatch(m.group(1)) else "{", text)
    if "{" in rest or "}" in rest:
        raise _Reject("markdown or toast text with more than a plain path in braces")


def _https_allowed(url: str, hosts: frozenset[str]) -> bool:
    if not url.startswith("https://"):
        return False
    head = re.split(r"[)\]>\"',;]", url, maxsplit=1)[0]
    return urlsplit(head).hostname in hosts


def _is_url_key(key: str) -> bool:
    low = key.lower()
    return key in _URL_KEYS or low.endswith(_URL_SUFFIXES)


def _check_url(key: str, value: str, hosts: frozenset[str]) -> None:
    """A URL key's string: "", a same-site path ("/x", not "//x") or "#x", an
    https URL on ``hosts``, or under a link key a mailto: or tel: link. A map's
    tiles must be a preset; a tile URL template is never taken."""
    url = _normalized(value)
    if key == "tiles":
        if url not in _TILE_PRESETS:
            raise _Reject("map tiles must be a preset")
        return
    if key in ("tile", "tileUrl"):
        raise _Reject("a custom tile template")
    if not url or url[0] == "#" or (url[0] == "/" and url[1:2] != "/"):
        return
    if _https_allowed(url, hosts):
        return
    if key.lower().endswith(_LINK_SUFFIXES) and url.startswith(("mailto:", "tel:")):
        return
    raise _Reject(f"a {key} that is not a same-site path or an allowed host")


def _check_text(value: str, hosts: frozenset[str]) -> None:
    """Any string in a strict card, and the string literals an expression in it
    concatenates (``{'java'+'script:'}``): no script or data link, an absolute
    URL only on ``hosts``, and every ``//`` only as https on ``hosts``."""
    text = _normalized(value)
    forms = [text]
    if "{" in text:
        forms.append(re.sub(r"[{}'\"`+]", "", text))
    for form in forms:
        if _BAD_SCHEME_TEXT.search(form):
            raise _Reject("text holding a script or data link")
        if form.startswith(_ABSOLUTE) and not _https_allowed(form, hosts):
            raise _Reject("a URL off the allowed hosts")
        at = form.find("//")
        while at != -1:
            if form[max(0, at - 6) : at] != "https:" or not _https_allowed(form[at - 6 :], hosts):
                raise _Reject("a link off the allowed hosts")
            at = form.find("//", at + 2)


def _css_char(match: re.Match[str]) -> str:
    if match.group(2) is not None:
        return match.group(2)
    point = int(match.group(1), 16)
    return chr(point) if 0 < point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF else "�"


def _check_css(value: str, *, declarations: bool) -> None:
    """A style string may not load anything: no ``url(`` and friends, no ``//``,
    no ``@import`` or ``expression(``, and no scheme (a declaration list may use
    ``:`` between property and value, so there only the known schemes count).
    CSS escapes (``\75 rl(``), comments, entities and case are undone first."""
    css = unicodedata.normalize("NFKC", html.unescape(value))
    css = _CSS_ESCAPE.sub(_css_char, _CSS_COMMENT.sub("", css))
    css = "".join(_visible(unicodedata.normalize("NFKC", css))).lower()
    if any(token in css for token in _CSS_LOADS):
        raise _Reject("a style that loads something")
    if (_CSS_DECL_SCHEME if declarations else _CSS_SCHEME).search(css):
        raise _Reject("a style naming a scheme")


def _check_strict(
    spec: dict[str, Any], events: list[str], lead_capture: bool, profile: CardProfile
) -> None:
    """``_strict_walk``, where any error the rules did not foresee (a JSON shape
    a check assumed away) is a logged refusal, never an exception."""
    try:
        _strict_walk(spec, events, lead_capture, profile)
    except _Reject:
        raise
    except Exception as exc:  # noqa: BLE001 — an unforeseen shape is refused
        _log_dropped(exc)
        raise _Reject("a shape the checks could not read") from exc


def _strict_walk(
    spec: dict[str, Any], events: list[str], lead_capture: bool, profile: CardProfile
) -> None:
    """The ``strict`` profile's walk over ``ui`` and ``state`` (see the header).
    Iterative, so no card can exhaust the stack; past ``_MAX_SCAN_LEVELS`` of
    nesting the card is refused. A flow card's steps (``_flow_steps``) share one
    node budget; ``emit ask`` passes only under one of ``ASK_HANDLERS``."""
    allowed, hosts = profile.actions, profile.url_hosts
    nodes = 0
    ui = spec["ui"]
    flow = any(field in ui for field in _FLOW_FIELDS)
    host_events = events
    events = [*events, *FLOW_EVENTS] if flow else events
    # (value, json level, node depth, key it sits under, under a handler, may ask
    # (``_ask_from``; False in state), is a node, is a data row whose "action" is
    # its own, inside a style, in state)
    stack: list[tuple[Any, int, int, str, bool, bool | None, bool, bool, bool, bool]] = (
        _flow_steps(ui) if flow else [(ui, 1, 1, "", False, None, True, False, False, False)]
    )
    stack.append((spec.get("state"), 1, 0, "", False, False, False, False, False, True))
    while stack:
        value, level, depth, key, handler, ask, is_node, data_row, css, in_state = stack.pop()
        if level > _MAX_SCAN_LEVELS:
            raise _Reject("nested too deeply")
        if isinstance(value, str):
            if css or key == "background":
                _check_css(value, declarations=key == "style")
            elif key in ("tiles", "tile", "tileUrl") or _is_url_key(key):
                _check_url(key, value, hosts)
            if "{" in value and _URL_ISH_KEY.search(key):
                raise _Reject(f"an expression in {key}, where a URL is read")
            _check_text(value, hosts)
            continue
        if isinstance(value, list):
            stack.extend(
                (v, level + 1, depth, key, handler, ask, False, data_row, css, in_state)
                for v in value
            )
            continue
        if not isinstance(value, dict):
            continue
        if "onComplete" in value or "onComplete" in (
            value.get("props") if isinstance(value.get("props"), dict) else ()
        ):
            # A step's own onComplete never reaches the walk (``_flow_steps``).
            raise _Reject("an onComplete off a flow step")
        kind = value.get("type")
        if not is_node and not css and isinstance(kind, str):
            # A node kept in a prop or in state: a widget's name, or node-shaped.
            is_node = kind in _RIPPLE_TYPES or any(
                k in value for k in ("props", "children", "else_children", "bind")
            )
            depth += 1
        if is_node:
            if not isinstance(kind, str):
                raise _Reject("a node has no type")
            nodes += 1
            if nodes > profile.max_nodes:
                raise _Reject(f"more than {profile.max_nodes} nodes")
            if profile.max_depth < depth:
                raise _Reject(f"nested deeper than {profile.max_depth}")
            if kind not in profile.widget_types:
                raise _Reject(f"unknown widget type {kind!r}")
            _check_events(value, events, allowed, ask=True)
            props = value.get("props")
            if props is not None and not isinstance(props, dict):
                raise _Reject("a node's props are not an object")
            if kind in SERVER_WIRED and any(map(_is_handler_key, [*value, *(props or ())])):
                raise _Reject(f"a handler on {kind}, which the server wires")
            if kind == "follow-up" and ("event" in (props or {}) or "on_submit" not in value):
                if (props or {}).get("event", _FOLLOW_UP_EVENT) not in host_events:
                    raise _Reject("a follow-up emits a host event this widget does not declare")
            # A node met in state or inside a handler may not ask at all.
            base = None if ask is None else False
            if kind == ILLUSTRATION:
                _check_illustration(value, props)
            if kind == BILL_SPLIT:
                _check_bill_split(value, props)
            if kind in _PLAY_CHECKS:
                _PLAY_CHECKS[kind](value, props if isinstance(props, dict) else {})
            if kind == "button":
                _check_choice(props)
            if kind == "form":
                if isinstance(props, dict) and _FORM_SUBMIT_PROPS & props.keys():
                    raise _Reject("a form with its own submit target")
                _check_form(props, lead_capture)
            for k, v in value.items():
                if k in ("children", "else_children"):
                    if v is None:
                        continue
                    if not isinstance(v, list):
                        raise _Reject(f"{k} is not a list")
                    stack.extend(
                        (kid, level + 2, depth + 1, k, False, base, True, False, False, in_state)
                        for kid in v
                    )
                elif k == "props" and isinstance(v, dict):
                    node_props = _NODE_PROPS.get(kind, frozenset())
                    for pk, pv in v.items():
                        if kind == ILLUSTRATION and pk == "svg":
                            continue  # held to the SVG policy (_check_illustration)
                        if (kind, pk) in _MARKDOWN_PROPS and isinstance(pv, str):
                            _check_plain_expressions(pv)
                        _check_resolved_prop(kind, pk, pv, node_props)
                        sub_node = pk in node_props and isinstance(pv, dict)
                        stack.append(
                            (
                                pv,
                                level + 2,
                                depth + 1 if sub_node else depth,
                                pk,
                                _is_handler_key(pk),
                                _ask_from(base, pk),
                                sub_node,
                                (kind, pk) in _DATA_ACTION_ROWS and not in_state,
                                pk == "style",
                                in_state,
                            )
                        )
                elif k != "type":
                    stack.append(
                        (
                            v,
                            level + 1,
                            depth,
                            k,
                            _is_handler_key(k),
                            _ask_from(base, k),
                            False,
                            False,
                            k == "style",
                            in_state,
                        )
                    )
            continue
        named = value.get("action")
        is_action = (handler and "action" in value) or (
            isinstance(named, str) and named in _RIPPLE_ACTIONS and not data_row and not css
        )
        if is_action:
            if in_state:
                raise _Reject("state holds an action")
            _check_actions(value, events, allowed, ask=ask is True)
            if named in _TOAST_ACTIONS and isinstance(value.get("message"), str):
                _check_plain_expressions(value["message"])
        stack.extend(
            (
                v,
                level + 1,
                depth,
                k,
                handler or _is_handler_key(k) or (is_action and k in _STEP_KEYS),
                _ask_from(ask, k),
                False,
                False,
                css or k == "style",
                in_state,
            )
            for k, v in value.items()
        )


def _check_illustration(node: dict[str, Any], props: Any) -> None:
    """An ``illustration`` node: no bind and no handler but a node-level ``on_select``
    (which may ask), ``svg`` and non-blank ``title`` text, ``caption`` text if given (null
    is absent), ``max_height`` a number in ``ILLUSTRATION_HEIGHT``, the svg passes
    ``svg_violation`` and ``annotations``, when given, ``_check_annotations``. The svg
    must be literal: a ``{...}`` there would be resolved by the engine into markup this
    check never saw."""
    props = props or {}
    handlers = [k for k in [*node, *props] if _is_handler_key(k)]
    if "bind" in node or [k for k in handlers if k != "on_select" or k in props]:
        raise _Reject("a handler or bind on an illustration")
    svg, title = props.get("svg"), props.get("title")
    if not isinstance(svg, str) or not isinstance(title, str):
        raise _Reject("an illustration needs svg and title text")
    if not title.strip():
        raise _Reject("an illustration with an empty title")
    if props.get("caption") is not None and not isinstance(props["caption"], str):
        raise _Reject("an illustration caption that is not text")
    height = props.get("max_height")
    height = ILLUSTRATION_HEIGHT[0] if height is None else height
    low, high = ILLUSTRATION_HEIGHT
    if not _is_number(height) or not low <= height <= high:
        raise _Reject(f"an illustration max_height outside {low}..{high}")
    if "{" in svg:
        raise _Reject("an expression in an illustration's svg")
    if reason := svg_violation(svg):
        raise _Reject(f"an illustration with {reason}")
    if props.get("annotations") is not None:
        _check_annotations(props["annotations"], svg)


def _js_len(text: str) -> int:
    """``text.length`` in the browser (UTF-16 units), which ripple's caps count."""
    return len(text.encode("utf-16-le")) // 2


def _check_annotations(notes: Any, svg: str) -> None:
    """An illustration's ``annotations``, as ripple's checkIllustrationAnnotations: at
    most ``ANNOTATIONS_MAX`` objects, each a unique non-blank ``id``, a non-blank
    ``label`` and a ``note`` of plain text within their caps, and exactly one of
    ``target`` (an id the widget's rebuild keeps, ``svg_ids``) or ``at`` (two finite
    numbers). A key set to null counts as given, as it does there."""
    if not isinstance(notes, list) or len(notes) > ANNOTATIONS_MAX:
        raise _Reject(f"annotations that are not a list of at most {ANNOTATIONS_MAX}")
    seen: set[str] = set()
    kept: frozenset[str] | None = None
    for note in notes:
        if not isinstance(note, dict):
            raise _Reject("an annotation that is not an object")
        nid, label, text = note.get("id"), note.get("label"), note.get("note")
        if not isinstance(nid, str) or not nid.strip() or nid in seen:
            raise _Reject("an annotation without a unique id")
        seen.add(nid)
        if not _plain(label) or not label.strip() or _js_len(label) > ANNOTATION_LABEL_MAX:
            raise _Reject(
                f"an annotation label that is not plain text of 1 to {ANNOTATION_LABEL_MAX}"
            )
        if not _plain(text) or _js_len(text) > ANNOTATION_NOTE_MAX:
            raise _Reject(
                f"an annotation note that is not plain text of at most {ANNOTATION_NOTE_MAX}"
            )
        if ("target" in note) == ("at" in note):
            raise _Reject("an annotation without exactly one of target or at")
        if "at" in note:
            at = note["at"]
            if not (isinstance(at, list) and len(at) == 2 and all(map(_is_number, at))):
                raise _Reject("an annotation at that is not two finite numbers")
            continue
        if kept is None:
            kept = svg_ids(svg)
        if note["target"] not in kept:
            raise _Reject("an annotation target that is not an id in the svg")


def _is_number(value: Any) -> bool:
    """A literal finite JSON number (not a bool, not an expression string)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_bill_split(node: dict[str, Any], props: Any) -> None:
    """A ``bill-split`` node: no handler, ``subtotal`` a finite number and ``tax``,
    ``tip_percent``, ``tip_options[]`` and each person's ``extras`` too when given
    (null is absent), ``people`` a list of ``BILL_SPLIT_PEOPLE`` rows with ``id`` and
    ``name`` text, and ``title``, ``currency``, ``extras_label``, ``note`` text.
    Literal values only: the widget does the maths, so an expression would reach it
    as a string."""
    props = props if isinstance(props, dict) else {}
    if any(map(_is_handler_key, [*node, *props])):
        raise _Reject("a handler on a bill-split")
    if not _is_number(props.get("subtotal")):
        raise _Reject("a bill-split subtotal that is not a finite number")
    for key in ("tax", "tip_percent"):
        if props.get(key) is not None and not _is_number(props[key]):
            raise _Reject(f"a bill-split {key} that is not a finite number")
    tips = props.get("tip_options")
    if tips is not None and not (isinstance(tips, list) and all(map(_is_number, tips))):
        raise _Reject("bill-split tip_options that are not finite numbers")
    for key in ("title", "currency", "extras_label", "note"):
        if props.get(key) is not None and not isinstance(props[key], str):
            raise _Reject(f"a bill-split {key} that is not text")
    people = props.get("people")
    low, high = BILL_SPLIT_PEOPLE
    if not isinstance(people, list) or not low <= len(people) <= high:
        raise _Reject(f"a bill-split without {low} to {high} people")
    for person in people:
        if not isinstance(person, dict) or not all(
            isinstance(person.get(k), str) for k in ("id", "name")
        ):
            raise _Reject("a bill-split person without id and name text")
        if person.get("extras") is not None and not _is_number(person["extras"]):
            raise _Reject("a bill-split person's extras that are not a finite number")


def _check_choice(props: Any) -> None:
    """A button's choice-card props, when given (null is absent): ``icon`` a
    ``CHOICE_ICON`` key, ``description`` plain text (no ``{...}``) of at most
    ``CHOICE_DESCRIPTION_MAX`` characters."""
    props = props if isinstance(props, dict) else {}
    icon, hint = props.get("icon"), props.get("description")
    if icon is not None and not (isinstance(icon, str) and CHOICE_ICON.fullmatch(icon)):
        raise _Reject("a button icon that is not an icon key")
    if hint is not None and not (
        isinstance(hint, str) and "{" not in hint and len(hint) <= CHOICE_DESCRIPTION_MAX
    ):
        raise _Reject(f"a button description over {CHOICE_DESCRIPTION_MAX} or not plain text")


def _plain(value: Any) -> bool:
    """Literal text: a string with no ``{...}`` for the engine to resolve."""
    return isinstance(value, str) and "{" not in value


def _is_int(value: Any, bounds: tuple[int, int]) -> bool:
    return _is_number(value) and value == int(value) and bounds[0] <= value <= bounds[1]


def _play_basics(
    kind: str,
    node: dict[str, Any],
    props: dict[str, Any],
    handler: str | None,
    texts: Iterable[str],
) -> None:
    """What every play widget shares: no handler but ``handler``, and each of ``texts``
    plain text when given (null is absent)."""
    if any(_is_handler_key(k) and k != handler for k in [*node, *props]):
        raise _Reject(f"a handler on a {kind} it does not take")
    for key in texts:
        if props.get(key) is not None and not _plain(props[key]):
            raise _Reject(f"a {kind} {key} that is not plain text")


def _optional(props: dict[str, Any], key: str, ok: Any) -> bool:
    return props.get(key) is None or ok(props[key])


def _check_memory_match(node: dict[str, Any], props: dict[str, Any]) -> None:
    """``pairs`` a list of ``MEMORY_PAIRS`` rows, each ``a`` and ``b`` plain text (a word,
    an emoji or ``icon:<key>``) and an ``id`` if given; ``columns`` and
    ``time_limit_s`` finite numbers; ``on_complete`` its one handler."""
    _play_basics("memory-match", node, props, "on_complete", ("title",))
    pairs = props.get("pairs")
    low, high = MEMORY_PAIRS
    if not isinstance(pairs, list) or not low <= len(pairs) <= high:
        raise _Reject(f"a memory-match without {low} to {high} pairs")
    for pair in pairs:
        if not isinstance(pair, dict) or not (_plain(pair.get("a")) and _plain(pair.get("b"))):
            raise _Reject("a memory-match pair without plain a and b text")
        if not _optional(pair, "id", _plain):
            raise _Reject("a memory-match pair id that is not plain text")
    if not (
        _optional(props, "columns", _is_number) and _optional(props, "time_limit_s", _is_number)
    ):
        raise _Reject("a memory-match columns or time_limit_s that is not a finite number")


def _check_word_guess(node: dict[str, Any], props: dict[str, Any]) -> None:
    """``answer`` 4 to 7 letters A to Z, ``max_guesses`` a whole number in
    ``WORD_GUESS_ROWS``, ``allow_any_word`` a boolean; ``on_complete`` its one handler."""
    _play_basics("word-guess", node, props, "on_complete", ("title", "hint"))
    answer = props.get("answer")
    if not isinstance(answer, str) or not WORD_GUESS_ANSWER.fullmatch(answer):
        raise _Reject("a word-guess answer that is not 4 to 7 letters A to Z")
    if not _optional(props, "max_guesses", lambda v: _is_int(v, WORD_GUESS_ROWS)):
        raise _Reject("a word-guess max_guesses outside 1..10")
    if not _optional(props, "allow_any_word", lambda v: isinstance(v, bool)):
        raise _Reject("a word-guess allow_any_word that is not a boolean")


def _check_quiz(node: dict[str, Any], props: dict[str, Any]) -> None:
    """``questions`` a list of ``QUIZ_QUESTIONS`` rows, each a plain ``prompt``,
    ``QUIZ_CHOICES`` plain ``choices``, ``answer`` a whole-number index into them, and a
    plain ``why`` and ``id`` and an ``image`` string if given (the walk holds the image
    to the URL policy); ``seconds_per_question`` in ``QUIZ_SECONDS``,
    ``shuffle_choices`` a boolean; ``on_complete`` its one handler."""
    _play_basics("quiz", node, props, "on_complete", ("title", "topic"))
    questions = props.get("questions")
    low, high = QUIZ_QUESTIONS
    if not isinstance(questions, list) or not low <= len(questions) <= high:
        raise _Reject(f"a quiz without {low} to {high} questions")
    for q in questions:
        if not isinstance(q, dict) or not _plain(q.get("prompt")):
            raise _Reject("a quiz question without a plain prompt")
        choices = q.get("choices")
        low, high = QUIZ_CHOICES
        if not (isinstance(choices, list) and low <= len(choices) <= high):
            raise _Reject(f"a quiz question without {low} to {high} choices")
        if not all(map(_plain, choices)):
            raise _Reject("a quiz choice that is not plain text")
        if not _is_int(q.get("answer"), (0, len(choices) - 1)):
            raise _Reject("a quiz answer that is not the index of a choice")
        if not (_optional(q, "why", _plain) and _optional(q, "id", _plain)):
            raise _Reject("a quiz why or id that is not plain text")
        if not _optional(q, "image", lambda v: isinstance(v, str)):
            raise _Reject("a quiz image that is not text")
    if not _optional(
        props,
        "seconds_per_question",
        lambda v: _is_number(v) and QUIZ_SECONDS[0] <= v <= QUIZ_SECONDS[1],
    ):
        raise _Reject("a quiz seconds_per_question outside 3..600")
    if not _optional(props, "shuffle_choices", lambda v: isinstance(v, bool)):
        raise _Reject("a quiz shuffle_choices that is not a boolean")


def _check_habit_tracker(node: dict[str, Any], props: dict[str, Any]) -> None:
    """No handler; ``habits`` a list of ``HABITS`` rows, each a unique plain ``id``, a
    plain ``name``, an ``icon`` key if given and ``target_per_week`` a whole number in
    ``HABIT_TARGET``; ``week_start`` mon or sun, ``weeks`` in ``HABIT_WEEKS``, and
    ``seed`` an object from habit ids to lists of whole days back, 0 to
    ``HABIT_SEED_DAY``."""
    _play_basics("habit-tracker", node, props, None, ("title",))
    habits = props.get("habits")
    low, high = HABITS
    if not isinstance(habits, list) or not low <= len(habits) <= high:
        raise _Reject(f"a habit-tracker without {low} to {high} habits")
    ids: set[str] = set()
    for habit in habits:
        if not isinstance(habit, dict) or not _plain(habit.get("name")):
            raise _Reject("a habit without a plain name")
        hid = habit.get("id")
        if not _plain(hid) or not hid or hid in ids:
            raise _Reject("a habit without a unique plain id")
        ids.add(hid)
        if not _optional(habit, "icon", lambda v: isinstance(v, str) and CHOICE_ICON.fullmatch(v)):
            raise _Reject("a habit icon that is not an icon key")
        if not _is_int(habit.get("target_per_week"), HABIT_TARGET):
            raise _Reject("a habit target_per_week outside 1..7")
    if not _optional(props, "week_start", lambda v: v in ("mon", "sun")):
        raise _Reject("a habit-tracker week_start that is not mon or sun")
    if not _optional(props, "weeks", lambda v: _is_int(v, HABIT_WEEKS)):
        raise _Reject("a habit-tracker weeks outside 1..4")
    seed = props.get("seed")
    if seed is not None and not (
        isinstance(seed, dict)
        and seed.keys() <= ids
        and all(
            isinstance(days, list)
            and len(days) <= HABIT_SEED_DAY + 1
            and all(_is_int(d, (0, HABIT_SEED_DAY)) for d in days)
            for days in seed.values()
        )
    ):
        raise _Reject("a habit-tracker seed that is not habit ids to days 0..27")


def _check_focus_timer(node: dict[str, Any], props: dict[str, Any]) -> None:
    """No handler; the minutes and rounds whole numbers in ``FOCUS_MIN``, ``BREAK_MIN``,
    ``FOCUS_ROUNDS`` and ``FOCUS_GOAL`` (ripple rounds and clamps them, so a fraction or
    an outlier would not draw as written), ``task`` plain text of at most
    ``FOCUS_TASK_MAX`` and ``auto_start_next`` a boolean."""
    _play_basics("focus-timer", node, props, None, ("title", "task"))
    for key, bounds in (
        ("focus_min", FOCUS_MIN),
        ("short_break_min", BREAK_MIN),
        ("long_break_min", BREAK_MIN),
        ("rounds_before_long", FOCUS_ROUNDS),
        ("goal_rounds", FOCUS_GOAL),
    ):
        if props.get(key) is not None and not _is_int(props[key], bounds):
            raise _Reject(f"a focus-timer {key} outside {bounds[0]}..{bounds[1]}")
    if not _optional(props, "task", lambda v: _js_len(v) <= FOCUS_TASK_MAX):
        raise _Reject(f"a focus-timer task over {FOCUS_TASK_MAX}")
    if not _optional(props, "auto_start_next", lambda v: isinstance(v, bool)):
        raise _Reject("a focus-timer auto_start_next that is not a boolean")


def _check_board_game(node: dict[str, Any], props: dict[str, Any]) -> None:
    """``game`` a key of ``BOARD_MARKS`` (an alias fills it), ``player`` one of that
    game's marks, ``first`` player or computer, ``difficulty`` easy, medium or hard and
    ``best_of`` one of ``BOARD_BEST_OF``; ``on_complete`` its one handler."""
    _play_basics("board-game", node, props, "on_complete", ("title",))
    game = props.get("game")
    if not isinstance(game, str) or game not in BOARD_MARKS:
        raise _Reject("a board-game without game tic-tac-toe or connect-four")
    if not _optional(props, "player", lambda v: v in BOARD_MARKS[game]):
        raise _Reject(f"a board-game player that is not {' or '.join(BOARD_MARKS[game])}")
    if not _optional(props, "first", lambda v: v in ("player", "computer")):
        raise _Reject("a board-game first that is not player or computer")
    if not _optional(props, "difficulty", lambda v: v in ("easy", "medium", "hard")):
        raise _Reject("a board-game difficulty that is not easy, medium or hard")
    if not _optional(props, "best_of", lambda v: _is_number(v) and v in BOARD_BEST_OF):
        raise _Reject("a board-game best_of that is not 1, 3 or 5")


_PLAY_CHECKS = {
    "memory-match": _check_memory_match,
    "word-guess": _check_word_guess,
    "quiz": _check_quiz,
    "habit-tracker": _check_habit_tracker,
    "focus-timer": _check_focus_timer,
    "board-game": _check_board_game,
}


def _check_resolved_prop(kind: str, key: str, value: Any, node_props: frozenset[str]) -> None:
    """A node prop the engine resolves (``"{state.h}"`` becomes whatever state
    holds then) before it dispatches or renders it, so it must be literal. A
    handler slot holds action objects (``actions``: or a composite's button
    rows), alone or in a list; a prop whose rows carry slots (comparison
    ``items``) is a list of objects whose slots hold the same; a node slot
    (``_NODE_PROPS``, or a row of ``_NODE_ROWS`` in a literal list) holds a
    literal node or text (``_check_node_slot``). Slots elsewhere (state, data
    rows, inside an action) are never resolved before they run, so a string
    there stays inert."""
    rows = value if isinstance(value, list) else [value]
    if _is_handler_key(key):
        for row in rows:
            if not isinstance(row, dict) or (key != "actions" and "action" not in row):
                raise _Reject(f"{key} holds something other than actions")
    if (kind, key) in _HANDLER_PROPS:
        for row in rows:
            if not isinstance(row, dict):
                raise _Reject(f"{key} holds a row that is not an object")
            for k, v in row.items():
                _check_resolved_prop("", k, v, frozenset())
    slot = _NODE_ROWS.get((kind, key))
    if slot is not None and value is not None:
        if not isinstance(value, list):
            raise _Reject(f"{key} is not a literal list of rows")
        for row in value:
            if slot:
                if not isinstance(row, dict):
                    raise _Reject(f"{key} holds a node row that is not an object")
                row = row.get(slot)
            _check_node_slot(key, row)
    if key in node_props:
        _check_node_slot(key, value)


def _check_node_slot(key: str, value: Any) -> None:
    """A value the engine draws as a node: a literal node (a Ripple widget, which
    the walk then checks as one) or text. Never a lone expression: it would draw
    whatever state holds by then, which ``set`` pieces can build unchecked."""
    text = value.strip() if isinstance(value, str) else ""
    if text.startswith("{") and text.endswith("}"):
        raise _Reject(f"an expression in {key}, where a node is read")
    if isinstance(value, dict):
        kind = value.get("type")
        if not isinstance(kind, str) or kind not in _RIPPLE_TYPES:
            raise _Reject(f"{key} holds an object that is not a node")


def _flow_steps(root: dict[str, Any]) -> list[tuple[Any, ...]]:
    """A flow card's steps as ``_check_strict`` walk entries: each step's ``ui`` a
    root node (depth 1, the card's node budget), every other step value data
    under its key, so its text gets the string checks. Refuses a step that is not
    an object of ``FLOW_STEP_KEYS`` with a ``ui`` node, more than
    ``MAX_FLOW_STEPS`` steps, and any ``onComplete`` but a chat message."""
    out: list[tuple[Any, ...]] = []
    steps: list[tuple[Any, int]] = [(root, 1)]
    count = 0
    while steps:
        step, level = steps.pop()
        count += 1
        if count > MAX_FLOW_STEPS:
            raise _Reject(f"more than {MAX_FLOW_STEPS} flow steps")
        if not isinstance(step, dict) or not step.keys() <= FLOW_STEP_KEYS:
            raise _Reject("a flow step that is not an object of step keys")
        if not isinstance(step.get("ui"), dict):
            raise _Reject("a flow step with no ui node")
        if "onComplete" in step:
            _check_on_complete(step["onComplete"])
        for k, v in step.items():
            if k == "chain":
                steps.append((v, level + 1))
            elif k == "chain_map":
                if not isinstance(v, dict):
                    raise _Reject("a chain_map that is not an object")
                steps.extend((branch, level + 2) for branch in v.values())
            elif k == "ui":
                out.append((v, level + 1, 1, k, False, None, True, False, False, False))
            else:
                out.append((v, level + 1, 0, k, False, False, False, False, False, False))
    return out


def _check_on_complete(value: Any) -> None:
    """A flow's ``onComplete``: only ``{"kind": "chat", "message": <plain text of
    at most ASK_MAX chars>}``. invoke_tool, call_binding, create_pocket,
    navigate, emit and any other kind run something no card may; an expression
    would send what it resolves to, unchecked."""
    if not (
        isinstance(value, dict)
        and value.get("kind") == "chat"
        and value.keys() <= {"kind", "message"}
        and isinstance(value.get("message"), str)
        and len(value["message"]) <= ASK_MAX
        and "{" not in value["message"]
    ):
        raise _Reject("a flow onComplete that is not a chat message")


def _check_form(props: Any, lead_capture: bool) -> None:
    """A form's prefill values, and the lead card's rules (see the header)."""
    if not isinstance(props, dict):
        return
    fields = props.get("fields")
    named: set[str] = set()
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict):
            continue
        if "value" in f and not (
            isinstance(f["value"], str) and len(f["value"]) <= FORM_PREFILL_MAX
        ):
            raise _Reject(f"a form value is not text of at most {FORM_PREFILL_MAX} characters")
        if isinstance(f.get("name"), str):
            named.add(f["name"])
    if props.get("verb") != LEAD_VERB:
        return
    if not lead_capture:
        raise _Reject("lead cards are off for this site")
    if not named or not named <= LEAD_FIELDS:
        raise _Reject("a lead card takes only name, email, phone and message")
    if not named & LEAD_CONTACT_FIELDS:
        raise _Reject("a lead card needs an email or phone field")


def _check_tree(
    root: Any,
    events: list[str],
    lead_capture: bool = False,
    profile: CardProfile = PAWBAR_PROFILE,
) -> None:
    """paw-bar's checkTree, plus the widget set, the event rules and the form
    rules. ``events`` are the host events this widget declares (a subset of
    ``HOST_EVENTS``); ``lead_capture`` allows the lead card; ``profile`` gives
    the widget set, actions and bounds."""
    count = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal count
        if not isinstance(node, dict):
            raise _Reject("a node is not an object")
        if not isinstance(node.get("type"), str):
            raise _Reject("a node has no type")
        count += 1
        if count > profile.max_nodes:
            raise _Reject(f"more than {profile.max_nodes} nodes")
        if depth > profile.max_depth:
            raise _Reject(f"nested deeper than {profile.max_depth}")
        if node["type"] not in profile.widget_types:
            raise _Reject(f"unknown widget type {node['type']!r}")
        _check_events(node, events, profile.actions)
        if node["type"] == "form":
            _check_form(node.get("props"), lead_capture)
        for key in ("children", "else_children"):
            kids = node.get(key)
            if kids is None:
                continue
            if not isinstance(kids, list):
                raise _Reject(f"{key} is not a list")
            for kid in kids:
                walk(kid, depth + 1)

    walk(root, 1)


def _hydrate(node: dict[str, Any], index: dict[str, Any], verbs: list[str]) -> dict | None:
    """A copy of ``node`` with product-cards filled from the catalog; None when
    ``node`` is a product-card with nothing left to show. A flow card's typeless
    root is copied as it is (no ripple widget is a product-card)."""
    out = dict(node)
    if out.get("type") == _PRODUCT_CARD:
        props = out.get("props") if isinstance(out.get("props"), dict) else {}
        items = _items(props.get("ids"), index, verbs)
        if not items:
            return None
        # Replaced whole: nothing the model put in props (a name, a price) survives.
        out["props"] = {"items": items}
    for key in ("children", "else_children"):
        if key in out:
            kept = [_hydrate(kid, index, verbs) for kid in out[key]]
            out[key] = [kid for kid in kept if kid is not None]
    return out


# What a booking says when the store's times could not be read.
STORE_DOWN_NOTICE: dict[str, str] = {
    "kind": "info",
    "text": "Times are unavailable right now. Please try again in a little while.",
}
# Item fields only the store writes on an item with a product_id.
_STORE_ITEM_KEYS = ("name", "description", "price", "image", "category", "tags", "kind", "groups")


def _fill_store(value: Any, store: Any, profile: CardProfile) -> Any:
    """A copy of a ripple ``ui`` with every menu-order, booking and comparison-layout
    node, at any depth (children, node props, flow steps), filled from ``store``
    (a ``concierge_store.StoreData``; None when the site names no store). Runs
    after the walk, so nothing it writes is checked again: the store data is
    cleaned when read, and nothing here reads a URL or a handler from the model."""
    if isinstance(value, list):
        return [_fill_store(v, store, profile) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _fill_store(v, store, profile) for k, v in value.items()}
    fill = _STORE_FILLS.get(out.get("type"))
    if fill is not None:
        props = out.get("props")
        out["props"] = fill(dict(props) if isinstance(props, dict) else {}, store)
        wired = SERVER_WIRED.get(out["type"])
        if (
            wired
            and store is not None
            and wired[1] in profile.host_events
            and _usable(out["props"])
        ):
            out[wired[0]] = {"action": "emit", "target": wired[1]}
    return out


def _usable(props: dict[str, Any]) -> bool:
    """Whether a filled widget can fire: a menu that checks out, a booking with the
    store's services and days. With no store nothing is wired: the card is display."""
    return props.get("checkout") is True or bool(props.get("days") and props.get("services"))


def _store_items(items: Any, store: Any, *, menu: bool) -> tuple[list[Any], bool]:
    """(items, every one filled) for a menu-order's or comparison's ``items``: an
    item with a product_id takes its store fields (the model's dropped), an
    unknown or repeated product_id drops the item; with no menu (``store`` None
    or unreachable) the model's price, image and options are dropped instead.
    ``menu`` (menu-order): every store field; else name, price and image."""
    import copy

    products = getattr(store, "products", None)
    keys = _STORE_ITEM_KEYS if menu else ("name", "price", "image")
    out: list[Any] = []
    seen: set[str] = set()
    filled = isinstance(items, list) and bool(items)
    for item in items if isinstance(items, list) else []:
        pid = item.get("product_id") if isinstance(item, dict) else None
        if not isinstance(pid, str) or not pid.strip():
            filled = False
            out.append(item)
            continue
        if products is None:
            filled = False
            out.append({k: v for k, v in item.items() if k not in ("price", "image", "groups")})
            continue
        pid = pid.strip()
        product = products.get(pid)
        if pid in seen or product is None:
            continue
        seen.add(pid)
        mine = {k: v for k, v in item.items() if k not in keys}
        out.append({**mine, **{k: copy.deepcopy(product[k]) for k in keys if k in product}})
    return out, filled and bool(out) and products is not None


def _fill_menu_order(props: dict[str, Any], store: Any) -> dict[str, Any]:
    """Items from the store's menu; ``checkout`` true only when every item filled."""
    props.pop("checkout", None)
    if store is None:
        return props
    props["items"], filled = _store_items(props.get("items"), store, menu=True)
    if store.products is not None:
        props["currency"] = store.currency
        props["fulfilment"] = list(store.fulfilment)
        if store.delivery_fee is not None:
            props["fee"] = {"delivery": store.delivery_fee}
        else:
            props.pop("fee", None)
    if filled:
        props["checkout"] = True
    return props


def _fill_booking(props: dict[str, Any], store: Any) -> dict[str, Any]:
    """Services and 7 days of slots from the store; a calm notice when it is down.
    ``confirmed`` and ``notice`` are the host's to write."""
    import copy

    props.pop("confirmed", None)
    props.pop("notice", None)
    if store is None:
        return props
    for key in ("services", "days", "tz"):
        props.pop(key, None)
    if store.services:
        props["services"] = copy.deepcopy(list(store.services))
    if store.services and store.days:
        props["days"] = copy.deepcopy(list(store.days))
        if store.tz:
            props["tz"] = store.tz
    else:
        props["notice"] = dict(STORE_DOWN_NOTICE)
    return props


def _fill_comparison(props: dict[str, Any], store: Any) -> dict[str, Any]:
    """Name, price and image for each item with a product_id."""
    if store is None or not any(
        isinstance(i, dict) and "product_id" in i for i in props.get("items") or []
    ):
        return props
    props["items"], _ = _store_items(props.get("items"), store, menu=False)
    if store.products is not None:
        props["currency"] = store.currency
    return props


_STORE_FILLS = {
    "menu-order": _fill_menu_order,
    "booking": _fill_booking,
    "comparison-layout": _fill_comparison,
}


def validate_and_hydrate(
    spec: dict,
    catalog: Iterable[Any] | None,
    *,
    verbs: Iterable[str] | None = HOST_EVENTS,
    lead_capture: bool = False,
    profile: CardProfile = PAWBAR_PROFILE,
    storefront: Any = None,
) -> dict | None:
    """The card to send, or None to drop it.

    ``spec`` is the parsed fence JSON (``{"ui": ..., "state"?: ...}``); ``catalog``
    the widget's catalog (``PawBarCatalogItem`` models or dicts); ``verbs`` the
    widget's declared action verbs, of which only add_to_cart / checkout become a
    product's buttons (the action endpoint refuses an undeclared verb). The result
    holds only ``ui`` and ``state``: a spec's ``theme`` is dropped, as paw-bar
    drops it. It is re-checked after hydration, since filling ids in makes it
    longer and paw-bar measures what it receives. ``lead_capture`` (the site's
    ``concierge_lead_capture``) allows a ``send_to_team`` form; off by default.
    ``profile`` (the site's ``CardProfile``) gives the widget set and bounds. On a
    strict profile ``storefront`` (``concierge_store.StoreData``, None: no store)
    fills the store widgets (``_fill_store``)."""
    try:
        if not isinstance(spec, dict) or "ui" not in spec:
            raise _Reject("not a spec")
        if not isinstance(spec["ui"], dict):
            raise _Reject("ui is not an object")
        # A non-object state is refused; paw-bar has always read a null one as
        # no state, so only a strict profile refuses null.
        state = spec.get("state")
        if not isinstance(state, dict) and (
            state is not None or (profile.strict and "state" in spec)
        ):
            raise _Reject("state is not an object")
        if len(_serialize(spec)) > profile.max_chars:
            raise _Reject(f"longer than {profile.max_chars} characters")
        events = _card_verbs(verbs)
        if profile.strict:
            _check_strict(spec, events, lead_capture, profile)
        else:
            _check_tree(spec["ui"], events, lead_capture, profile)
        state = spec.get("state")
        ui = _hydrate(spec["ui"], _catalog_index(catalog), events)
        if ui is None:
            return None
        if profile.strict:
            ui = _fill_store(ui, storefront, profile)
        out: dict[str, Any] = {"ui": ui}
        if state is not None:
            out["state"] = state
        body = _serialize(out)
        if len(body) > profile.max_chars or _FENCE in body:
            return None
        return out
    except (_Reject, RecursionError):
        return None
    except Exception as exc:  # noqa: BLE001 — a card the checks can't read is dropped
        _log_dropped(exc)
        return None


def _log_dropped(exc: Exception) -> None:
    # The type only: the message or a traceback could quote the card.
    logger.warning("card_spec: dropped a card the checks could not read (%s)", type(exc).__name__)


# --------------------------------------------------------------------------- #
# Fence bodies
# --------------------------------------------------------------------------- #


# A body too deeply nested to parse here, or (strict) one repeating a key in an
# object. Never passed through as a legacy card: the client's parser might read
# it, and nothing here checked it (JSON.parse keeps a repeated key's last value).
_TOO_DEEP = object()


class _DuplicateKey(Exception):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise _DuplicateKey
    return out


# What JS ``String.prototype.trim`` strips (ECMAScript WhiteSpace and
# LineTerminator). paw-bar trims a fence body with ``trimEnd()`` before
# ``JSON.parse``; Python's ``json.loads`` refuses most of these.
_JS_SPACE = (
    "\t\n\v\f\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


def _parse(body: str, unique: bool = False) -> Any:
    """The parsed body, ``_JS_SPACE`` stripped first; None when it is not JSON
    (NaN, Infinity or a number too big for a float included: ``card.final``
    must be JSON); ``_TOO_DEEP`` when it is too deep to read, or, with
    ``unique``, repeats a key in an object."""
    try:
        return json.loads(
            body.strip(_JS_SPACE),
            object_pairs_hook=_unique if unique else None,
            parse_constant=_no_constant,
            parse_float=_finite,
        )
    except ValueError:
        return None
    except (RecursionError, _DuplicateKey):
        return _TOO_DEEP


def _is_spec(raw: Any) -> bool:
    return isinstance(raw, dict) and "ui" in raw


# --------------------------------------------------------------------------- #
# Closer repair (strict profile)
# --------------------------------------------------------------------------- #


# Most closing brackets a repair may insert in one body.
MAX_REPAIR_CLOSERS = 4
# ponytail: a body naming "state" inside its ui more often than this is not
# rescanned past the last ones (each try reads the whole body).
_MAX_REPAIR_CUTS = 8
_CLOSER = {"{": "}", "[": "]"}
_JSON_SPACE = " \t\n\r"
_TOKEN = re.compile(r'["{}\[\]]')


def _closers(opened: str) -> str:
    return "".join(_CLOSER[ch] for ch in reversed(opened))


def _string_end(text: str, i: int) -> int:
    """The index past the string opening at ``text[i]``, -1 when it never closes."""
    j = i + 1
    while (m := _STRING_STOP.search(text, j)) is not None:
        if m.group() == '"':
            return m.end()
        j = m.end() + 1  # a backslash: skip the escaped character
    return -1


def _scan_open(text: str) -> tuple[str, list[tuple[int, str]]] | None:
    """``text`` read for its brackets, strings skipped: the containers still open
    at its end (openers, outermost first), and for each ``,"state":`` key read
    while the root's ``ui`` member was open, its comma's index and the containers
    open there. None when ``text`` is not an object whose closers all match, ends
    inside a string, or goes on after its root closes."""
    if not text.startswith("{"):
        return None
    stack: list[str] = []
    cuts: list[tuple[int, str]] = []
    root_key = ""
    i = 0
    while (m := _TOKEN.search(text, i)) is not None:
        i, ch = m.start(), m.group()
        if ch == '"':
            end = _string_end(text, i)
            if end < 0:
                return None
            j = end
            while j < len(text) and text[j] in _JSON_SPACE:
                j += 1
            if j < len(text) and text[j] == ":":
                key = text[i + 1 : end - 1]
                if len(stack) == 1:
                    root_key = key
                elif key == "state" and root_key == "ui":
                    k = i - 1
                    while text[k] in _JSON_SPACE:
                        k -= 1
                    if text[k] == ",":
                        cuts.append((k, "".join(stack)))
            i = end
            continue
        if ch in _CLOSER:
            stack.append(ch)
        elif not stack or _CLOSER[stack.pop()] != ch:
            return None
        i += 1
        if not stack:
            return ("", cuts) if not text[i:].strip(_JSON_SPACE) else None
    return "".join(stack), cuts


def _closer_fixes(text: str) -> list[str]:
    """``text`` with only closing brackets inserted, fewest first: before a
    ``,"state":`` read inside ``ui`` the containers of the ui subtree are closed
    (preferred on a tie), and at the end whatever is still open. A fix needing no
    closer, or more than ``MAX_REPAIR_CLOSERS``, is left out."""
    scan = _scan_open(text)
    if scan is None:
        return []
    opened, cuts = scan
    fixes: list[tuple[int, int, str]] = []
    near = [cut for cut in cuts if len(cut[1]) - 1 <= MAX_REPAIR_CLOSERS]
    for pos, at in near[-_MAX_REPAIR_CUTS:]:
        head = text[:pos] + _closers(at[1:])
        rest = _scan_open(head + text[pos:])
        if rest is not None:
            fixed = head + text[pos:] + _closers(rest[0])
            fixes.append((len(fixed) - len(text), 0, fixed))
    if opened:
        fixes.append((len(opened), 1, text + _closers(opened)))
    fixes.sort(key=lambda fix: fix[:2])
    return [fixed for added, _, fixed in fixes if 0 < added <= MAX_REPAIR_CLOSERS]


# What may follow a root that closed one ``}`` early: the root's own state.
_STATE_TAIL = re.compile(r'[ \t\n\r]*,[ \t\n\r]*"state"[ \t\n\r]*:[ \t\n\r]*(?=\{)')


def _surplus_closer_fix(text: str) -> str | None:
    """``text`` whose root object closed one ``}`` early, the rest of it exactly
    ``,"state":<object>}``, with that one surplus closer dropped; else None.
    Two surplus closers leave a ``}`` before the comma, so they never match."""
    decode = json.JSONDecoder().raw_decode
    try:
        _, end = decode(text)
        tail = _STATE_TAIL.match(text, end)
        if tail is None:
            return None
        _, stop = decode(text, tail.end())
    except (ValueError, RecursionError):
        return None
    if text[stop:].strip(_JSON_SPACE) != "}":
        return None
    return text[: end - 1] + text[end:]


def _repaired(body: str, profile: CardProfile) -> str | None:
    """On a strict profile, a ``body`` that is not JSON with the fewest closing
    brackets inserted (``_closer_fixes``), or its one surplus closer before the
    root's state dropped (``_surplus_closer_fix``), that make it JSON holding a
    spec; else None. The caller parses and checks the result like any body."""
    if not profile.strict or len(body) > profile.max_chars:
        return None
    text = body.strip(_JS_SPACE)
    for fixed in [*_closer_fixes(text), _surplus_closer_fix(text)]:
        if fixed is not None and _is_spec(_parse(fixed)):
            return fixed
    return None


# --------------------------------------------------------------------------- #
# Flat props repair (strict profile)
# --------------------------------------------------------------------------- #


# Node keys never moved into ``props``, even where a widget declares one by that
# name (button and input declare ``type``, input ``bind``); handler keys neither.
_NODE_KEYS: frozenset[str] = frozenset(
    {"type", "id", "props", "children", "else_children", "show", "each"}
    | {"item_as", "index_as", "bind", "state"}
)
# Each ripple widget's declared props a flat node key may be lifted into: not a
# node key, not a handler key, and not ``value`` on a widget that binds it.
_LIFTABLE: dict[str, frozenset[str]] = {
    w["type"]: frozenset(
        k
        for k in w.get("props") or {}
        if k not in _NODE_KEYS
        and not _is_handler_key(k)
        and not (k == "value" and "bind" in w["props"])
    )
    for w in RIPPLE_MANIFEST["widgets"]
}


def _is_alias_node(value: dict[str, Any], canonical: str) -> bool:
    """A node written under an alias of ``canonical``, not a data row that happens to
    share the name: every key a node key, a handler or one of the widget's props, and
    one of them more than ``type``."""
    known = _NODE_KEYS | _LIFTABLE.get(canonical, frozenset())
    return len(value) > 1 and all(k in known or _is_handler_key(k) for k in value)


def _lift_flat_props(ui: Any, profile: CardProfile) -> tuple[Any, int]:
    """``ui`` with, in place, every widget node's top-level keys that its manifest
    entry declares as props (``_LIFTABLE``) moved into ``props`` (made when
    missing; a key ``props`` already holds stays put, the flat one beside it),
    at any depth: children, node slots inside props, flow steps, a node written under
    an alias (``RIPPLE_ALIASES``, ``_is_alias_node``) renamed first. A node's own
    ``props`` object is never read as a node. Strict profile only; the caller
    checks the result in full. Returns it and how many keys moved."""
    if not profile.strict:
        return ui, 0
    moved = 0
    stack = [ui]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        props = value.get("props", {})
        canonical = RIPPLE_ALIASES.get(kind) if isinstance(kind, str) else None
        if canonical and isinstance(props, dict) and _is_alias_node(value, canonical):
            value["type"] = canonical
            if canonical == "board-game" and "game" not in {*value, *props}:
                value["props"] = props = {**props, "game": kind}
            kind = canonical
        if isinstance(kind, str) and isinstance(props, dict):
            liftable = _LIFTABLE.get(kind, frozenset())
            flat = [k for k in value if k in liftable and k not in props]
            if flat:
                value["props"] = props = {**props, **{k: value.pop(k) for k in flat}}
                moved += len(flat)
            # A node: its props' values may hold nodes, never the props object.
            stack.extend(props.values())
            stack.extend(v for k, v in value.items() if k != "props")
            continue
        stack.extend(value.values())
    return ui, moved


def _legacy_product(card: dict, index: dict[str, Any], verbs: list[str]) -> dict | None:
    ids = [item.get("id") for item in card.get("items") or [] if isinstance(item, dict)]
    items = _items(ids, index, verbs)
    return {"kind": "product", "items": items} if items else None


def render_card(
    body: str,
    catalog: Iterable[Any] | None,
    *,
    verbs: Iterable[str] | None = HOST_EVENTS,
    lead_capture: bool = False,
    profile: CardProfile = PAWBAR_PROFILE,
    storefront: Any = None,
) -> str | None:
    """The complete ```pawbar-card fence to emit for a fence ``body``, or None to
    drop it. A Ripple spec is validated (against ``profile``) and hydrated; a
    legacy product card is repriced from the catalog; a legacy form card is held
    to the form rules; any other legacy card passes through verbatim; a body
    that is not JSON is dropped, unless the profile is strict and only closing
    brackets are missing from a spec (``_repaired``). Never raises: a card no check can read is
    dropped and logged by error type only."""
    try:
        return _render_card(body, catalog, verbs, lead_capture, profile, storefront)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        _log_dropped(exc)
        return None


def _render_card(
    body: str,
    catalog: Iterable[Any] | None,
    verbs: Iterable[str] | None,
    lead_capture: bool,
    profile: CardProfile,
    storefront: Any = None,
) -> str | None:
    raw = _parse(body, unique=profile.strict)
    if raw is None and (fixed := _repaired(body, profile)) is not None:
        # Only closers were missing, or one too many: the repaired body is read
        # and checked as any other. Logged by count only, never the card.
        added = len(fixed) - len(body.strip(_JS_SPACE))
        if added > 0:
            logger.info("card_spec: repaired a card missing %d closing bracket(s)", added)
        else:
            logger.info("card_spec: repaired a card with a surplus closer before its state")
        body, raw = fixed, _parse(fixed, unique=profile.strict)
    if raw is None or raw is _TOO_DEEP:
        return None
    if _is_spec(raw):
        # Props written flat on a node are moved under props, then checked in full.
        raw["ui"], lifted = _lift_flat_props(raw["ui"], profile)
        if lifted:
            logger.info("card_spec: lifted %d flat prop(s) into props", lifted)
        # Measured as paw-bar measures it: CRLF folded, trailing whitespace trimmed.
        if len(body.replace("\r\n", "\n").rstrip(_JS_SPACE)) > profile.max_chars:
            return None
        spec = validate_and_hydrate(
            raw,
            catalog,
            verbs=verbs,
            lead_capture=lead_capture,
            profile=profile,
            storefront=storefront,
        )
        return None if spec is None else f"{_FENCE}pawbar-card\n{_serialize(spec)}\n{_FENCE}"
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and (not isinstance(kind, str) or kind in ("", "product")):
        # paw-bar reads a card with no kind as a product card.
        card = _legacy_product(raw, _catalog_index(catalog), _card_verbs(verbs))
        if card is None:
            return None
        text = _serialize(card)
        return None if _FENCE in text else f"{_FENCE}pawbar-card\n{text}\n{_FENCE}"
    if isinstance(raw, dict) and kind == "form":
        try:
            _check_form(raw, lead_capture)
        except _Reject:
            return None
    return f"{_FENCE}pawbar-card\n{body}{_FENCE}"


def has_lead_form(body: str, profile: CardProfile = PAWBAR_PROFILE) -> bool:
    """Whether a card body holds a send_to_team form, as a Ripple spec node (in
    any flow step too) or a legacy ``{"kind": "form"}`` card. Says nothing about
    whether it is valid: ask it of a body ``render_card`` passed, with the same
    ``profile`` (a strict one reads a body missing only closers as repaired)."""
    raw = _parse(body)
    if raw is None and (fixed := _repaired(body, profile)) is not None:
        raw = _parse(fixed)
    if isinstance(raw, dict) and raw.get("kind") == "form":
        return raw.get("verb") == LEAD_VERB
    if not _is_spec(raw):
        return False
    ui = raw["ui"]
    stack = [ui]
    if isinstance(ui, dict) and any(field in ui for field in _FLOW_FIELDS):
        try:
            stack = [entry[0] for entry in _flow_steps(ui) if entry[3] == "ui"]
        except _Reject:
            return False
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        props = node.get("props")
        if node.get("type") == "form" and isinstance(props, dict):
            if props.get("verb") == LEAD_VERB:
                return True
        for key in ("children", "else_children"):
            kids = node.get(key)
            if isinstance(kids, list):
                stack.extend(kids)
    return False


def _ids_in(node: Any, out: dict[str, None], budget: list[int]) -> None:
    if not isinstance(node, dict) or budget[0] <= 0:
        return
    budget[0] -= 1
    if node.get("type") == _PRODUCT_CARD:
        props = node.get("props") if isinstance(node.get("props"), dict) else {}
        for raw in props.get("ids") if isinstance(props.get("ids"), list) else []:
            if isinstance(raw, str) and raw.strip():
                out.setdefault(raw.strip(), None)
    for key in ("children", "else_children"):
        kids = node.get(key)
        for kid in kids if isinstance(kids, list) else []:
            _ids_in(kid, out, budget)


def card_ids(body: str, profile: CardProfile = PAWBAR_PROFILE) -> list[str]:
    """The catalog ids a fence body names: a Ripple spec's product-card ``ids``,
    or a legacy product card's item ids. What a lookup must fetch before
    ``render_card`` can hydrate it; at most ``MAX_CARD_IDS``, first seen first,
    reading at most ``profile.max_nodes`` nodes."""
    raw = _parse(body)
    out: dict[str, None] = {}
    if _is_spec(raw):
        _ids_in(raw["ui"], out, [profile.max_nodes])
    elif isinstance(raw, dict):
        for item in raw.get("items") if isinstance(raw.get("items"), list) else []:
            pid = item.get("id") if isinstance(item, dict) else None
            if isinstance(pid, str) and pid.strip():
                out.setdefault(pid.strip(), None)
    return list(out)[:MAX_CARD_IDS]


_STRING_STOP = re.compile(r'["\\]')
_TILE_KEYS = ("tiles", "tile", "tileUrl")


def _string_fails(value: str, key: str, styled: bool, hosts: frozenset[str]) -> bool:
    """Whether ``_check_strict`` would refuse this string value under ``key``.
    ``styled``: some key on its path is "style", so it may be CSS context; the
    checks that depend on that are skipped rather than guessed."""
    if key == "svg":
        return False  # an illustration's svg: its own policy runs on the finished card
    try:
        if key in ("style", "background"):
            _check_css(value, declarations=key == "style")
        elif not styled and (key in _TILE_KEYS or _is_url_key(key)):
            _check_url(key, value, hosts)
        if "{" in value and _URL_ISH_KEY.search(key):
            raise _Reject("an expression where a URL is read")
        _check_text(value, hosts)
    except _Reject:
        return True
    return False


class PartialScan:
    """The strict string checks over a card body as it streams. ``feed`` takes
    the next text and returns True once the body holds a DEFINITE violation: a
    COMPLETE string value under the root's ``ui`` / ``state`` that
    ``_check_strict`` would refuse in any finished card (a body with no ``ui``
    at all is refused at the close anyway), or a key repeated in one object (the
    close refuses that body whole, and the bytes after it would replace what was
    checked). A string still open is never judged, so a half-written ``"/pa`` or
    ``"java`` passes. Never True for a non-strict profile. The body is read as
    ``render_card`` reads it: what JS ``trim`` strips (``_JS_SPACE``) is skipped
    before the root, and a root that is not an object is a violation at once. A
    body that stops being JSON later stops being scanned (the close refuses it)."""

    def __init__(self, profile: CardProfile) -> None:
        self._hosts = profile.url_hosts
        self._done = not profile.strict
        # Open containers: [is object, key (object: current; array: inherited),
        # expecting a key, a "style" key on the path, the object's keys so far].
        self._stack: list[list[Any]] = []
        self._raw: list[str] | None = None  # the open string's raw text
        self._escape = False
        self.hit = False

    def feed(self, text: str) -> bool:
        i, n = 0, len(text)
        while i < n and not (self._done or self.hit):
            if self._raw is not None:
                if self._escape:
                    self._raw.append(text[i])
                    self._escape, i = False, i + 1
                    continue
                m = _STRING_STOP.search(text, i)
                if m is None:
                    self._raw.append(text[i:])
                    break
                k = m.start()
                self._raw.append(text[i : k + 1])
                i = k + 1
                if text[k] == "\\":
                    self._escape = True
                    continue
                raw, self._raw = "".join(self._raw)[:-1], None
                self._string(raw)
                continue
            ch, i = text[i], i + 1
            if not self._stack and ch != "{":
                self.hit = ch not in _JS_SPACE  # before the root: trimmed, or not a card
            elif ch == '"':
                self._raw = []
            elif ch in "{[":
                self._open(ch == "{")
            elif ch in "}]":
                if self._stack:
                    self._stack.pop()
                self._done = not self._stack
            elif ch == "," and self._stack and self._stack[-1][0]:
                self._stack[-1][2] = True
        return self.hit

    def _open(self, is_obj: bool) -> None:
        if not self._stack:  # the root, always an object (``feed``)
            self._stack.append([True, "", True, False, set()])
            return
        top = self._stack[-1]
        top[2] = False
        # Directly under the root, the walk starts with key "" (as _check_strict).
        key = top[1] if len(self._stack) > 1 else ""
        styled = top[3] or key == "style"
        self._stack.append([is_obj, "" if is_obj else key, is_obj, styled, set()])

    def _string(self, raw: str) -> None:
        try:
            value = json.loads(f'"{raw}"')
        except ValueError:
            self._done = True  # not JSON: the close refuses it
            return
        top = self._stack[-1] if self._stack else None
        if top is None:
            self._done = True
            return
        if top[0] and top[2]:
            if value in top[4]:
                self.hit = True
                return
            top[1], top[2] = value, False
            top[4].add(value)
            return
        if self._stack[0][1] not in ("ui", "state"):
            return
        root = len(self._stack) == 1
        key = "" if root else top[1]
        styled = not root and (top[3] or (top[0] and key == "style"))
        self.hit = _string_fails(value, key, styled, self._hosts)


def scan_partial(body: str, profile: CardProfile) -> bool:
    """``PartialScan(profile).feed(body)``: whether a partial card body already
    holds a violation the finished card cannot escape."""
    return PartialScan(profile).feed(body)


def card_verdict(
    body: str,
    catalog: Iterable[Any] | None,
    *,
    verbs: Iterable[str] | None = HOST_EVENTS,
    lead_capture: bool = False,
) -> Literal["accept", "reject", "legacy"]:
    """The parity-fixture verdict for a fence body: ``legacy`` when it is not a
    Ripple spec, else ``accept`` when it would be emitted, ``reject`` when not."""
    if not _is_spec(_parse(body)):
        return "legacy"
    out = render_card(body, catalog, verbs=verbs, lead_capture=lead_capture)
    return "reject" if out is None else "accept"


# --------------------------------------------------------------------------- #
# The prompt's view of the manifest
# --------------------------------------------------------------------------- #


# A typed line's type for one prop, at most this long.
_TYPE_CHARS = 90
# A union of 12 or more string literals (habit-tracker's icons) prints as a string: the
# widget's description names the ones that matter, and the row's later fields stay in view.
_LONG_ENUM = re.compile(r'(?:"[^"]*"\s*\|\s*){11,}"[^"]*"')
# A typed line's description recapping a row shape its field list already gives
# (``cards[{front, back}]``) keeps just the name; a recap with a note in parentheses
# (``answer (index)``) adds something, so it stays.
_SHAPE_RECAP = re.compile(r"\b(\w+)\[\{[^{}()\[\]]*\}\]")
# Props every data widget shares; the ripple rules teach them once, so a typed
# line leaves them out.
_COMMON_PROPS: frozenset[str] = frozenset({"title", "subtitle", "verdict", "currency"})
# ``: string`` before a delimiter: string is the default, so a typed line omits it.
_STRING_FIELD = re.compile(r": string(?=\s*(?:[;,}\]]|$))")


def _short_type(text: str) -> str:
    """A prop's TypeScript type as a typed line prints it: ``Array<X>`` as
    ``[X]``, no quotes, ``string`` left out (empty for a plain string), tight
    separators, capped at ``_TYPE_CHARS``."""
    text = " ".join(_LONG_ENUM.sub("string", text).split()).replace('"', "")
    while (shorter := re.sub(r"Array<((?:[^<>]|<[^<>]*>)*)>", r"[\1]", text)) != text:
        text = shorter
    text = _STRING_FIELD.sub("", text)
    if text == "string":
        return ""
    for wide, tight in (
        ("; ", ","),
        (", ", ","),
        (" | ", "|"),
        (": ", ":"),
        ("{ ", "{"),
        (" }", "}"),
    ):
        text = text.replace(wide, tight)
    if len(text) > _TYPE_CHARS:
        text = text[: _TYPE_CHARS - 1] + "…"
    return text


def _widget_line(
    widget: dict[str, Any], typed: bool = False, shown: frozenset[str] | None = None
) -> str:
    def entry(name: str, spec: dict[str, Any]) -> str:
        head = name + ("" if spec.get("required") else "?")
        kind = _short_type(str(spec.get("type", ""))) if typed else ""
        return f"{head}: {kind}" if kind else head

    def listed(fields: dict[str, Any] | None) -> list[tuple[str, Any]]:
        return [
            (n, s)
            for n, s in (fields or {}).items()
            if (n in shown if shown is not None else not (typed and n in _COMMON_PROPS))
        ]

    props = [entry(name, spec) for name, spec in listed(widget.get("props"))]
    props += [name for name, _ in listed(widget.get("events"))]
    props += [entry(name, spec) for name, spec in listed(widget.get("nodeFields"))]
    text = str(widget.get("description", ""))
    if typed:
        text = _SHAPE_RECAP.sub(r"\1", text)
    return f"- {widget['type']} {{{', '.join(props)}}}: {text}"


# A summary line's description: its first sentence, at most this long.
_BRIEF_CHARS = 73


def _brief_line(widget: dict[str, Any]) -> str:
    text = " ".join(str(widget.get("description", "")).split())
    first = text.split(". ", 1)[0]
    if len(first) > _BRIEF_CHARS:
        first = first[: _BRIEF_CHARS - 1].rstrip() + "…"
    return f"- {widget['type']}: {first}"


def compact_manifest(profile: CardProfile = PAWBAR_PROFILE) -> str:
    """One line per widget: its type, its props (``?`` = optional; a
    ``profile.typed`` widget's with a short type), events and node fields, and
    what it is for; a widget outside ``profile.detailed`` gets only its type and
    the first sentence of what it is for."""
    return "\n".join(
        _brief_line(w)
        if profile.detailed is not None and w["type"] not in profile.detailed
        else _widget_line(w, typed=True, shown=profile.typed[w["type"]])
        if w["type"] in profile.typed
        else _widget_line(w)
        for w in profile.manifest["widgets"]
        if w["type"] in profile.widget_types
    )


__all__ = [
    "ASK_EVENT",
    "ASK_HANDLERS",
    "ASK_MAX",
    "FLOW_EVENTS",
    "FLOW_STEP_KEYS",
    "MAX_FLOW_STEPS",
    "PAWBAR_PROFILE",
    "PartialScan",
    "RIPPLE_CHROME",
    "RIPPLE_DATA_WIDGETS",
    "RIPPLE_DEFERRED",
    "RIPPLE_MANIFEST_PATH",
    "RIPPLE_PROFILE",
    "CardProfile",
    "DEFERRED_WIDGETS",
    "FORM_PREFILL_MAX",
    "HOST_EVENTS",
    "BILL_SPLIT",
    "RIPPLE_ALIASES",
    "RIPPLE_PLAY_WIDGETS",
    "ILLUSTRATION",
    "LEAD_CONTACT_FIELDS",
    "LEAD_FIELDS",
    "LEAD_VERB",
    "MANIFEST_PATH",
    "MAX_SPEC_CHARS",
    "MAX_SPEC_DEPTH",
    "MAX_SPEC_NODES",
    "MAX_CARD_IDS",
    "SPEC_ACTIONS",
    "WIDGET_TYPES",
    "card_ids",
    "card_verdict",
    "compact_manifest",
    "has_lead_form",
    "render_card",
    "scan_partial",
    "validate_and_hydrate",
]
