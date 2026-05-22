"""Minimal ripple spec normalizer — ensures envelope fields and widget IDs.

Changes: 2026-05-21 (#1172) — ``normalize_ripple_spec`` now stamps a
stable ``n_xxxxxxxx`` id on every node of a UISpec ``ui`` tree (and
each ``panes`` value) via ``spec_ops.ensure_ids``. Every persist path
routes through this normalizer, so stored specs always carry node ids
and the chat agent can address nodes with granular edit ops. Previously
ids were minted only at the start of a granular mutation op, leaving a
freshly created pocket's tree id-less and unaddressable.

Changes: 2026-05-22 (PR #1177, RFC 04 alpha) — ``normalize_ripple_spec``
now runs ``_lift_rest_sources`` before everything else. The
pocket-authoring agent reliably emits a hallucinated data-source shape:
a ``rippleSpec.tool_specs`` REST list (with invented ``kind`` / ``url``
/ ``auto_fetch`` / ``into`` fields) instead of the RFC 04
``rippleSpec.sources`` block, and refresh buttons wired with a ``source_id``
field instead of ``source``. The runtime reads only ``sources`` and only
``handler.source``, so that output is inert. Prompt guidance has not
beaten the model's prior, so we translate the output deterministically:
lift the REST entries into ``sources`` and repair the button handlers.
"""

from __future__ import annotations

import logging
import secrets
import urllib.parse
from typing import Any

log = logging.getLogger(__name__)


# Event-handler slots a node may carry. A run_source button uses one of
# these (``on_click`` in practice); the repair walk checks all of them.
_HANDLER_SLOTS = (
    "on_click",
    "on_change",
    "on_input",
    "on_submit",
    "on_focus",
    "on_blur",
)


def _relative_path(raw: str) -> str:
    """Return ``raw`` as a relative path. If ``raw`` is an absolute URL,
    keep only its path + query portion (the source executor joins it onto
    the pocket's configured backend base URL). A path that is already
    relative is returned untouched.
    """
    split = urllib.parse.urlsplit(raw)
    if split.scheme or split.netloc:
        rebuilt = split.path or "/"
        if split.query:
            rebuilt = f"{rebuilt}?{split.query}"
        return rebuilt
    return raw


def _looks_like_rest_source(entry: dict[str, Any]) -> bool:
    """Heuristic: is this ``tool_specs`` entry a REST data source?

    True when it declares ``kind == "rest"``, or when it carries both a
    ``url``/``path`` and an ``into``/``bind`` — the minimum a data binding
    needs. LLM-tool specs (the legitimate ``tool_specs`` shape) carry none
    of these and are left alone.
    """
    if entry.get("kind") == "rest":
        return True
    has_endpoint = bool(entry.get("url")) or bool(entry.get("path"))
    has_target = bool(entry.get("into")) or bool(entry.get("bind"))
    return has_endpoint and has_target


