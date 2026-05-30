# Created: 2026-05-21 — refactor/gate-planner-mcp. Unit tests for the
#   per-agent planner opt-in wired through ``AgentPool._build``. A cloud
#   agent enables the ``pocketpaw_planner`` MCP server by listing the bare
#   token ``"pocketpaw_planner"`` in its ``config.tools``; ``_build``
#   translates that into a ``ToolPolicy.mcp_servers_allow`` frozenset and
#   passes the policy to the Claude SDK backend. Covers: off by default,
#   on when opted in, the non-regression case (global ``tools_allow``
#   stays intact), deny-wins, and unknown-token filtering.
"""Tests for the AgentPool planner opt-in."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.pool import AgentPool
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy


class _FakeConfig:
    """Stand-in for the Beanie ``AgentConfig`` sub-model.

    ``_build`` calls ``agent_doc.config.model_dump()``; this returns a plain
    dict with just the fields ``_build`` reads.
    """

    def __init__(self, **fields):
        base = {
            "backend": "claude_agent_sdk",
            "model": "",
            "tools": [],
            "soul_enabled": False,
        }
        base.update(fields)
        self._fields = base

    def model_dump(self) -> dict:
        return dict(self._fields)


def _make_agent_doc(**config_fields) -> SimpleNamespace:
    return SimpleNamespace(
        id="agent-1",
        name="Test Agent",
        config=_FakeConfig(**config_fields),
        updatedAt=datetime.now(UTC),
    )


class _CapturingBackend:
    """Backend stub that records the policy it was constructed with.

    Subbed in for ``ClaudeSDKBackend`` so ``_build`` can run without the
    real Claude SDK. The class identity check in ``_build`` keys off the
    real ``ClaudeSDKBackend``, so the patch must replace that name.
    """

    last_settings: Settings | None = None
    last_policy: ToolPolicy | None = None

    def __init__(self, settings: Settings, policy: ToolPolicy | None = None):
        _CapturingBackend.last_settings = settings
        _CapturingBackend.last_policy = policy


async def _build_with(agent_doc, settings: Settings) -> ToolPolicy:
    """Run ``AgentPool._build`` with stubbed backend + settings, return the
    ``ToolPolicy`` the Claude SDK backend was constructed with."""
    _CapturingBackend.last_policy = None
    _CapturingBackend.last_settings = None
    pool = AgentPool()
    with (
        patch(
            "pocketpaw.agents.registry.get_backend_class",
            return_value=_CapturingBackend,
        ),
        patch("pocketpaw.agents.claude_sdk.ClaudeSDKBackend", _CapturingBackend),
        patch("pocketpaw.config.Settings.load", return_value=settings),
    ):
        await pool._build(agent_doc)
    assert _CapturingBackend.last_policy is not None, "backend got no policy"
    return _CapturingBackend.last_policy


@pytest.mark.asyncio
async def test_no_tools_planner_off():
    """Agent with ``tools=[]`` — the planner is not opted in."""
    doc = _make_agent_doc(tools=[])
    policy = await _build_with(doc, Settings(anthropic_api_key="k"))
    assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False


@pytest.mark.asyncio
async def test_planner_token_opts_planner_in():
    """Agent listing ``pocketpaw_planner`` in ``tools`` opts the planner in."""
    doc = _make_agent_doc(tools=["pocketpaw_planner"])
    policy = await _build_with(doc, Settings(anthropic_api_key="k"))
    assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is True


@pytest.mark.asyncio
async def test_opt_in_does_not_disable_other_tools():
    """Non-regression: opting the planner in must not flip the policy into
    allow-list mode. A global ``tools_allow`` is untouched and ordinary
    tools / external MCP servers still resolve."""
    doc = _make_agent_doc(tools=["pocketpaw_planner"])
    settings = Settings(
        anthropic_api_key="k",
        tool_profile="full",
        tools_allow=[],
    )
    policy = await _build_with(doc, settings)

    # Planner opted in.
    assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is True
    # Every ordinary tool still resolves — the policy is still allow-by-default.
    assert policy.is_tool_allowed("Bash") is True
    assert policy.is_tool_allowed("read_file") is True
    # External MCP servers are unaffected.
    assert policy.is_mcp_server_allowed("filesystem") is True
    assert policy.is_mcp_server_allowed("some-notion-server") is True


@pytest.mark.asyncio
async def test_deny_wins_over_planner_token():
    """A deny entry blocks the planner even when the token is in ``tools``."""
    doc = _make_agent_doc(tools=["pocketpaw_planner"])
    settings = Settings(
        anthropic_api_key="k",
        tools_deny=["mcp:pocketpaw_planner:*"],
    )
    policy = await _build_with(doc, settings)
    assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is False


@pytest.mark.asyncio
async def test_unknown_token_filtered_out():
    """An unrecognized token in ``tools`` is dropped — no crash, no opt-in."""
    doc = _make_agent_doc(tools=["not_a_real_mcp_server", "pocketpaw_planner"])
    policy = await _build_with(doc, Settings(anthropic_api_key="k"))
    # The known token still opts the planner in.
    assert policy.is_mcp_server_explicitly_allowed("pocketpaw_planner") is True
    # The unknown token does not opt anything in.
    assert policy.is_mcp_server_explicitly_allowed("not_a_real_mcp_server") is False


def test_claude_sdk_backend_is_the_real_class():
    """Guard: the planner opt-in keys off the real ``ClaudeSDKBackend``.

    If the backend module ever stops exporting it, the class-identity
    branch in ``_build`` would silently skip the policy plumbing.
    """
    assert ClaudeSDKBackend.__name__ == "ClaudeSDKBackend"


@pytest.mark.asyncio
async def test_pocket_planner_ambient_does_not_need_opt_in_token():
    """Sibling pin of ``test_no_tools_planner_off`` for the pocket-create
    planner. ``pocketpaw_pocket_planner`` is NOT in ``OPT_IN_MCP_SERVERS``
    — the bundled pocket-create skill calls it without the agent ever
    naming it in ``config.tools``. The pool builder must therefore NOT
    add it to ``mcp_servers_allow`` even if the agent does name it (an
    unknown token is dropped silently — same as any other non-opt-in
    server name).
    """
    # Agent with no tools — the ambient pocket planner must still be
    # reachable (the allow-by-default path), so ``is_mcp_server_allowed``
    # returns True. We assert by ABSENCE from ``mcp_servers_allow``: the
    # ambient server is intentionally not in OPT_IN_MCP_SERVERS so the
    # opt-in path doesn't even see it.
    doc = _make_agent_doc(tools=[])
    policy = await _build_with(doc, Settings(anthropic_api_key="k"))
    # Not opted in (because not in OPT_IN_MCP_SERVERS), but still reachable
    # via the allow-by-default channel.
    assert policy.is_mcp_server_explicitly_allowed("pocketpaw_pocket_planner") is False
    assert policy.is_mcp_server_allowed("pocketpaw_pocket_planner") is True

    # Even an agent that explicitly names the ambient server in `tools`
    # does not flip it into the opt-in set — only servers in
    # OPT_IN_MCP_SERVERS get translated.
    doc_with_token = _make_agent_doc(tools=["pocketpaw_pocket_planner"])
    policy_with_token = await _build_with(doc_with_token, Settings(anthropic_api_key="k"))
    assert policy_with_token.is_mcp_server_explicitly_allowed("pocketpaw_pocket_planner") is False
