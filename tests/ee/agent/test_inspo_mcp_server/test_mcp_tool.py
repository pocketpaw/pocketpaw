# tests/ee/agent/test_inspo_mcp_server/test_mcp_tool.py
# Created: 2026-09-16 (feat/sites-bundled-design-research) — coverage for the
# in-process ``pocketpaw_inspo`` MCP server, which gives the /sites authoring
# agent design research over an archive of real shipped websites. Mirrors the
# icons / stock_images layout: registration assertions (server name, tool id
# namespacing, build shape, provider allowlist publication) plus per-handler
# tests that inject an httpx.MockTransport (NO live network) and inspect the MCP
# envelope the SDK returns — happy path, missing argument, upstream error, and
# the fail-soft that keeps a create turn moving when the archive is down.
"""MCP server registration + handler tests for the design-research tool."""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.agent.mcp_servers import inspo as inspo_mcp  # noqa: E402,I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_payload(envelope: dict) -> dict:
    """MCP responses pack the JSON body into ``content[0].text``."""
    assert "content" in envelope
    return json.loads(envelope["content"][0]["text"])


def _mock_upstream(monkeypatch, handler):
    """Point the server's httpx client at ``handler`` instead of the network.

    Patches ``httpx.AsyncClient`` itself rather than the module's own symbol,
    because the client is imported INSIDE ``_call_upstream`` — a module-level
    patch would miss it.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


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
# Registration
# ---------------------------------------------------------------------------


def test_tool_ids_namespace_under_the_server_name():
    """Claude Code matches ``mcp__<server>__<tool>`` by exact string, and the
    /sites allow-list is built from these constants — a drift between the id and
    the tool's registered name makes the tool silently unreachable."""
    assert inspo_mcp.SERVER_NAME == "pocketpaw_inspo"
    for tool_id in inspo_mcp.INSPO_TOOL_IDS:
        assert tool_id.startswith(f"mcp__{inspo_mcp.SERVER_NAME}__")


def test_every_published_tool_id_maps_to_an_upstream_tool():
    """``_UPSTREAM`` is the whole integration surface.

    Our tools are named for the job and theirs for their catalogue, so this map
    is the only place the two vocabularies meet. A published id with no entry
    here is a tool that reaches the archive and asks it for nothing.
    """
    published = {t.split("__")[-1] for t in inspo_mcp.INSPO_TOOL_IDS}
    assert published == set(inspo_mcp._UPSTREAM)


def test_build_returns_the_name_server_pair_the_backend_expects():
    """Same ``(name, server)`` / ``None`` shape as ``build_stock_server``, which
    is what lets the backend's registration loop treat them identically."""
    built = inspo_mcp.build_inspo_server()
    if built is None:  # claude_agent_sdk not installed — the documented degrade
        pytest.skip("claude_agent_sdk not installed")
    name, server = built
    assert name == inspo_mcp.SERVER_NAME
    assert server is not None


def test_the_provider_publishes_the_same_ids():
    """The provider is what ``_collect_mcp_tool_ids`` reads. If it published a
    different set, the allow-list and the live tools would disagree."""
    from pocketpaw_ee.extensions import CloudInspoMcpProvider

    assert list(CloudInspoMcpProvider().tool_ids()) == list(inspo_mcp.INSPO_TOOL_IDS)


# ---------------------------------------------------------------------------
# research_page_design
# ---------------------------------------------------------------------------


async def test_research_returns_the_reference(monkeypatch):
    """Happy path: the upstream result is unwrapped and handed to the agent."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _mcp_result({"pick": {"macrostructure": {"slug": "feature-stack"}}})

    _mock_upstream(monkeypatch, handler)
    envelope = await inspo_mcp._research_handler({"brief": "a dental clinic"})

    assert not envelope.get("is_error")
    body = _decode_payload(envelope)
    assert body["ok"] is True
    assert body["reference"]["pick"]["macrostructure"]["slug"] == "feature-stack"
    # Calls the upstream tool this one maps to, not our own name.
    assert captured["params"]["name"] == "recommend"
    assert captured["params"]["arguments"]["brief"] == "a dental clinic"


async def test_research_rejects_an_empty_brief(monkeypatch):
    """Guard before the network. A blank brief would burn a request on the
    archive's rate limit and return something unrelated to this site."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the network")

    _mock_upstream(monkeypatch, handler)
    envelope = await inspo_mcp._research_handler({"brief": "   "})

    assert envelope["is_error"] is True


async def test_research_fails_soft_when_the_archive_is_down(monkeypatch):
    """THE ONE THAT MATTERS IN PRODUCTION.

    This is a free third-party service with no SLA sitting in the create path.
    It must never take a site build down with it: the handler returns an MCP
    error whose text tells the agent to proceed on its own inference, which is
    what the preamble's ROBUSTNESS rule already says to do.

    THE MUTATION THAT BREAKS THIS: let the exception propagate. Run: the archive
    having a bad day becomes a failed site build.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("archive unreachable")

    _mock_upstream(monkeypatch, handler)
    envelope = await inspo_mcp._research_handler({"brief": "a dental clinic"})

    assert envelope["is_error"] is True
    text = envelope["content"][0]["text"].lower()
    assert "proceed" in text
    assert "do not retry" in text


async def test_research_surfaces_a_jsonrpc_error(monkeypatch):
    """An upstream JSON-RPC ``error`` is a failure even though the HTTP status
    is 200 — reading only the status code would hand the agent an empty
    reference and let it build on nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "rate limited"}}
        )

    _mock_upstream(monkeypatch, handler)
    envelope = await inspo_mcp._research_handler({"brief": "a dental clinic"})

    assert envelope["is_error"] is True
    assert "rate limited" in envelope["content"][0]["text"]


# ---------------------------------------------------------------------------
# get_reference_design_system
# ---------------------------------------------------------------------------


async def test_design_system_returns_the_reference_for_a_slug(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _mcp_result({"fonts": ["Instrument Sans"], "palette": []})

    _mock_upstream(monkeypatch, handler)
    envelope = await inspo_mcp._design_system_handler({"slug": "acme-landing"})

    body = _decode_payload(envelope)
    assert body["ok"] is True
    assert body["slug"] == "acme-landing"
    assert body["design_system"]["fonts"] == ["Instrument Sans"]
    assert captured["params"]["name"] == "get_design_system"


async def test_design_system_requires_a_slug(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not reach the network")

    _mock_upstream(monkeypatch, handler)
    envelope = await inspo_mcp._design_system_handler({})

    assert envelope["is_error"] is True


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_endpoint_defaults_to_the_hosted_archive():
    assert inspo_mcp._endpoint() == "https://inspomcp.dev/api/mcp"


def test_endpoint_honours_the_self_host_override(monkeypatch):
    """The hosted archive rate-limits per IP and a multi-tenant deploy is one
    egress IP for every tenant, so self-hosting has to be a config change rather
    than a code change."""
    from pocketpaw import config as pp_config

    monkeypatch.setenv("POCKETPAW_INSPO_MCP_URL", "https://inspo.internal/mcp")
    pp_config.get_settings.cache_clear()
    try:
        assert inspo_mcp._endpoint() == "https://inspo.internal/mcp"
    finally:
        monkeypatch.delenv("POCKETPAW_INSPO_MCP_URL", raising=False)
        pp_config.get_settings.cache_clear()
