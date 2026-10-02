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
#     pass through untouched. ``card_ids`` says which catalog items to fetch.
#
# pawbar-manifest.json is vendored byte-for-byte from paw-bar's
# app/pawbar-manifest.json. The drift test in
# tests/cloud/test_paw_bar_concierge_v2_output.py pins its hash and says how to
# refresh it. The shared parity fixtures live in tests/fixtures/card_parity/.

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

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


def _check_actions(value: Any, events: list[str]) -> None:
    for action in value if isinstance(value, list) else [value]:
        if not isinstance(action, dict) or action.get("action") not in SPEC_ACTIONS:
            raise _Reject("an event runs an action the bar does not honour")
        if action["action"] == "emit" and action.get("target") not in events:
            raise _Reject("an emit names a host event this widget does not declare")


def _check_events(node: dict[str, Any], events: list[str]) -> None:
    props = node.get("props")
    for holder in (node, props if isinstance(props, dict) else {}):
        for key, value in holder.items():
            if isinstance(key, str) and key.startswith("on_"):
                _check_actions(value, events)


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


def _check_tree(root: Any, events: list[str], lead_capture: bool = False) -> None:
    """paw-bar's checkTree, plus the widget set, the event rules and the form
    rules. ``events`` are the host events this widget declares (a subset of
    ``HOST_EVENTS``); ``lead_capture`` allows the lead card."""
    count = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal count
        if not isinstance(node, dict):
            raise _Reject("a node is not an object")
        if not isinstance(node.get("type"), str):
            raise _Reject("a node has no type")
        count += 1
        if count > MAX_SPEC_NODES:
            raise _Reject(f"more than {MAX_SPEC_NODES} nodes")
        if depth > MAX_SPEC_DEPTH:
            raise _Reject(f"nested deeper than {MAX_SPEC_DEPTH}")
        if node["type"] not in WIDGET_TYPES:
            raise _Reject(f"unknown widget type {node['type']!r}")
        _check_events(node, events)
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
) -> dict | None:
    """The card to send, or None to drop it.

    ``spec`` is the parsed fence JSON (``{"ui": ..., "state"?: ...}``); ``catalog``
    the widget's catalog (``PawBarCatalogItem`` models or dicts); ``verbs`` the
    widget's declared action verbs, of which only add_to_cart / checkout become a
    product's buttons (the action endpoint refuses an undeclared verb). The result
    holds only ``ui`` and ``state``: a spec's ``theme`` is dropped, as paw-bar
    drops it. It is re-checked after hydration, since filling ids in makes it
    longer and paw-bar measures what it receives. ``lead_capture`` (the site's
    ``concierge_lead_capture``) allows a ``send_to_team`` form; off by default."""
    try:
        if not isinstance(spec, dict) or "ui" not in spec:
            raise _Reject("not a spec")
        if len(_serialize(spec)) > MAX_SPEC_CHARS:
            raise _Reject(f"longer than {MAX_SPEC_CHARS} characters")
        events = _card_verbs(verbs)
        _check_tree(spec["ui"], events, lead_capture)
        state = spec.get("state")
        if state is not None and not isinstance(state, dict):
            raise _Reject("state is not an object")
        ui = _hydrate(spec["ui"], _catalog_index(catalog), events)
        if ui is None:
            return None
        out: dict[str, Any] = {"ui": ui}
        if state is not None:
            out["state"] = state
        body = _serialize(out)
        if len(body) > MAX_SPEC_CHARS or _FENCE in body:
            return None
        return out
    except _Reject:
        return None


# --------------------------------------------------------------------------- #
# Fence bodies
# --------------------------------------------------------------------------- #


def _parse(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


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
) -> str | None:
    """The complete ```pawbar-card fence to emit for a fence ``body``, or None to
    drop it. A Ripple spec is validated and hydrated; a legacy product card is
    repriced from the catalog; a legacy form card is held to the form rules; any
    other legacy card passes through verbatim."""
    raw = _parse(body)
    if _is_spec(raw):
        # Measured as paw-bar measures it: CRLF folded, trailing whitespace trimmed.
        if len(body.replace("\r\n", "\n").rstrip()) > MAX_SPEC_CHARS:
            return None
        spec = validate_and_hydrate(raw, catalog, verbs=verbs, lead_capture=lead_capture)
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


def card_ids(body: str) -> list[str]:
    """The catalog ids a fence body names: a Ripple spec's product-card ``ids``,
    or a legacy product card's item ids. What a lookup must fetch before
    ``render_card`` can hydrate it; at most ``MAX_CARD_IDS``, first seen first."""
    raw = _parse(body)
    out: dict[str, None] = {}
    if _is_spec(raw):
        _ids_in(raw["ui"], out, [MAX_SPEC_NODES])
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
    return f"- {widget['type']} {{{', '.join(props)}}}: {widget.get('description', '')}"


def compact_manifest() -> str:
    """One line per widget: its type, its props (``?`` = optional) and events,
    and what it is for."""
    return "\n".join(_widget_line(w) for w in MANIFEST["widgets"] if w["type"] in WIDGET_TYPES)


__all__ = [
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
    "render_card",
    "validate_and_hydrate",
]