def _lifted_source_entry(entry: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Translate one hallucinated REST ``tool_specs`` entry into a canonical
    ``rippleSpec.sources`` entry. Returns ``None`` when the entry cannot be
    represented as an alpha source (non-GET method — alpha is GET-only).
    """
    method = str(entry.get("method") or "GET").upper()
    if method != "GET":
        log.warning(
            "ripple_normalizer: skipped tool_specs entry %r — method %s "
            "is not GET (RFC 04 alpha is read-only)",
            key,
            method,
        )
        return None

    raw_path = entry.get("path") or entry.get("url")
    if not isinstance(raw_path, str) or not raw_path:
        log.warning("ripple_normalizer: skipped tool_specs entry %r — no path/url", key)
        return None

    into = entry.get("into")
    bind = entry.get("bind")
    if isinstance(bind, str) and bind:
        resolved_bind = bind
    elif isinstance(into, str) and into:
        resolved_bind = f"state.{into}"
    else:
        resolved_bind = f"state.{key}"

    refresh = entry.get("refresh")
    if isinstance(refresh, list) and refresh:
        resolved_refresh = refresh
    elif entry.get("auto_fetch"):
        resolved_refresh = ["pocket_open", "manual"]
    else:
        resolved_refresh = ["manual"]

    return {
        "method": "GET",
        "path": _relative_path(raw_path),
        "bind": resolved_bind,
        "refresh": resolved_refresh,
    }


def _repair_run_source_handler(handler: Any, sources: dict[str, Any]) -> Any:
    """Repair a single action handler that targets ``run_source``.

    Fixes the agent's ``source_id`` field-name miss (the runtime reads
    ``handler.source``), and binds a sourceless handler to the lone source
    when there is exactly one. Non-``run_source`` handlers pass through.
    """
    if not isinstance(handler, dict) or handler.get("action") != "run_source":
        return handler
    repaired = dict(handler)
    if "source" not in repaired and "source_id" in repaired:
        repaired["source"] = repaired.pop("source_id")
    elif "source_id" in repaired:
        # `source` already present — drop the stray alias.
        repaired.pop("source_id")
    if not repaired.get("source") and len(sources) == 1:
        repaired["source"] = next(iter(sources))
    return repaired


def _repair_handlers_in_node(node: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    """Repair every ``run_source`` handler reachable from a node's event
    slots. A slot value can be a single handler object or a list of them.
    """
    repaired = node
    for slot in _HANDLER_SLOTS:
        if slot not in repaired:
            continue
        value = repaired[slot]
        if isinstance(value, list):
            new_value: Any = [_repair_run_source_handler(h, sources) for h in value]
        else:
            new_value = _repair_run_source_handler(value, sources)
        if new_value != value:
            repaired = {**repaired, slot: new_value}
    return repaired


def _walk_repair_handlers(node: Any, sources: dict[str, Any]) -> Any:
    """Recursively walk a UI structure (dict tree or list) repairing every
    ``run_source`` handler. Returns a new structure; input is not mutated.
    """
    if isinstance(node, dict):
        fixed = _repair_handlers_in_node(node, sources)
        for key in ("children", "else_children"):
            kids = fixed.get(key)
            if isinstance(kids, list):
                fixed = {**fixed, key: [_walk_repair_handlers(c, sources) for c in kids]}
        return fixed
    if isinstance(node, list):
        return [_walk_repair_handlers(c, sources) for c in node]
    return node


def _lift_rest_sources(spec: dict[str, Any]) -> dict[str, Any]:
    """Translate the authoring agent's hallucinated data-source output into
    the canonical RFC 04 shape.

    Two repairs, both pure (the input ``spec`` is not mutated):

    1. Lift REST entries out of a ``rippleSpec.tool_specs`` list into
       ``rippleSpec.sources``. ``tool_specs`` is NOT a real rippleSpec field
       (the real ``tool_specs`` is a top-level Pocket field for LLM tools);
       the agent invents it for data sources. Lifted entries are removed
       from the nested ``tool_specs`` list, and the key is dropped entirely
       once empty. A correctly-authored ``sources`` block is never clobbered
       — lifted entries merge in without overwriting existing keys.
    2. Repair ``run_source`` button handlers — rename the agent's
       ``source_id`` field to ``source``, and bind a sourceless handler to
       the lone source when there is exactly one.

    A spec with no nested ``tool_specs`` and no ``run_source`` handlers
    passes through structurally unchanged.
    """
    result = spec
    sources: dict[str, Any] = (
        dict(spec.get("sources") or {}) if isinstance(spec.get("sources"), dict) else {}
    )

    raw_tool_specs = spec.get("tool_specs")
    if isinstance(raw_tool_specs, list) and raw_tool_specs:
        remaining: list[Any] = []
        lifted_any = False
        generated = 0
        for entry in raw_tool_specs:
            if not isinstance(entry, dict) or not _looks_like_rest_source(entry):
                remaining.append(entry)
                continue
            key = entry.get("id") or entry.get("into")
            if not isinstance(key, str) or not key:
                generated += 1
                key = f"src_{generated}"
            lifted = _lifted_source_entry(entry, key)
            if lifted is None:
                # Non-GET / unrepresentable — drop it (not a real LLM tool
                # spec either, so it does not belong back in tool_specs).
                lifted_any = True
                continue
            lifted_any = True
            # Do not clobber a correctly-authored source under the same key.
            if key not in sources:
                sources[key] = lifted
        if lifted_any:
            new_spec = {k: v for k, v in result.items() if k != "tool_specs"}
            if remaining:
                new_spec["tool_specs"] = remaining
            if sources:
                new_spec["sources"] = sources
            result = new_spec

    # Repair run_source handlers across every UI surface.
    if isinstance(result.get("ui"), (dict, list)):
        result = {**result, "ui": _walk_repair_handlers(result["ui"], sources)}
    if isinstance(result.get("panes"), dict):
        result = {
            **result,
            "panes": {k: _walk_repair_handlers(v, sources) for k, v in result["panes"].items()},
        }
    return result


def _stamp_node_ids(ui: Any) -> Any:
    """Assign a stable ``n_xxxxxxxx`` id to every node in a UISpec
    tree that lacks one. Idempotent and collision-safe — nodes that
    already carry a valid unique id pass through untouched. Returns the
    same object (``ensure_ids`` mutates in place).

    ``spec_ops`` is imported lazily: ``pockets/__init__`` pulls in the
    pockets router, which imports ``pockets/service``, which imports
    this module — a top-level import here would close the cycle.
    """
    if isinstance(ui, dict):
        from pocketpaw_ee.cloud.pockets import spec_ops

        spec_ops.ensure_ids(ui)
    return ui


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


def _fix_entity_detail_actions(node: dict[str, Any]) -> dict[str, Any]:
    """Strip dead `entity-detail` action items — those with `id`/`label`
    but no `actions` (or `on_click`) handler.

    EntityDetail's `props.actions[]` renders as primary CTAs in the hero.
    An item with no wired handler renders a clickable button that does
    nothing — visually identical to a working button. The renderer falls
    back to a host `onaction` callback when a handler is missing, but
    pocket specs don't have one, so the click silently no-ops.

    We also lift `on_click` → `actions` for items that use the wrong
    field name (parallel to `bind` → `items` on `each`).

    Stripping is intentional over raising: a ValidationError on persist
    would lock the agent in a retry loop. The prompt teaches the agent
    the correct shape; this is the safety net.
    """
    if node.get("type") != "entity-detail":
        return node
    props = node.get("props")
    if not isinstance(props, dict):
        return node
    actions = props.get("actions")
    if not isinstance(actions, list) or not actions:
        return node

    cleaned: list[Any] = []
    changed = False
    for item in actions:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        # Lift `on_click` → `actions` when the agent uses the wrong field.
        if "actions" not in item and "on_click" in item:
            handler = item["on_click"]
            item = {k: v for k, v in item.items() if k != "on_click"}
            item["actions"] = handler
            changed = True
        handler = item.get("actions")
        has_handler = (isinstance(handler, list) and len(handler) > 0) or (
            isinstance(handler, dict) and handler
        )
        if has_handler:
            cleaned.append(item)
        else:
            changed = True
            log.warning(
                "ripple_normalizer: dropped entity-detail action %r — no handler wired",
                item.get("id") or item.get("label") or "<unnamed>",
            )

    if not changed:
        return node
    return {**node, "props": {**props, "actions": cleaned}}


def _walk_and_fix(node: Any) -> Any:
    """Recursively walk a UISpec tree, applying node-level fixes.
    Returns a new structure; input is not mutated."""
    if isinstance(node, dict):
        fixed = _fix_control_flow_node(node)
        fixed = _fix_entity_detail_actions(fixed)
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

    # Translate the authoring agent's hallucinated data-source output
    # (``rippleSpec.tool_specs`` REST list + ``source_id`` buttons) into the
    # canonical RFC 04 shape before any other normalization. Pure: returns
    # a new dict, leaves a correctly-authored spec structurally unchanged.
    spec = _lift_rest_sources(spec)

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

    # Multi-pane: walk every pane to fix control-flow nodes inside, then
    # stamp node ids so each pane tree is addressable by edit ops.
    if spec.get("panes") and isinstance(spec["panes"], dict):
        fixed_panes = {k: _stamp_node_ids(_walk_and_fix(v)) for k, v in spec["panes"].items()}
        return {**spec, **envelope, "version": spec.get("version", "1.0"), "panes": fixed_panes}

    # UISpec v1.0: walk the tree, then stamp node ids before persisting.
    ui = spec.get("ui")
    if isinstance(ui, dict) and ui.get("type"):
        return {
            **spec,
            **envelope,
            "version": spec.get("version", "1.0"),
            "ui": _stamp_node_ids(_walk_and_fix(ui)),
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
                "ui": _stamp_node_ids(_walk_and_fix(candidate)),
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
    if isinstance(spec_type, str) and spec_type and ("props" in spec or "children" in spec):
        node_keys = ("type", "props", "children", "style", "show", "id")
        node = {k: v for k, v in spec.items() if k in node_keys}
        return {
            **envelope,
            "version": spec.get("version", "1.0"),
            "ui": _stamp_node_ids(_walk_and_fix(node)),
        }

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
