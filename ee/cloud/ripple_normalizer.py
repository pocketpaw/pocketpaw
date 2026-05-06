"""Minimal ripple spec normalizer — ensures envelope fields and widget IDs."""

from __future__ import annotations

import secrets
from typing import Any


def _short_id() -> str:
    return secrets.token_hex(4)


def _fix_control_flow_node(node: dict[str, Any]) -> dict[str, Any]:
    """Fix node-level control-flow field misnames the agent commonly
    gets wrong on `each` and `if`.

    The agent has been heavily trained on `bind` for value-bound widgets
    (input, kanban, checkbox), and over-applies it to control-flow
    widgets that use node-level `items` (each) or `condition` (if).
    Without `items`, an `each` block renders zero iterations — the
    visible symptom is "header + composer but no list rows". Without
    `condition`, an `if` always renders its `else_children`.

    The frontend renderer doesn't tolerate these aliases, so we lift
    them on persist + read. Idempotent: nodes already in the canonical
    shape pass through unchanged.
    """
    ntype = node.get("type")
    if ntype == "each" and "items" not in node:
        # `bind: "todos"` → `items: "todos"` (renderer accepts bare paths
        # and `{state.foo}` templates equivalently — the resolver strips
        # the curlies before lookup).
        bind = node.get("bind")
        if isinstance(bind, str) and bind:
            node = {k: v for k, v in node.items() if k != "bind"}
            node["items"] = bind
    elif ntype == "if" and "condition" not in node:
        # Less common but symmetrical: agent sometimes uses `bind` or
        # `when` for an `if`'s gate.
        for alias in ("bind", "when", "if"):
            candidate = node.get(alias)
            if isinstance(candidate, str) and candidate and alias != "type":
                node = {k: v for k, v in node.items() if k != alias}
                node["condition"] = candidate
                break
    return node


def _walk_and_fix(node: Any) -> Any:
    """Recursively walk a UISpec tree, applying node-level fixes.
    Returns a new structure; input is not mutated."""
    if isinstance(node, dict):
        fixed = _fix_control_flow_node(node)
        if "children" in fixed and isinstance(fixed["children"], list):
            fixed = {**fixed, "children": [_walk_and_fix(c) for c in fixed["children"]]}
        if "else_children" in fixed and isinstance(fixed["else_children"], list):
            fixed = {**fixed, "else_children": [_walk_and_fix(c) for c in fixed["else_children"]]}
        return fixed
    if isinstance(node, list):
        return [_walk_and_fix(c) for c in node]
    return node


def normalize_ripple_spec(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize AI-generated rippleSpec before persistence.

    Ensures envelope fields (version, intent, lifecycle.id).
    Passes through UISpec and multi-pane specs with minimal changes.
    Generates widget IDs if missing for flat widget specs.
    """
    if not spec or not isinstance(spec, dict):
        return None

    name = spec.get("title") or spec.get("name")
    pocket_id = spec.get("id") or (spec.get("lifecycle") or {}).get("id") or f"pocket-{_short_id()}"
    meta = spec.get("metadata") or {}
    color = spec.get("color") or meta.get("color", "#0A84FF")

    envelope = {
        "lifecycle": spec.get("lifecycle") or {"type": "persistent", "id": pocket_id},
        "title": name or spec.get("title"),
        "name": name or spec.get("name"),
        "color": color,
        "metadata": {
            "category": spec.get("category") or meta.get("category", "custom"),
            "color": color,
            **meta,
        },
    }

    # Multi-pane: walk every pane to fix control-flow nodes inside.
    if spec.get("panes") and isinstance(spec["panes"], dict):
        fixed_panes = {k: _walk_and_fix(v) for k, v in spec["panes"].items()}
        return {**spec, **envelope, "version": spec.get("version", "1.0"), "panes": fixed_panes}

    # UISpec v1.0: walk the tree before persisting.
    ui = spec.get("ui")
    if isinstance(ui, dict) and ui.get("type"):
        return {
            **spec,
            **envelope,
            "version": spec.get("version", "1.0"),
            "ui": _walk_and_fix(ui),
        }

    # UISpec under a misnamed top-level key — the agent occasionally
    # invents `root` / `tree` / `view` / `body` / `content` for the UI
    # tree instead of `ui`. The spec is otherwise valid (state,
    # bindings, action handlers all in place); only the field name is
    # wrong. Detect a dict-with-`type` under any of these aliases and
    # lift it into `ui` so the renderer picks it up. Agent-side prompt
    # is the primary fix; this is the safety net.
    for alias in ("root", "tree", "view", "body", "content"):
        candidate = spec.get(alias)
        if isinstance(candidate, dict) and isinstance(candidate.get("type"), str):
            promoted = {k: v for k, v in spec.items() if k != alias}
            return {
                **promoted,
                **envelope,
                "version": spec.get("version", "1.0"),
                "ui": _walk_and_fix(candidate),
            }

    # UISpec passed as a raw root node — i.e. ``{type: "flex", props,
    # children, ...}`` instead of ``{ui: {type: "flex", ...}}``. The
    # ``create_pocket`` MCP tool description tells the agent to send a
    # "UISpec v1.0 component tree", which it often interprets as the
    # node itself (no ``ui`` wrapper). Detect that shape and lift the
    # node under ``ui`` so the frontend's UISpec renderer picks it up
    # — without this, the persisted spec has no ``ui`` and no
    # ``widgets``, and the dashboard renderer falls back to the
    # "No widgets yet" empty state.
    spec_type = spec.get("type")
    if isinstance(spec_type, str) and spec_type and (
        "props" in spec or "children" in spec
    ):
        node_keys = ("type", "props", "children", "style", "show", "id")
        node = {k: v for k, v in spec.items() if k in node_keys}
        return {**envelope, "version": spec.get("version", "1.0"), "ui": _walk_and_fix(node)}

    # Flat widgets: ensure IDs
    raw_widgets = spec.get("widgets")
    if isinstance(raw_widgets, list) and raw_widgets:
        widgets = []
        for i, w in enumerate(raw_widgets):
            if not isinstance(w, dict):
                continue
            w = {**w}
            if not w.get("id"):
                w["id"] = f"{pocket_id}-w{i}"
            if not w.get("title"):
                w["title"] = w.get("name", f"Widget {i + 1}")
            widgets.append(w)
        return {
            **spec,
            **envelope,
            "version": spec.get("version", "2.0"),
            "intent": spec.get("intent", "dashboard"),
            "widgets": widgets,
            "display": spec.get("display") or {"columns": 3},
            "dashboard_layout": spec.get("dashboard_layout")
            or {"type": "grid", "columns": 3, "gap": 10},
        }

    # No widgets, no ui, no panes — return as-is with envelope
    return {**spec, **envelope}
