# tests/cloud/lens/test_lens_mcp.py — the ``pocketpaw_lens`` MCP tools and the
# surface-scoped registration that keeps them off every other chat.
#
# Tools: the handlers run against the conftest MockTransport upstream with the
# stream identity (agent_service ContextVar readers) and the user loader
# patched. Covers: the caller's workspace is sent upstream, a member's run /
# span content is stripped (same ``redact`` as the proxy), an admin's is not, a
# missing user fails closed, bad ids are rejected with no upstream call, and no
# workspace is an error.
# Gating: ``surface_scoped_tool_deny`` names the lens ids unless granted;
# claude_sdk ``_build_options`` drops the server and its ids from a normal chat
# and keeps both when the agent_health profile's grant is passed; pydantic_ai
# adds the ungranted lens ids to the deny set it builds the agent with.

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pocketpaw_ee.agent.mcp_servers import lens as lens_mcp
from pocketpaw_ee.cloud.chat import agent_service
from pocketpaw_ee.cloud.surface.domain import SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.service import resolve_profile

from pocketpaw.tools.policy import surface_scoped_tool_deny
from tests.cloud.lens.test_router import _RUN, _SPAN, _settings

TRACE = "a" * 32


def _as_user(monkeypatch, role: str | None, workspace: str | None = "ws_test") -> None:
    monkeypatch.setattr(agent_service, "current_workspace_id", lambda: workspace)
    monkeypatch.setattr(agent_service, "current_user_id", lambda: "u1")
    user = (
        None
        if role is None
        else SimpleNamespace(id="u1", workspaces=[SimpleNamespace(workspace="ws_test", role=role)])
    )

    async def _load(_uid):
        return user

    monkeypatch.setattr(lens_mcp, "_load_user", _load)


def _body(result: dict):
    assert not result.get("is_error"), result
    return json.loads(result["content"][0]["text"])


async def test_run_uses_caller_workspace_and_strips_for_member(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "member")
    upstream(lambda req: httpx.Response(200, json=_RUN))
    got = _body(await lens_mcp._run_handler({"trace_id": TRACE}))
    assert seen[0].url.path == f"/v1/runs/{TRACE}"
    assert seen[0].url.params["workspace_id"] == "ws_test"
    assert got["content_hidden"] is True
    assert got["run"]["summary"] == ""


async def test_span_full_for_admin(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "admin")
    upstream(lambda req: httpx.Response(200, json=_SPAN))
    got = _body(await lens_mcp._run_handler({"trace_id": TRACE, "span_id": "s1"}))
    assert got == _SPAN


@pytest.mark.parametrize("role", ["member", None])
async def test_span_stripped_for_member_or_unknown_user(monkeypatch, upstream, role):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, role)
    upstream(lambda req: httpx.Response(200, json=_SPAN))
    got = _body(await lens_mcp._run_handler({"trace_id": TRACE, "span_id": "s1"}))
    assert got["messages"] is None
    assert got["tool"]["arguments"] is None
    assert got["attributes"]["gen_ai.input.messages"] == "[hidden]"


async def test_runs_and_issue_detail_strip_for_member(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "member")
    upstream(lambda req: httpx.Response(200, json=[{"summary": "secret"}]))
    assert _body(await lens_mcp._runs_handler({"status": "error", "limit": 5})) == [{"summary": ""}]
    assert dict(seen[0].url.params) == {"status": "error", "limit": "5", "workspace_id": "ws_test"}
    upstream(lambda req: httpx.Response(200, json={"runs": [{"summary": "secret"}]}))
    got = _body(await lens_mcp._issues_handler({"fingerprint": "fp1"}))
    assert got["runs"] == [{"summary": ""}]


async def test_overview_and_monitors_pass_through(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "member")
    upstream(lambda req: httpx.Response(200, json={"runs": 3}))
    assert _body(await lens_mcp._overview_handler({"agent_id": "ag1"})) == {"runs": 3}
    assert _body(await lens_mcp._monitors_handler({"slug": "reminder:r1"})) == {"runs": 3}
    assert [r.url.path for r in seen] == ["/v1/overview", "/v1/monitors/reminder:r1"]


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        ("_run_handler", {"trace_id": "../x"}),
        ("_run_handler", {"trace_id": TRACE, "span_id": "a/b"}),
        ("_run_handler", {}),
        ("_runs_handler", {"agent_id": "a.b"}),
        ("_runs_handler", {"limit": 500}),
        ("_runs_handler", {"status": "running"}),
        ("_issues_handler", {"fingerprint": "-x"}),
        ("_monitors_handler", {"slug": "a?b"}),
        ("_overview_handler", {"since": "x" * 100}),
    ],
)
async def test_bad_args_rejected_without_upstream(monkeypatch, upstream, seen, handler, args):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "admin")
    result = await getattr(lens_mcp, handler)(args)
    assert result["is_error"] is True
    assert seen == []


