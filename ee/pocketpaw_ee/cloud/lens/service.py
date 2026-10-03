# ee/pocketpaw_ee/cloud/lens/service.py — workspace-scoped calls to paw-lens.
#
# One module-level function per paw-lens route. Each takes the caller's
# ``workspace_id`` (from ``current_workspace_id``, never the request) and sends
# it upstream as the ``workspace_id`` query param on every call, GETs and POSTs
# alike. Optional filters (``since``, ``status``, ``agent_id``, ``automation``,
# ``limit``) are forwarded only when set.
#
# Settings are read at call time, not import, so test overrides apply (the
# cached ``get_settings`` still needs a restart in prod). An empty URL short-circuits to
# ``{"enabled": false}`` before any network I/O. Upstream JSON is returned as
# is (the UI's types follow the paw-lens contract). Path params arrive already
# validated by the router, so they are safe to splice into the upstream path.

from __future__ import annotations

from typing import Any

from pocketpaw.config import get_settings
from pocketpaw_ee.cloud.lens.client import LensClient

_client = LensClient()


def _disabled() -> dict[str, bool]:
    return {"enabled": False}


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


async def get_issue(workspace_id: str, fingerprint: str, since: str | None = None) -> Any:
    return await _call("GET", f"/v1/issues/{fingerprint}", workspace_id, {"since": since})


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
) -> Any:
    params = {
        "agent_id": agent_id,
        "automation": automation,
        "status": status,
        "since": since,
        "limit": str(limit) if limit is not None else None,
    }
    return await _call("GET", "/v1/runs", workspace_id, params)


async def get_run(workspace_id: str, trace_id: str, since: str | None = None) -> Any:
    return await _call("GET", f"/v1/runs/{trace_id}", workspace_id, {"since": since})


async def get_span(workspace_id: str, trace_id: str, span_id: str) -> Any:
    return await _call("GET", f"/v1/runs/{trace_id}/spans/{span_id}", workspace_id)


async def list_agents(
    workspace_id: str, since: str | None = None, agent_id: str | None = None
) -> Any:
    return await _call("GET", "/v1/agents", workspace_id, {"since": since, "agent_id": agent_id})


async def list_monitors(workspace_id: str, since: str | None = None) -> Any:
    return await _call("GET", "/v1/monitors", workspace_id, {"since": since})


async def get_monitor(workspace_id: str, slug: str, since: str | None = None) -> Any:
    return await _call("GET", f"/v1/monitors/{slug}", workspace_id, {"since": since})
