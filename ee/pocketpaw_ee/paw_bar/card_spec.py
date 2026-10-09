# ee/pocketpaw_ee/paw_bar/card_spec.py — bound and hydrate the cards a v2 concierge writes.
#
# A v2 reply can carry a generated UI as a ```pawbar-card fence whose JSON has a
# ``ui`` node tree (a Ripple spec). paw-bar draws it on a customer's page, so the
# server checks it before any of it leaves:
#
#   * the same bounds paw-bar's lib/spec-card.ts applies: 32,000 chars, 80 nodes,
#     depth 8 (root is 1), ``children`` / ``else_children`` lists, ``state`` an
#     object, so a card the server passes is never one the client refuses;
#   * every node ``type`` is a widget in the vendored pawbar-manifest.json (minus
#     ``DEFERRED_WIDGETS``, which the server doesn't back yet), and every event
#     action is one the manifest lists, with ``emit`` limited to the add_to_cart /
#     checkout host events the widget declares;
#   * a ``form``'s prefill ``value``s are strings of at most ``FORM_PREFILL_MAX``.
#     A form whose verb is ``send_to_team`` (the lead card) passes only with
#     ``lead_capture`` on (the site's ``concierge_lead_capture``), only with
#     fields from ``LEAD_FIELDS`` and only with an email or phone field among
#     them. The legacy ``{"kind": "form"}`` card is held to the same form rules;
#   * product data comes only from the site catalog: a ``product-card``'s ``ids``
#     become ``items`` (name, price, currency, image, page url, description),
#     unknown ids are dropped, an empty product-card is dropped. A legacy
#     ``{"kind": "product"}`` card is repriced the same way; other legacy cards
#     pass through untouched. ``card_ids`` says which catalog items to fetch;
#     ``has_lead_form`` says whether a card is the lead card.
#
# Every rule above reads a ``CardProfile``: the widget set, the action set and
# the bounds. ``PAWBAR_PROFILE`` (the default everywhere) is the paw-bar widget's
# set and bounds above. ``RIPPLE_PROFILE`` (a site whose ``concierge_ui_profile``
# is "ripple") takes the full Ripple catalog from ripple-manifest.json (minus
# ``RIPPLE_DEFERRED``), the same actions and host events, and 400 nodes / depth 16
# / 64,000 chars. The full catalog keeps nodes, handlers and links in props, so
# a ``strict`` profile (``_check_strict``) walks the whole ``ui`` AND ``state``,
# iteratively and depth-capped: any node found in a prop (a popover's
# ``content``) is a node, held to every node rule; any action, under a handler
# key or named as a manifest action anywhere, must be an allowed one (audit-log
# entries' own ``action`` field is data); a form may not carry a native submit
# target; every URL-valued key must be a same-site path or an https URL on
# ``url_hosts`` (empty: none); and no text may hold a javascript: link.
#
# pawbar-manifest.json is vendored byte-for-byte from paw-bar's
# app/pawbar-manifest.json; ripple-manifest.json from @ripple-ui/svelte's
# dist/manifest.json minus each widget's example (the ``.source`` file beside
# it says how). The drift tests in
# tests/cloud/test_paw_bar_concierge_v2_output.py and
# tests/cloud/test_paw_bar_ripple_profile.py pin both hashes and say how to
# refresh them. The shared parity fixtures live in tests/fixtures/card_parity/.

