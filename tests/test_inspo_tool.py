# tests/test_inspo_tool.py — the Inspo design-research tool on the non-SDK backends.
#
# Created 2026-09-24 (feat/inspo-backend-parity). Inspo reached only the
# claude_agent_sdk backend (the EE in-process ``pocketpaw_inspo`` server), while
# Refero already had BaseTools for pydantic_ai and friends. These tests cover the
# OSS module both surfaces now share: the upstream call, and the two BaseTools
# that expose it off the SDK path.
#
# As with Refero, the load-bearing tests are the fail-soft ones. The archive is a
# free third-party service with no SLA, so a create turn must keep moving when it
# is down — the tools answer ``ok: false`` with a "proceed without it" message
# and never raise into the turn.
#
# The transport is an ``httpx.MockTransport`` injected at ``inspo._TRANSPORT``,
# the same seam ``refero`` and ``stock_images`` expose, so the JSON-RPC exchange
# is exercised for real with no network.

from __future__ import annotations

import json

import httpx
import pytest

from pocketpaw.tools.builtin import inspo


def _mock(handler):
    """Install a MockTransport for one test and hand back the captured requests."""
    seen: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    inspo._TRANSPORT = httpx.MockTransport(_capture)
    return seen


@pytest.fixture(autouse=True)
def _clear_transport():
    yield
    inspo._TRANSPORT = None


def _mcp_result(body: dict) -> httpx.Response:
    """An upstream MCP ``tools/call`` success, in the shape the real one uses."""
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(body)}]},
        },
    )


# ---------------------------------------------------------------------------
# The shared upstream call
# ---------------------------------------------------------------------------


async def test_research_calls_recommend_with_the_brief_and_a_token_cap():
    seen = _mock(lambda _r: _mcp_result({"macrostructure": "stat-led"}))

    body = await inspo.research_page_design("  landing page for a dental clinic  ")

    assert body == {"macrostructure": "stat-led"}
    sent = json.loads(seen[0].content)
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "recommend"
    assert sent["params"]["arguments"]["brief"] == "landing page for a dental clinic"
    assert sent["params"]["arguments"]["maxTokens"] == inspo._MAX_TOKENS


async def test_design_system_calls_get_design_system_with_the_slug():
    seen = _mock(lambda _r: _mcp_result({"fonts": ["Inter"]}))

    body = await inspo.get_reference_design_system(" linear-app ")

    assert body == {"fonts": ["Inter"]}
    sent = json.loads(seen[0].content)
    assert sent["params"] == {"name": "get_design_system", "arguments": {"slug": "linear-app"}}


async def test_a_non_json_text_result_comes_back_as_text():
    _mock(
        lambda _r: httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "# DESIGN.md"}]},
            },
        )
    )

    assert await inspo.get_reference_design_system("x") == {"text": "# DESIGN.md"}


async def test_a_jsonrpc_error_raises_for_the_caller_to_soften():
    _mock(
        lambda _r: httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "rate limited"}},
        )
    )

    with pytest.raises(RuntimeError, match="rate limited"):
        await inspo.research_page_design("a bakery")


def test_endpoint_defaults_to_the_hosted_archive():
    assert inspo.endpoint() == "https://inspomcp.dev/api/mcp"


# ---------------------------------------------------------------------------
# The BaseTools
# ---------------------------------------------------------------------------


async def test_the_research_tool_returns_the_reference():
    _mock(lambda _r: _mcp_result({"macrostructure": "feature-stack"}))

    out = json.loads(await inspo.InspoResearchTool().execute(brief="a SaaS landing page"))

    assert out == {"ok": True, "reference": {"macrostructure": "feature-stack"}}


async def test_the_design_system_tool_returns_the_system_for_a_slug():
    _mock(lambda _r: _mcp_result({"palette": ["#111"]}))

    out = json.loads(await inspo.InspoDesignSystemTool().execute(slug="stripe-com"))

    assert out == {"ok": True, "slug": "stripe-com", "design_system": {"palette": ["#111"]}}


async def test_the_tools_fail_soft_when_the_archive_is_down():
    _mock(lambda _r: httpx.Response(503, text="unavailable"))

    research = json.loads(await inspo.InspoResearchTool().execute(brief="a bakery"))
    system = json.loads(await inspo.InspoDesignSystemTool().execute(slug="stripe-com"))

    assert research["ok"] is False and "do not retry" in research["error"]
    assert system["ok"] is False and "unavailable" in system["error"]


async def test_the_tools_refuse_an_empty_argument_without_calling_out():
    seen = _mock(lambda _r: _mcp_result({}))

    research = json.loads(await inspo.InspoResearchTool().execute(brief="   "))
    system = json.loads(await inspo.InspoDesignSystemTool().execute(slug=""))

    assert research["ok"] is False and system["ok"] is False
    assert seen == []


def test_both_tools_declare_a_usable_schema():
    for tool, required in (
        (inspo.InspoResearchTool(), "brief"),
        (inspo.InspoDesignSystemTool(), "slug"),
    ):
        assert tool.name.startswith("inspo_")
        assert tool.parameters["required"] == [required]
        assert required in tool.parameters["properties"]
        assert tool.trust_level == "standard"


def test_the_tools_are_registered_and_classified_tenant_safe():
    """Registered in the lazy map (so ``tool_bridge`` builds them) AND reviewed as
    tenant-safe (so pydantic_ai does not withhold them). Missing either one means
    the tool silently never reaches a non-SDK agent."""
    from pocketpaw.agents.pydantic_ai import _TENANT_SAFE_TOOLS
    from pocketpaw.tools.builtin import _LAZY_IMPORTS

    assert _LAZY_IMPORTS["InspoResearchTool"] == (".inspo", "InspoResearchTool")
    assert _LAZY_IMPORTS["InspoDesignSystemTool"] == (".inspo", "InspoDesignSystemTool")
    assert {"inspo_research_page_design", "inspo_reference_design_system"} <= _TENANT_SAFE_TOOLS
