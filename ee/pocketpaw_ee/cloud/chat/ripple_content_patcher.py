"""Persist Ripple inline-state patches onto the message that holds the spec.

When a user drags a kanban card, toggles a checkbox, or finishes a flow inside
a Ripple widget in a chat message, the frontend PATCHes the new state to the
backend. A spec lives in one of two places, and ``spec_id`` says which:

* ``spec_<n>`` - the n-th fenced ``ui-spec`` block inside ``message.content``.
  ``patch_content_with_state`` splices the new values into that JSON, so
  ``message.content`` stays the single source of truth for both the renderer
  and the agent's memory.
* ``att_<n>`` - the n-th attachment of ``type == "ripple"`` (other attachment
  types are not counted). Agent replies land here because the run pipeline
  strips the fence out of the content and stores the spec as the attachment's
  ``meta``. ``patch_attachments_with_state`` shallow-merges the patch into
  ``meta["state"]`` and leaves the rest of ``meta`` alone.

For content fences, the state keys (``_auto_kanban_0``, ``_auto_checkbox_1``,
...) are synthesized client-side by ``autoBindStatefulWidgets`` in
``paw-enterprise/.../ripple-spec-fix.ts``. To map a key back to the correct
node, we mirror that traversal here: depth-first, pre-order, counting
per-type indices for stateful widgets that don't already declare ``bind``.

Both functions are pure and return ``None`` when the target can't be found;
the caller turns that into a 404.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

# Must mirror the set in
# ``paw-enterprise/src/lib/components/chat/ripple-spec-fix.ts``. Keep in sync
# by hand — there's no shared schema.
STATEFUL_WIDGETS = frozenset(
    {
        "kanban",
        "checkbox",
        "switch",
        "input",
        "textarea",
        "select",
        "slider",
        "radio-group",
    }
)

# Match the same fence shape MarkdownRenderer's segment scanner uses:
# ``\n``-delimited fences with the language tag ``ui-spec``.
_FENCE_RE = re.compile(r"```ui-spec\n(.+?)\n```", re.DOTALL)


def _walk_collect_targets(node: Any, counters: dict[str, int], targets: dict[str, dict]) -> None:
    """Depth-first pre-order walk that mirrors ``autoBindStatefulWidgets``.

    For each stateful widget without an explicit ``bind``, assign the next
    deterministic auto-key (``_auto_<type>_<index>``) and record the node so
    the caller can update its seed ``value`` in place.
    """
    if not isinstance(node, dict):
        return
    type_ = node.get("type")
    if isinstance(type_, str) and type_ in STATEFUL_WIDGETS and not node.get("bind"):
        idx = counters.get(type_, 0)
        counters[type_] = idx + 1
        key = f"_auto_{type_}_{idx}"
        targets[key] = node
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            _walk_collect_targets(child, counters, targets)


def _set_widget_value(node: dict, value: Any) -> None:
    """Write ``value`` onto the node where its renderer will read it.

    Mirrors the agent-shape coverage in
    ``MarkdownRenderer.normalizeNode``: flat ``value`` at root and nested
    ``props.value`` are both valid agent emissions. We update wherever the
    field already exists; if neither does, default to ``props.value`` (the
    canonical Ripple shape after normalization).
    """
    if "value" in node:
        node["value"] = value
    elif isinstance(node.get("props"), dict) and "value" in node["props"]:
        node["props"]["value"] = value
    else:
        if not isinstance(node.get("props"), dict):
            node["props"] = {}
        node["props"]["value"] = value


def _resolve_root(spec: dict) -> dict | None:
    """Find the UI tree's root node inside a parsed spec.

    Tolerates both UISpec (``{ui: <node>}``) and a bare ``{type, ...}``
    root, which is what most agents emit for chat-inline blocks.
    """
    if not isinstance(spec, dict):
        return None
    ui = spec.get("ui")
    if isinstance(ui, dict) and "type" in ui:
        return ui
    if "type" in spec:
        return spec
    return None


def patch_content_with_state(content: str, spec_id: str, patch_state: dict[str, Any]) -> str | None:
    """Return ``content`` with the ``spec_id``-th ``ui-spec`` block updated.

    ``spec_id`` is the position-based key the frontend uses (``spec_0``,
    ``spec_1``, ...). The Nth fence in document order is the target.

    For each ``_auto_<type>_<idx>`` key in ``patch_state``, we walk the
    fence's tree to find the matching node and write the new value. We also
    set the spec's top-level ``state`` map for redundancy so frontend renders
    that don't re-walk for any reason still see the latest values.

    Returns the new content string, or ``None`` if the fence couldn't be
    located or its JSON is malformed (caller treats this as a no-op write).
    """
    if not spec_id.startswith("spec_"):
        return None
    try:
        target_index = int(spec_id[len("spec_") :])
    except ValueError:
        return None

    matches = list(_FENCE_RE.finditer(content))
    if target_index < 0 or target_index >= len(matches):
        return None
    match = matches[target_index]

    try:
        spec = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, dict):
        return None

    root = _resolve_root(spec)
    if root is not None:
        targets: dict[str, dict] = {}
        _walk_collect_targets(root, {}, targets)
        for auto_key, value in patch_state.items():
            node = targets.get(auto_key)
            if node is not None:
                _set_widget_value(node, value)

    # Persist the full state map at the spec root too — covers agent emissions
    # that already declared ``bind`` (so the walker skipped them) and any
    # future state keys that don't map onto a single node's seed prop.
    existing_state = spec.get("state")
    spec["state"] = {
        **(existing_state if isinstance(existing_state, dict) else {}),
        **patch_state,
    }

    new_block = "```ui-spec\n" + json.dumps(spec, ensure_ascii=False) + "\n```"
    return content[: match.start()] + new_block + content[match.end() :]


def patch_attachments_with_state(
    attachments: Sequence[Any], spec_id: str, patch_state: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Return a new attachments list with the ``spec_id`` ripple spec's state merged.

    ``spec_id`` is ``att_<n>``: the n-th (0-based) attachment whose ``type`` is
    ``"ripple"``. The target's ``meta["state"]`` becomes
    ``{**existing_state, **patch_state}``; every other key is kept as-is.

    Accepts plain dicts or pydantic models (anything with ``model_dump``) and
    always returns plain dicts, deep enough copies that the input is never
    mutated. Returns ``None`` for a malformed id, an out-of-range index, or a
    target whose ``meta`` isn't a dict.
    """
    if not spec_id.startswith("att_"):
        return None
    try:
        target_index = int(spec_id[len("att_") :])
    except ValueError:
        return None
    if target_index < 0:
        return None

    out: list[dict[str, Any]] = [
        a.model_dump() if hasattr(a, "model_dump") else dict(a) for a in attachments
    ]
    ripple_seen = 0
    for i, att in enumerate(out):
        if att.get("type") != "ripple":
            continue
        if ripple_seen == target_index:
            meta = att.get("meta")
            if not isinstance(meta, dict):
                return None
            existing_state = meta.get("state")
            out[i] = {
                **att,
                "meta": {
                    **meta,
                    "state": {
                        **(existing_state if isinstance(existing_state, dict) else {}),
                        **patch_state,
                    },
                },
            }
            return out
        ripple_seen += 1
    return None


__all__ = ["patch_attachments_with_state", "patch_content_with_state", "STATEFUL_WIDGETS"]