from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

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
    ``strict`` runs ``_check_strict`` (nodes in props, actions anywhere, URLs)
    instead of the children-only walk; ``url_hosts`` are the https hosts its
    URLs may name."""

    name: str
    widget_types: frozenset[str]
    actions: frozenset[str]
    max_nodes: int
    max_depth: int
    max_chars: int
    manifest: dict[str, Any]
    detailed: frozenset[str] | None = None
    strict: bool = False
    url_hosts: frozenset[str] = frozenset()


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
# A ripple card's actions: paw-bar's, plus the client-side flow ones (no network,
# no navigation). Every step inside a flow or branch is held to the same set.
RIPPLE_ACTIONS: frozenset[str] = SPEC_ACTIONS | {"flow", "branch", "validate", "toast"}
RIPPLE_PROFILE = CardProfile(
    name="ripple",
    widget_types=frozenset(w["type"] for w in RIPPLE_MANIFEST["widgets"]) - RIPPLE_DEFERRED,
    actions=RIPPLE_ACTIONS,
    max_nodes=400,
    max_depth=16,
    max_chars=64_000,
    manifest=RIPPLE_MANIFEST,
    # The widgets the authoring rules lean on (the inputs and layouts, and the
    # ready-made data widgets), listed with their props.
    detailed=frozenset(
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
            "alert",
            "callout",
            "analytics-dashboard",
            "comparison-layout",
        }
    ),
    strict=True,
)

# Every widget name and action name the Ripple manifest knows (deferred ones too).
_RIPPLE_TYPES: frozenset[str] = frozenset(w["type"] for w in RIPPLE_MANIFEST["widgets"])
_RIPPLE_ACTIONS: frozenset[str] = frozenset(RIPPLE_MANIFEST["actions"])
# Props whose value is itself a node (``string | UISpec``), by widget.
_NODE_PROPS: dict[str, frozenset[str]] = {
    w["type"]: frozenset(
        name
        for name, spec in (w.get("props") or {}).items()
        if "UISpec" in str(spec.get("type", "")) and not str(spec["type"]).startswith("Array")
    )
    for w in RIPPLE_MANIFEST["widgets"]
}
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


def _check_actions(value: Any, events: list[str], allowed: frozenset[str] = SPEC_ACTIONS) -> None:
    for action in value if isinstance(value, list) else [value]:
        if not isinstance(action, dict) or action.get("action") not in allowed:
            raise _Reject("an event runs an action the bar does not honour")
        if action["action"] == "emit" and action.get("target") not in events:
            raise _Reject("an emit names a host event this widget does not declare")


def _check_events(
    node: dict[str, Any], events: list[str], allowed: frozenset[str] = SPEC_ACTIONS
) -> None:
    props = node.get("props")
    for holder in (node, props if isinstance(props, dict) else {}):
        for key, value in holder.items():
            if isinstance(key, str) and key.startswith("on_"):
                _check_actions(value, events, allowed)


def _is_handler_key(key: Any) -> bool:
    return isinstance(key, str) and (
        key.startswith("on_") or key == "actions" or key.endswith("Actions")
    )


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
    """The ``strict`` profile's walk over ``ui`` and ``state`` (see the header).
    Iterative, so no card can exhaust the stack; past ``_MAX_SCAN_LEVELS`` of
    nesting the card is refused."""
    allowed, hosts = profile.actions, profile.url_hosts
    nodes = 0
    # (value, json level, node depth, key it sits under, under a handler, is a
    # node, is a data row whose "action" is its own, inside a style)
    stack: list[tuple[Any, int, int, str, bool, bool, bool, bool]] = [
        (spec["ui"], 1, 1, "", False, True, False, False),
        (spec.get("state"), 1, 0, "", False, False, False, False),
    ]
    while stack:
        value, level, depth, key, handler, is_node, data_row, css = stack.pop()
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
            stack.extend((v, level + 1, depth, key, handler, False, data_row, css) for v in value)
            continue
        if not isinstance(value, dict):
            continue
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
            _check_events(value, events, allowed)
            props = value.get("props")
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
                        (kid, level + 2, depth + 1, k, False, True, False, False) for kid in v
                    )
                elif k == "props" and isinstance(v, dict):
                    node_props = _NODE_PROPS.get(kind, frozenset())
                    for pk, pv in v.items():
                        if (kind, pk) in _MARKDOWN_PROPS and isinstance(pv, str):
                            _check_plain_expressions(pv)
                        sub_node = pk in node_props and isinstance(pv, dict)
                        stack.append(
                            (
                                pv,
                                level + 2,
                                depth + 1 if sub_node else depth,
                                pk,
                                _is_handler_key(pk),
                                sub_node,
                                (kind, pk) in _DATA_ACTION_ROWS,
                                pk == "style",
                            )
                        )
                elif k != "type":
                    stack.append(
                        (v, level + 1, depth, k, _is_handler_key(k), False, False, k == "style")
                    )
            continue
        named = value.get("action")
        is_action = (handler and "action" in value) or (
            isinstance(named, str) and named in _RIPPLE_ACTIONS and not data_row and not css
        )
        if is_action:
            _check_actions(value, events, allowed)
            if named in _TOAST_ACTIONS and isinstance(value.get("message"), str):
                _check_plain_expressions(value["message"])
        stack.extend(
            (
                v,
                level + 1,
                depth,
                k,
                handler or _is_handler_key(k) or (is_action and k in _STEP_KEYS),
                False,
                False,
                css or k == "style",
            )
            for k, v in value.items()
        )


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
    ``node`` is a product-card with nothing left to show."""
    out = dict(node)
    if out["type"] == _PRODUCT_CARD:
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


