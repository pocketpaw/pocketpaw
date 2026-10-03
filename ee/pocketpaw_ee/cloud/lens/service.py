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
# ``run_overview`` (admin-only at the router) returns the run's cached AI
# overview, or builds a compact digest of the run (prompt, assistant turns, tool
# calls, findings, cost) and asks the active agent backend for a 3-6 bullet
# overview via ``PocketPawCompilerBackend``, under ``baggage(paw.internal=true)``
# so paw-lens never counts that housekeeping call as a run. The result is PUT to
# paw-lens, which owns the cache. LLM failure or timeout is 503
# ``lens.overview_failed``; paw-lens errors keep their own CloudError.
#
# Settings are read at call time, not import, so test overrides apply (the
# cached ``get_settings`` still needs a restart in prod). An empty URL short-circuits to
# ``{"enabled": false}`` before any network I/O. Upstream JSON is otherwise
# returned as is (the UI's types follow the paw-lens contract). Path params
# arrive already validated by the router, so they are safe to splice into the
# upstream path.

from __future__ import annotations

import asyncio
import json as _json
import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.observability import baggage
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.kb.backend_adapter import PocketPawCompilerBackend
from pocketpaw_ee.cloud.lens.client import LensClient

logger = logging.getLogger(__name__)

_client = LensClient()

# Id patterns shared by the HTTP router and the MCP tools. A leading
# alphanumeric blocks ``.``/``..``, so nothing can path-inject upstream.
SAFE_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
AGENT_ID = r"^[A-Za-z0-9_-]{1,64}$"

OVERVIEW_TIMEOUT_SECONDS = 60.0
OVERVIEW_MAX_BYTES = 8 * 1024  # paw-lens rejects a longer overview text
DIGEST_MAX_CHARS = 24_000
_FIELD_MAX_CHARS = 600
_MAX_SPAN_FETCHES = 40
_SPAN_FETCH_CONCURRENCY = 8
_OVERVIEW_SYSTEM = (
    "You review one AI agent run for a workspace admin. Reply with 3-6 short "
    "markdown bullets and nothing else: what the user asked, what the agent did, "
    "what failed and why (if anything), cost or latency notes worth knowing, and "
    "one concrete suggested fix. Be specific; quote tool names and errors."
)


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


def _clip(value: Any, limit: int = _FIELD_MAX_CHARS) -> str:
    text = value if isinstance(value, str) else _json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _message_text(message: Any) -> str:
    """Best-effort text of one gen_ai message (``content`` or ``parts``)."""
    if not isinstance(message, dict):
        return _clip(message)
    for key in ("content", "parts"):
        if key in message:
            return _clip(message[key])
    return _clip(message)


def build_digest(detail: dict[str, Any], spans: list[Any]) -> str:
    """Compact, size-capped text view of a run for the overview prompt."""
    run = detail.get("run") or {}
    lines = [
        f"Agent: {run.get('agent', '?')}  model: {run.get('model', '?')}  "
        f"status: {run.get('status', '?')}  duration_ms: {run.get('duration_ms', '?')}",
        f"Tokens: {_clip(run.get('tokens', {}))}  cost_usd: {run.get('cost_usd', '?')}",
    ]
    prompt_seen = False
    for span in spans:
        if not isinstance(span, dict):
            continue
        messages = span.get("messages") or {}
        if not prompt_seen:
            users = [m for m in messages.get("input") or [] if (m or {}).get("role") == "user"]
            if users:
                lines.append(f"User prompt: {_message_text(users[-1])}")
                prompt_seen = True
        for out in messages.get("output") or []:
            lines.append(f"Assistant: {_message_text(out)}")
        tool = span.get("tool")
        if isinstance(tool, dict):
            outcome = (
                f"error: {_clip(span['error'])}"
                if span.get("error")
                else (f"result: {_clip(tool.get('result'))}")
            )
            lines.append(
                f"Tool {tool.get('name', '?')}({_clip(tool.get('arguments'))}) -> {outcome}"
            )
        elif span.get("error"):
            lines.append(f"Span {span.get('name', '?')} error: {_clip(span['error'])}")
    for finding in detail.get("findings") or []:
        lines.append(
            f"Finding [{finding.get('severity', '?')}] {finding.get('detector', '?')}: "
            f"{_clip(finding.get('message', ''))}"
        )
    digest = "\n".join(lines)
    if len(digest) > DIGEST_MAX_CHARS:
        digest = digest[:DIGEST_MAX_CHARS] + "\n…[digest truncated]"
    return digest


def _overview_model(settings: Any) -> str:
    backend = getattr(settings, "agent_backend", "") or ""
    field = {"claude_agent_sdk": "claude_sdk_model"}.get(backend, f"{backend}_model")
    return getattr(settings, field, "") or backend or "unknown"


async def run_overview(
    workspace_id: str, trace_id: str, *, refresh: bool, generated_by: str
) -> Any:
    """The run's AI overview ``{text, model, created_at}``; cached unless ``refresh``."""
    detail = await get_run(workspace_id, trace_id, full=True)
    if detail == _disabled():
        return detail
    if detail.get("overview") and not refresh:
        return detail["overview"]

    wanted = [
        s.get("span_id")
        for s in detail.get("spans") or []
        if s.get("kind") in ("model", "tool") or s.get("error")
    ][:_MAX_SPAN_FETCHES]
    gate = asyncio.Semaphore(_SPAN_FETCH_CONCURRENCY)

    async def _span(span_id: str) -> Any:
        async with gate:
            try:
                return await get_span(workspace_id, trace_id, span_id, full=True)
            except CloudError:
                return None  # one unreadable span must not sink the overview

    spans = await asyncio.gather(*(_span(sid) for sid in wanted if sid))
    prompt = f"Run {trace_id}:\n{build_digest(detail, list(spans))}"

    settings = get_settings()
    try:
        with baggage(**{"paw.internal": "true", "paw.workspace_id": workspace_id}):
            raw = await asyncio.wait_for(
                PocketPawCompilerBackend().complete(prompt, system_prompt=_OVERVIEW_SYSTEM),
                timeout=OVERVIEW_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # noqa: BLE001 — any LLM failure is the same 503
        logger.warning("lens overview for %s failed: %s", trace_id, type(exc).__name__)
        raise CloudError(503, "lens.overview_failed", "Could not generate the overview.") from exc
    text = (raw or "").strip().encode()[:OVERVIEW_MAX_BYTES].decode(errors="ignore")
    if not text:
        raise CloudError(503, "lens.overview_failed", "Could not generate the overview.")

    model = _overview_model(settings)
    stored = await _call(
        "PUT",
        f"/v1/runs/{trace_id}/overview",
        workspace_id,
        json={"text": text, "model": model, "generated_by": generated_by},
    )
    created_at = stored.get("created_at") if isinstance(stored, dict) else None
    return {"text": text, "model": model, "created_at": created_at or datetime.now(UTC).isoformat()}