async def test_no_workspace_is_error(monkeypatch, upstream, seen):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "admin", workspace=None)
    result = await lens_mcp._overview_handler({})
    assert result["is_error"] is True
    assert seen == []


async def test_upstream_error_is_tool_error(monkeypatch, upstream):
    _settings(monkeypatch, "http://lens:8790")
    _as_user(monkeypatch, "admin")
    upstream(lambda req: httpx.Response(404))
    result = await lens_mcp._run_handler({"trace_id": TRACE})
    assert result["is_error"] is True
    assert "lens.not_found" in result["content"][0]["text"]


# --- surface-scoped registration -------------------------------------------


def test_only_agent_health_profile_grants_lens():
    granted = resolve_profile(SurfaceKind.AGENT_HEALTH, SurfaceMeta()).allowed_sdk_tools
    assert granted == frozenset(lens_mcp.LENS_TOOL_IDS)
    for kind in SurfaceKind:
        if kind is SurfaceKind.AGENT_HEALTH:
            continue
        allow = resolve_profile(kind, SurfaceMeta()).allowed_sdk_tools or frozenset()
        assert not allow & granted, kind


def test_scoped_deny_names_lens_unless_granted():
    assert surface_scoped_tool_deny(frozenset()) == frozenset(lens_mcp.LENS_TOOL_IDS)
    assert surface_scoped_tool_deny(frozenset(lens_mcp.LENS_TOOL_IDS)) == frozenset()


def test_lens_server_builds():
    built = lens_mcp.build_lens_server()
    assert built is not None and built[0] == "pocketpaw_lens"


@pytest.fixture
def sdk_backend(tmp_path, monkeypatch):
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
    from pocketpaw.config import get_settings

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    backend = ClaudeSDKBackend(get_settings())
    other = "mcp__pocketpaw_ask__ask_user"
    monkeypatch.setattr(backend, "_collect_mcp_tool_ids", lambda: [other, *lens_mcp.LENS_TOOL_IDS])
    return backend


async def _build(backend, allow_sdk_tools):
    return await backend._build_options(
        "hello",
        system_prompt="You are Paw.",
        session_key=None,
        deny_mcp_tool_ids=frozenset(),
        allow_sdk_tools=allow_sdk_tools,
        allow_mcp_tool_ids=None,
        skill_names=frozenset(),
        stderr_sink=[],
    )


async def test_normal_chat_has_no_lens_server(sdk_backend):
    kwargs = (await _build(sdk_backend, frozenset())).options_kwargs
    assert "pocketpaw_ask" in kwargs["mcp_servers"]
    assert "pocketpaw_lens" not in kwargs["mcp_servers"]
    assert not set(lens_mcp.LENS_TOOL_IDS) & set(kwargs["allowed_tools"])
    assert "mcp__pocketpaw_ask__ask_user" in kwargs["allowed_tools"]


async def test_agent_health_surface_has_lens_server(sdk_backend):
    grant = resolve_profile(SurfaceKind.AGENT_HEALTH, SurfaceMeta()).allowed_sdk_tools
    kwargs = (await _build(sdk_backend, grant)).options_kwargs
    assert {"pocketpaw_ask", "pocketpaw_lens"} <= set(kwargs["mcp_servers"])
    assert set(lens_mcp.LENS_TOOL_IDS) <= set(kwargs["allowed_tools"])


@pytest.mark.parametrize("granted", [False, True])
async def test_pydantic_ai_denies_lens_unless_granted(granted):
    from pydantic_ai.models.test import TestModel

    from tests.test_pydantic_ai_backend import _backend_with_model, _collect

    backend = _backend_with_model(TestModel(custom_output_text="ok"))
    real, denies = backend._get_or_create_agent, []

    def _watch(*args, **kwargs):
        denies.append(kwargs["deny_mcp_tool_ids"])
        return real(*args, **kwargs)

    backend._get_or_create_agent = _watch  # type: ignore[method-assign]
    grant = frozenset(lens_mcp.LENS_TOOL_IDS) if granted else frozenset()
    await _collect(backend, "hi", session_key="s1", allow_sdk_tools=grant)
    lens_denied = set(lens_mcp.LENS_TOOL_IDS) & set(denies[0])
    assert lens_denied == (set() if granted else set(lens_mcp.LENS_TOOL_IDS))