def validate_and_hydrate(
    spec: dict,
    catalog: Iterable[Any] | None,
    *,
    verbs: Iterable[str] | None = HOST_EVENTS,
    lead_capture: bool = False,
    profile: CardProfile = PAWBAR_PROFILE,
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
    ``profile`` (the site's ``CardProfile``) gives the widget set and bounds."""
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


# A body too deeply nested to parse here. Never passed through as a legacy
# card: the client's parser might read it, and nothing here checked it.
_TOO_DEEP = object()


def _parse(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None
    except RecursionError:
        return _TOO_DEEP


def _is_spec(raw: Any) -> bool:
    return isinstance(raw, dict) and "ui" in raw


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
) -> str | None:
    """The complete ```pawbar-card fence to emit for a fence ``body``, or None to
    drop it. A Ripple spec is validated (against ``profile``) and hydrated; a
    legacy product card is repriced from the catalog; a legacy form card is held
    to the form rules; any other legacy card passes through verbatim. Never
    raises: a card no check can read is dropped and logged by error type only."""
    try:
        return _render_card(body, catalog, verbs, lead_capture, profile)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        _log_dropped(exc)
        return None


def _render_card(
    body: str,
    catalog: Iterable[Any] | None,
    verbs: Iterable[str] | None,
    lead_capture: bool,
    profile: CardProfile,
) -> str | None:
    raw = _parse(body)
    if raw is _TOO_DEEP:
        return None
    if _is_spec(raw):
        # Measured as paw-bar measures it: CRLF folded, trailing whitespace trimmed.
        if len(body.replace("\r\n", "\n").rstrip()) > profile.max_chars:
            return None
        spec = validate_and_hydrate(
            raw, catalog, verbs=verbs, lead_capture=lead_capture, profile=profile
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


def has_lead_form(body: str) -> bool:
    """Whether a card body holds a send_to_team form, as a Ripple spec node or a
    legacy ``{"kind": "form"}`` card. Says nothing about whether it is valid:
    ask it of a body ``render_card`` passed."""
    raw = _parse(body)
    if isinstance(raw, dict) and raw.get("kind") == "form":
        return raw.get("verb") == LEAD_VERB
    if not _is_spec(raw):
        return False
    stack = [raw["ui"]]
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


def _widget_line(widget: dict[str, Any]) -> str:
    props = [
        name + ("" if spec.get("required") else "?")
        for name, spec in (widget.get("props") or {}).items()
    ]
    props += list((widget.get("events") or {}).keys())
    props += [
        name + ("" if spec.get("required") else "?")
        for name, spec in (widget.get("nodeFields") or {}).items()
    ]
    return f"- {widget['type']} {{{', '.join(props)}}}: {widget.get('description', '')}"


# A summary line's description: its first sentence, at most this long.
_BRIEF_CHARS = 90


def _brief_line(widget: dict[str, Any]) -> str:
    text = " ".join(str(widget.get("description", "")).split())
    first = text.split(". ", 1)[0]
    if len(first) > _BRIEF_CHARS:
        first = first[: _BRIEF_CHARS - 1].rstrip() + "…"
    return f"- {widget['type']}: {first}"


def compact_manifest(profile: CardProfile = PAWBAR_PROFILE) -> str:
    """One line per widget: its type, its props (``?`` = optional), events and
    node fields, and what it is for; a widget outside ``profile.detailed`` gets
    only its type and the first sentence of what it is for."""
    return "\n".join(
        _widget_line(w)
        if profile.detailed is None or w["type"] in profile.detailed
        else _brief_line(w)
        for w in profile.manifest["widgets"]
        if w["type"] in profile.widget_types
    )


__all__ = [
    "PAWBAR_PROFILE",
    "RIPPLE_DEFERRED",
    "RIPPLE_MANIFEST_PATH",
    "RIPPLE_PROFILE",
    "CardProfile",
    "DEFERRED_WIDGETS",
    "FORM_PREFILL_MAX",
    "HOST_EVENTS",
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
    "validate_and_hydrate",
]
