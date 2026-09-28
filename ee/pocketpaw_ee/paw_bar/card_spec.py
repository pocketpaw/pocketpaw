# ee/pocketpaw_ee/paw_bar/card_spec.py — bound and hydrate the cards a v2 concierge writes.
#
# Created: 2026-09-28 (feat/concierge-v2-output, CR-2). A v2 reply can carry a
# generated UI as a ```pawbar-card fence whose JSON has a ``ui`` node tree (a
# Ripple spec). paw-bar draws it on a customer's page, so the server checks it
# before any of it leaves:
#
#   * the same bounds paw-bar's lib/spec-card.ts applies: 32,000 chars, 80 nodes,
#     depth 8 (root is 1), ``children`` / ``else_children`` lists, ``state`` an
#     object — so a card the server passes is never one the client refuses;
#   * two rules the client leaves to render time: every node ``type`` is a widget
#     in the vendored pawbar-manifest.json, and every event action is one the
#     manifest lists, with ``emit`` limited to the add_to_cart / checkout host
#     events;
#   * product data comes only from the site catalog: a ``product-card``'s ``ids``
#     become ``items`` (name, price, currency, image from the catalog), unknown ids
#     are dropped, an empty product-card is dropped. The model never supplies a
#     name, a price or an image. A legacy ``{"kind": "product"}`` card is repriced
#     the same way; other legacy cards pass through untouched.
#
# pawbar-manifest.json is vendored byte-for-byte from paw-bar's
# app/pawbar-manifest.json (qbtrix/paw-bar PR #26, branch feat/wire-spec-cards,
# commit f0c8c12, unmerged when vendored). The drift test in
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
# The only host events a card may emit; SpecCard.svelte ignores every other one.
HOST_EVENTS: tuple[str, ...] = ("add_to_cart", "checkout")

MANIFEST_PATH = Path(__file__).with_name("pawbar-manifest.json")
MANIFEST: dict[str, Any] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
WIDGET_TYPES: frozenset[str] = frozenset(w["type"] for w in MANIFEST["widgets"])
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
                "actions": list(verbs),
            }
        )
    return items


# --------------------------------------------------------------------------- #
# Ripple specs
# --------------------------------------------------------------------------- #


def _check_actions(value: Any) -> None:
    for action in value if isinstance(value, list) else [value]:
        if not isinstance(action, dict) or action.get("action") not in SPEC_ACTIONS:
            raise _Reject("an event runs an action the bar does not honour")
        if action["action"] == "emit" and action.get("target") not in HOST_EVENTS:
            raise _Reject("an emit names a host event other than add_to_cart / checkout")


def _check_events(node: dict[str, Any]) -> None:
    props = node.get("props")
    for holder in (node, props if isinstance(props, dict) else {}):
        for key, value in holder.items():
            if isinstance(key, str) and key.startswith("on_"):
                _check_actions(value)


def _check_tree(root: Any) -> None:
    """paw-bar's checkTree, plus the widget set and the event rules."""
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
        _check_events(node)
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
    spec: dict, catalog: Iterable[Any] | None, *, verbs: Iterable[str] | None = HOST_EVENTS
) -> dict | None:
    """The card to send, or None to drop it.

    ``spec`` is the parsed fence JSON (``{"ui": ..., "state"?: ...}``); ``catalog``
    the widget's catalog (``PawBarCatalogItem`` models or dicts); ``verbs`` the
    widget's declared action verbs, of which only add_to_cart / checkout become a
    product's buttons (the action endpoint refuses an undeclared verb). The result
    holds only ``ui`` and ``state``: a spec's ``theme`` is dropped, as paw-bar
    drops it. It is re-checked after hydration, since filling ids in makes it
    longer and paw-bar measures what it receives."""
    try:
        if not isinstance(spec, dict) or "ui" not in spec:
            raise _Reject("not a spec")
        if len(_serialize(spec)) > MAX_SPEC_CHARS:
            raise _Reject(f"longer than {MAX_SPEC_CHARS} characters")
        _check_tree(spec["ui"])
        state = spec.get("state")
        if state is not None and not isinstance(state, dict):
            raise _Reject("state is not an object")
        ui = _hydrate(spec["ui"], _catalog_index(catalog), _card_verbs(verbs))
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
    body: str, catalog: Iterable[Any] | None, *, verbs: Iterable[str] | None = HOST_EVENTS
) -> str | None:
    """The complete ```pawbar-card fence to emit for a fence ``body``, or None to
    drop it. A Ripple spec is validated and hydrated; a legacy product card is
    repriced from the catalog; any other legacy card passes through verbatim."""
    raw = _parse(body)
    if _is_spec(raw):
        # Measured as paw-bar measures it: CRLF folded, trailing whitespace trimmed.
        if len(body.replace("\r\n", "\n").rstrip()) > MAX_SPEC_CHARS:
            return None
        spec = validate_and_hydrate(raw, catalog, verbs=verbs)
        return None if spec is None else f"{_FENCE}pawbar-card\n{_serialize(spec)}\n{_FENCE}"
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if isinstance(raw, dict) and (not isinstance(kind, str) or kind in ("", "product")):
        # paw-bar reads a card with no kind as a product card.
        card = _legacy_product(raw, _catalog_index(catalog), _card_verbs(verbs))
        if card is None:
            return None
        text = _serialize(card)
        return None if _FENCE in text else f"{_FENCE}pawbar-card\n{text}\n{_FENCE}"
    return f"{_FENCE}pawbar-card\n{body}{_FENCE}"


def card_verdict(
    body: str, catalog: Iterable[Any] | None, *, verbs: Iterable[str] | None = HOST_EVENTS
) -> Literal["accept", "reject", "legacy"]:
    """The parity-fixture verdict for a fence body: ``legacy`` when it is not a
    Ripple spec, else ``accept`` when it would be emitted, ``reject`` when not."""
    if not _is_spec(_parse(body)):
        return "legacy"
    return "reject" if render_card(body, catalog, verbs=verbs) is None else "accept"


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
    return "\n".join(_widget_line(w) for w in MANIFEST["widgets"])


__all__ = [
    "HOST_EVENTS",
    "MANIFEST_PATH",
    "MAX_SPEC_CHARS",
    "MAX_SPEC_DEPTH",
    "MAX_SPEC_NODES",
    "SPEC_ACTIONS",
    "WIDGET_TYPES",
    "card_verdict",
    "compact_manifest",
    "render_card",
    "validate_and_hydrate",
]
