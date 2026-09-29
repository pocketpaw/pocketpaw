# ee/agent/mcp_servers/surfaces.py — in-process MCP server: let the chat agent
# OPEN an app surface on the user's screen.
#
# Created: 2026-09-29 (feat/open-surface-tool). Clones the timeline.py shape: SDK
# import guard, SERVER_NAME + *_TOOL_ID allowlist constants, _error_response /
# _success_response helpers, and a marker envelope run_core promotes to a
# dedicated chat server event.
#
# ONE tool, ``open_surface(route, params?, reason?)``. It has NO side effect: it
# validates and returns ``{"open_surface": {route, params?, reason?}}``, and
# run_core turns that envelope into an ``open_surface`` server event the
# frontend's ChatSession acts on (the browser does the opening). The wire shape
# is a FIXED contract with paw-enterprise — do not change it here alone.
#
# No ContextVar identity read, unlike belt / external_actions: nothing here is
# tenant-scoped or persisted, so there is no identity to bind.
#
# SCOPING. Ambient registration (``CloudSurfacesMcpProvider``), NOT in
# ``ALWAYS_ALLOWED_MCP_SERVERS`` and NOT in any surface allowlist. The GENERIC
# surface (what the /no-ui-lab sends) resolves to ``_DEFAULT_PROFILE``, whose
# ``allow_mcp_tool_ids`` is None — no MCP restriction — so the tool is reachable
# there with zero scoping code. ALWAYS_ALLOWED would push it onto every
# allowlisted surface (/sites, /studio, /belt, /ship, /browser, the public
# concierge is exclusive anyway), which is wider than the need. Known ceiling:
# allowlisted surfaces such as /studio/editor filter it out, so "pick another
# file" from inside the editor cannot open /files yet — add the id to that
# surface's allowlist when a surface actually needs it.
#
# TRUST. run_core promotes the envelope only from a tool_result whose resolved
# name is in ``OPEN_SURFACE_TOOL_NAMES`` (unresolved fails closed) — a file read
# or web fetch can carry a well-formed marker — and re-validates it through
# ``validate_open_surface``.
#
# EDITOR ``src``. /studio/editor fetches ``src`` into the timeline, so an
# external URL would put attacker-chosen media in front of the user. Only the
# backend's own media shapes are accepted, exactly the two URLs the agent is
# ever handed for a file: ``/api/v1/uploads/<file_id>`` (uploads/service.py,
# deliver.py) and ``/api/v1/media/<name>`` (cloud/media/storage.py). Both are
# single-segment, relative, auth-gated routes; the frontend mints/resolves them
# into the fetchable absolute URL its handoff wants (editor/handoff.ts says the
# caller owns that). Absolute http(s) URLs — including a presigned S3 URL the
# UI itself may mint — are rejected: the agent never needs one.
#
# Changes: 2026-09-29 (same branch, review fix) — name-gated promotion
# (``OPEN_SURFACE_TOOL_NAMES``) and the /studio/editor ``src`` shape check.

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_surfaces"

OPEN_SURFACE_TOOL_ID = f"mcp__{SERVER_NAME}__open_surface"

SURFACES_TOOL_IDS = (OPEN_SURFACE_TOOL_ID,)

# The tool_result names run_core promotes from: the SDK's full MCP id and the
# bare name a bridging backend may report. Exact match, not a suffix — another
# MCP server could register its own ``open_surface``.
OPEN_SURFACE_TOOL_NAMES = frozenset({OPEN_SURFACE_TOOL_ID, "open_surface"})

# /studio/editor ``src``: the backend's own media routes only (see header).
_EDITOR_SRC_RE = re.compile(r"/api/v1/(?:uploads|media)/[A-Za-z0-9][A-Za-z0-9._-]*")

# The contract's closed route set. Anything else is rejected, never forwarded.
ALLOWED_ROUTES = ("/files", "/studio/editor", "/chat", "/pockets", "/knowledge")

MAX_PARAMS = 10
MAX_PARAM_KEY_CHARS = 64
MAX_PARAM_VALUE_CHARS = 500
MAX_REASON_CHARS = 200


def _error_response(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(body, separators=(",", ":"), default=str)}]
    }


