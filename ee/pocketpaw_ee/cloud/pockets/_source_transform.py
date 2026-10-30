# _source_transform.py — pure, declarative shaping of a raw source result.
# Created: 2026-06-12 (feat/connector-as-pocket-backend) — a pocket source
#   binding may carry a small `transform` spec that maps the raw result of a
#   fetch (http GET / sense / connector action) into the shape the widget's
#   state wants BEFORE it is bound. This module is the ONLY place that
#   interprets that spec. It is PURE: no network, no eval, no arbitrary code,
#   no I/O — given the same (raw, spec) it always returns the same value, so
#   it is exhaustively unit-testable and can never widen the source path's
#   blast radius.
#
# v1 spec (intentionally tiny — reject anything not listed):
#   { "select": "dotted.path" }       drill into the raw response to the
#                                     array/value to bind (e.g. data.applications).
#   { "map": [ {field def}, ... ] }   for a LIST input, one output row per input
#                                     row. Each field def is exactly one of:
#                                       {"to": "...", "from": "dotted.path"}      copy by path
#                                       {"to": "...", "from": "p", "values": {...},
#                                        "default": x}                            lookup-map a value
#                                       {"to": "...", "const": <json>}            constant
#   select + map compose: select drills first, map shapes the drilled list.
#   Both optional. No transform (None / {}) -> the raw value, unchanged
#   (today's behavior). An unknown/missing `from` resolves to None — it never
#   raises. A `transform` that contains an unrecognized key, or a malformed
#   field def, is rejected up front with `TransformError` so a hallucinated
#   spec fails loud at apply-time instead of silently mangling data.

from __future__ import annotations

from typing import Any

# The only top-level keys a v1 transform may carry.
_ALLOWED_KEYS = frozenset({"select", "map"})
# The only keys a single map-field def may carry.
_ALLOWED_FIELD_KEYS = frozenset({"to", "from", "values", "default", "const"})


class TransformError(ValueError):
    """A transform spec is not valid per the v1 grammar.

    Raised at apply-time (not at fetch-time) so the caller maps it onto the
    same per-source error envelope every other source failure uses — one bad
    transform never aborts a sibling source.
    """


def _resolve_path(value: Any, dotted: str) -> Any:
    """Walk a dotted path into nested dicts (and list-index segments).

    ``a.b.c`` reads ``value["a"]["b"]["c"]``. A numeric segment indexes a
    list (``items.0.name``). A missing key, a non-subscriptable value, or an
    out-of-range index all resolve to ``None`` — this never raises, so an
    unknown/missing ``from`` is null rather than a crash.
    """
    if not dotted:
        return value
    cur = value
    for seg in dotted.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)):
            # A numeric segment indexes a sequence; anything else misses.
            if seg.lstrip("-").isdigit():
                idx = int(seg)
                cur = cur[idx] if -len(cur) <= idx < len(cur) else None
            else:
                return None
        else:
            return None
    return cur


def _validate_field_def(field: Any) -> None:
    """Reject a map-field def that is not exactly one of the three shapes."""
    if not isinstance(field, dict):
        raise TransformError("each map field must be an object")
    extra = set(field) - _ALLOWED_FIELD_KEYS
    if extra:
        raise TransformError(f"map field has unknown keys: {sorted(extra)}")
    if not isinstance(field.get("to"), str) or not field["to"]:
        raise TransformError("each map field needs a non-empty string 'to'")
    has_const = "const" in field
    has_from = "from" in field
    if has_const and has_from:
        raise TransformError("a map field is 'const' OR 'from', not both")
    if not has_const and not has_from:
        raise TransformError("a map field needs either 'from' or 'const'")
    if has_const and ("values" in field or "default" in field):
        raise TransformError("'values'/'default' only apply to a 'from' field")
    if has_from and not isinstance(field["from"], str):
        raise TransformError("'from' must be a dotted-path string")
    if "values" in field and not isinstance(field["values"], dict):
        raise TransformError("'values' must be an object")


def _apply_field(field: dict, row: Any) -> Any:
    """Produce one output value for one map-field def against one input row."""
    if "const" in field:
        return field["const"]
    raw = _resolve_path(row, field["from"])
    if "values" in field:
        # Lookup-map: only str/int/bool keys can match a JSON object's keys.
        table: dict = field["values"]
        if isinstance(raw, (str, int, bool)) and raw in table:
            return table[raw]
        return field.get("default")  # None when no default declared
    return raw


def _validate_spec(spec: dict) -> None:
    """Reject anything outside the v1 grammar before any data is touched."""
    extra = set(spec) - _ALLOWED_KEYS
    if extra:
        raise TransformError(f"transform has unknown keys: {sorted(extra)}")
    if "select" in spec and not isinstance(spec["select"], str):
        raise TransformError("'select' must be a dotted-path string")
    if "map" in spec:
        if not isinstance(spec["map"], list):
            raise TransformError("'map' must be a list of field definitions")
        for field in spec["map"]:
            _validate_field_def(field)


def apply_transform(raw: Any, spec: dict | None) -> Any:
    """Shape ``raw`` per the transform ``spec``. Pure; never touches the network.

    ``None`` (or an empty ``{}``) returns ``raw`` unchanged — the default
    no-transform behavior every source had before this feature. Otherwise:

    1. ``select`` drills into ``raw`` along a dotted path (missing -> ``None``).
    2. ``map`` reshapes a LIST into one output row per input row. If the
       (post-select) value is not a list, ``map`` yields an empty list — a
       map over a non-list is a no-op, not a crash.

    Raises ``TransformError`` for any spec outside the v1 grammar so a
    malformed/hallucinated transform fails loud at the per-source boundary.
    """
    if not spec:
        return raw
    if not isinstance(spec, dict):
        raise TransformError("transform must be an object")
    _validate_spec(spec)

    value = _resolve_path(raw, spec["select"]) if "select" in spec else raw

    if "map" in spec:
        if not isinstance(value, list):
            return []
        fields: list[dict] = spec["map"]
        return [{f["to"]: _apply_field(f, row) for f in fields} for row in value]

    return value


__all__ = ["apply_transform", "TransformError"]
