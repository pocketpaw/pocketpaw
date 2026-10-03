# ee/pocketpaw_ee/cloud/lens/service.py — workspace-scoped calls to paw-lens.
#
# One module-level function per paw-lens route. Each takes the caller's
# ``workspace_id`` (from ``current_workspace_id``, never the request) and sends
# it upstream as the ``workspace_id`` query param on every call, GETs and POSTs
# alike. Optional filters (``since``, ``status``, ``agent_id``, ``automation``,
# ``limit``) are forwarded only when set.
#
# Privacy A: the reads that can carry message or tool content (runs list, run
# detail, span detail, issue detail) take a REQUIRED keyword ``full``. When it is
# False (caller is not a workspace admin), ``redact`` strips the content and
# marks dict bodies ``content_hidden: true``. ``redact`` is the one stripping
# function: the HTTP proxy and the ``pocketpaw_lens`` MCP tools both go through
# these functions, so neither can drift from the other.
#
# Settings are read at call time, not import, so test overrides apply (the
# cached ``get_settings`` still needs a restart in prod). An empty URL short-circuits to
# ``{"enabled": false}`` before any network I/O. Upstream JSON is otherwise
# returned as is (the UI's types follow the paw-lens contract). Path params
# arrive already validated by the router, so they are safe to splice into the
# upstream path.

from __future__ import annotations

from typing import Any

from pocketpaw.config import get_settings
from pocketpaw_ee.cloud.lens.client import LensClient

_client = LensClient()


def _disabled() -> dict[str, bool]:
    return {"enabled": False}


# Span attribute keys whose values are prompt / completion / tool payloads.
_HIDDEN_ATTR_PREFIXES = (
    "gen_ai.input.",
    "gen_ai.output.",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "pydantic_ai.all_messages",
)
HIDDEN = "[hidden]"


def _strip(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("summary", "args_preview") and isinstance(value, str):
            out[key] = ""
        elif key == "messages":
            out[key] = None
        elif key == "tool" and isinstance(value, dict):
            out[key] = {**value, "arguments": None, "result": None}
        elif key == "attributes" and isinstance(value, dict):
            out[key] = {
                k: HIDDEN if k.startswith(_HIDDEN_ATTR_PREFIXES) else v for k, v in value.items()
            }
        else:
            out[key] = _strip(value)
    return out


def redact(body: Any, full: bool) -> Any:
    """Privacy A. ``full`` (workspace admin) returns ``body`` untouched; otherwise
    run summaries, span messages, tool arguments/results and content-bearing
    attributes are stripped, and a dict body gains ``content_hidden: true``. A
    list body (the runs list) cannot carry the flag and is only stripped."""
    if full or body == _disabled():
        return body
    out = _strip(body)
    if isinstance(out, dict):
        out["content_hidden"] = True
    return out


async def _call(
    method: str,
    path: str,
    workspace_id: str,
    params: dict[str, str | None] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    settings = get_settings()
    base_url = (settings.lens_api_url or "").strip().rstrip("/")
    if not base_url:
        return _disabled()
    query = {k: v for k, v in (params or {}).items() if v is not None}
    query["workspace_id"] = workspace_id
    return await _client.request(
        method,
        base_url,
        path,
        token=settings.lens_api_token or "",
        params=query,
        json=json,
    )


async def overview(workspace_id: str, since: str | None = None, agent_id: str | None = None) -> Any:
    return await _call("GET", "/v1/overview", workspace_id, {"since": since, "agent_id": agent_id})


async def list_issues(
    workspace_id: str,
    status: str | None = None,
    since: str | None = None,
    agent_id: str | None = None,
) -> Any:
    return await _call(
        "GET",
        "/v1/issues",
        workspace_id,
        {"status": status, "since": since, "agent_id": agent_id},
    )


async def get_issue(
    workspace_id: str, fingerprint: str, since: str | None = None, *, full: bool
) -> Any:
    body = await _call("GET", f"/v1/issues/{fingerprint}", workspace_id, {"since": since})
    return redact(body, full)


async def mute_issue(workspace_id: str, fingerprint: str, minutes: int) -> Any:
    return await _call(
        "POST", f"/v1/issues/{fingerprint}/mute", workspace_id, json={"minutes": minutes}
    )


async def resolve_issue(workspace_id: str, fingerprint: str) -> Any:
    return await _call("POST", f"/v1/issues/{fingerprint}/resolve", workspace_id)


async def list_runs(
    workspace_id: str,
    agent_id: str | None = None,
    automation: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    *,
    full: bool,
) -> Any:
    params = {
        "agent_id": agent_id,
        "automation": automation,
        "status": status,
        "since": since,
        "limit": str(limit) if limit is not None else None,
    }
    return redact(await _call("GET", "/v1/runs", workspace_id, params), full)


async def get_run(workspace_id: str, trace_id: str, since: str | None = None, *, full: bool) -> Any:
    body = await _call("GET", f"/v1/runs/{trace_id}", workspace_id, {"since": since})
    return redact(body, full)


async def get_span(workspace_id: str, trace_id: str, span_id: str, *, full: bool) -> Any:
    body = await _call("GET", f"/v1/runs/{trace_id}/spans/{span_id}", workspace_id)
    return redact(body, full)


async def list_agents(
    workspace_id: str, since: str | None = None, agent_id: str | None = None
) -> Any:
    return await _call("GET", "/v1/agents", workspace_id, {"since": since, "agent_id": agent_id})


async def list_monitors(workspace_id: str, since: str | None = None) -> Any:
    return await _call("GET", "/v1/monitors", workspace_id, {"since": since})


async def get_monitor(workspace_id: str, slug: str, since: str | None = None) -> Any:
    return await _call("GET", f"/v1/monitors/{slug}", workspace_id, {"since": since})