def validate_open_surface(args: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate ``{route, params?, reason?}`` into the wire payload.

    Returns ``(payload, None)`` or ``(None, error)``. The error is written for
    the model to read and fix in the same turn. ``params`` / ``reason`` are
    omitted from the payload when absent or empty, per the contract.
    """
    if not isinstance(args, dict):
        return None, "arguments must be an object with a route."

    route = args.get("route")
    route = route.strip() if isinstance(route, str) else ""
    if route not in ALLOWED_ROUTES:
        return None, (
            f"{route or '(empty)'!r} is not a surface you can open. "
            f"Valid routes: {', '.join(ALLOWED_ROUTES)}."
        )
    payload: dict[str, Any] = {"route": route}

    params = args.get("params")
    if params is not None and params != "":
        if not isinstance(params, dict):
            return None, "params must be a flat object of string keys to string values."
        if len(params) > MAX_PARAMS:
            return None, f"params has {len(params)} keys; at most {MAX_PARAMS} are allowed."
        for key, value in params.items():
            if not isinstance(key, str) or not key or len(key) > MAX_PARAM_KEY_CHARS:
                return None, (
                    f"param key {key!r} must be a non-empty string of at most "
                    f"{MAX_PARAM_KEY_CHARS} chars."
                )
            if not isinstance(value, str):
                return None, f"param {key!r} must be a string (got {type(value).__name__})."
            if len(value) > MAX_PARAM_VALUE_CHARS:
                return None, f"param {key!r} is over {MAX_PARAM_VALUE_CHARS} chars."
        src = params.get("src")
        if route == "/studio/editor" and src is not None and not _EDITOR_SRC_RE.fullmatch(src):
            return None, (
                "src for /studio/editor must be the file's own backend path, "
                "/api/v1/uploads/<file_id> or /api/v1/media/<name>, not an external URL."
            )
        if params:
            payload["params"] = dict(params)

    reason = args.get("reason")
    if reason is not None:
        if not isinstance(reason, str):
            return None, "reason must be a string."
        reason = reason.strip()
        if len(reason) > MAX_REASON_CHARS:
            return None, f"reason is over {MAX_REASON_CHARS} chars; keep it to one short line."
        if reason:
            payload["reason"] = reason

    return payload, None


async def _open_surface_handler(args: dict) -> dict:
    """Validate and hand the open request to the browser."""
    from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args

    # SDK callers that cannot pass a nested object through a flat signature
    # send ``params`` as a JSON string.
    payload, error = validate_open_surface(coerce_json_object_args(args, ("params",)))
    if error is not None:
        return _error_response(error)
    return _success_response(
        {
            "ok": True,
            "open_surface": payload,
            "note": (
                "Sent to the user's screen; the app opens it. Say what you opened "
                "and what they should do there — do not claim it has loaded."
            ),
        }
    )


OPEN_SURFACE_DESCRIPTION = """\
Open an app surface on the user's screen.

Use it when the user needs to SEE or CHOOSE something, not to answer a question
you can answer in text. /files to upload or pick files; to edit a video, open
/files first unless a clip is already known, then /studio/editor with the clip
handoff params src, name, mime, kind (src is the file's /api/v1/uploads/<id> or
/api/v1/media/<name> path); /chat to read a conversation; /pockets and
/knowledge to browse those. Returns once dispatched; the browser does the
opening."""


def _open_surface_schema() -> dict[str, Any]:
    # A full JSON Schema (type + properties), not the flat {name: spec} form:
    # the SDK marks every key of the flat form required, and params / reason
    # are optional in the contract.
    properties: dict[str, Any] = {
        "route": {
            "type": "string",
            "enum": list(ALLOWED_ROUTES),
            "description": "The surface to open.",
        },
        "params": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": (
                f"Optional flat string-to-string map (at most {MAX_PARAMS} keys, values "
                f"up to {MAX_PARAM_VALUE_CHARS} chars). For /studio/editor pass the clip "
                "handoff: src (/api/v1/uploads/<file_id> or /api/v1/media/<name>), name, "
                "mime, kind."
            ),
        },
        "reason": {
            "type": "string",
            "description": f"Optional one line (max {MAX_REASON_CHARS} chars) shown to the user.",
        },
    }
    return {"type": "object", "properties": properties, "required": ["route"]}


def build_surfaces_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_surfaces MCP disabled")
        return None

    @tool("open_surface", OPEN_SURFACE_DESCRIPTION, _open_surface_schema())
    async def open_surface(args):  # type: ignore[no-untyped-def]
        return await _open_surface_handler(args)

    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[open_surface])
    return SERVER_NAME, server


__all__ = [
    "ALLOWED_ROUTES",
    "OPEN_SURFACE_TOOL_ID",
    "OPEN_SURFACE_TOOL_NAMES",
    "SERVER_NAME",
    "SURFACES_TOOL_IDS",
    "build_surfaces_server",
    "validate_open_surface",
]
