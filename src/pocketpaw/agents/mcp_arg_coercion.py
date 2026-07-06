"""Coerce object/array tool arguments that arrived as JSON *strings*.

Why this exists: MCP-capable models sometimes serialize an object- or
array-typed tool argument as a JSON string — e.g. they send
``hints='{"name":"x"}'`` or ``ops='[{"op":"set_state",...}]'`` instead of the
real nested structure. The in-process SDK MCP handlers then either reject the
string with a type error or silently drop it, and the model burns a round-trip
retrying (narrating "the tool needs an actual object, not a stringified JSON").
Decoding the string at the handler boundary makes the FIRST call succeed.

This generalizes the per-tool ``_coerce_json_arg`` that ``sdk_mcp_widgets.py``
grew for ``start_flow`` (2026-06-15, the #1 Chain Flow v2 flakiness cause) into
one helper every in-process MCP handler can share.

Changes:
- 2026-07-03 (fix/mcp-tool-json-string-args): initial version — a shared
  ``coerce_json_object_args`` for the object/array params across the OSS +
  EE in-process MCP servers.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["coerce_json_object_args"]


def coerce_json_object_args(args: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """Return a shallow copy of *args* with the named *keys* decoded from a
    JSON string back into the object/array they encode.

    Only the keys in *keys* are touched, and only when their value is a string
    that (a) looks like a JSON object/array literal (starts with ``{`` or ``[``)
    and (b) parses to a ``dict`` or ``list``. A value that is already a
    structure, a string that is not valid JSON, or one that parses to a scalar,
    is left exactly as-is — so the handler's own type check still surfaces its
    normal, precise error for genuinely malformed input. The input mapping is
    never mutated.

    Args:
        args: the raw ``args`` dict an SDK MCP tool handler receives.
        keys: the parameter names whose schema type is ``object`` or ``array``.

    Returns:
        A new dict safe to read the object/array params from.
    """
    out = dict(args)
    for key in keys:
        val = out.get(key)
        if not isinstance(val, str):
            continue
        stripped = val.strip()
        if not stripped or stripped[0] not in "{[":
            continue
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, (dict, list)):
            out[key] = parsed
    return out
