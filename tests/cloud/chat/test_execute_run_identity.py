# tests/cloud/chat/test_execute_run_identity.py
#
# Created: 2026-06-25 (fix/worker-trusts-spec-workspace) — reproduction +
# regression guard for the cloud-chat-worker tenancy bug: a run that calls
# create_landing_site / create_svelte_site failed on the deployed backend with
# "requires workspace and user context (call from a cloud chat session)".
#
# Root cause: execute_run re-derives tenancy from the Mongo scope DOC via
# resolve_scope_context and threw away the authenticated, non-empty
# spec.workspace_id. When the doc's ``workspace`` field was empty/missing, the
# resolved ScopeContext carried workspace_id="", attach_agent_identity .set() an
# empty contextvar, and the in-process MCP tool's _identity() guard raised.
#
# These tests drive the REAL identity path — resolve_scope_context(...,
# expected_workspace_id=spec.workspace_id) → attach_agent_identity(
# workspace_id=ctx.workspace_id, ...) → current_workspace_id() — NOT the
# scripts/test_svelte_create.py harness shortcut (which hardcodes the
# workspace_id straight into attach_agent_identity and bypasses the resolver).
# The fix must (1) fall back to the trusted spec workspace when the doc's is
# empty, (2) reject a spec that DISAGREES with a non-empty doc workspace
# (cross-tenant guard), and (3) make attach_agent_identity reject empty ids.
#
# Updated: 2026-08-01 (Sense Phase 2, SP2-1) — added the agent-identity block at
# the bottom of this module: attach binds the running agent's id, an in-process
# MCP-tool-shaped callee reads it back through current_agent_id() (including from
# a spawned task, the shape an SDK tool call actually runs in), an unbound caller
# still sees None, and detach leaves NO residue. The leak guard is the point: a
# stale agent id surviving a detach is the same class of bug as the warm-client
# role bleed, where one caller's identity froze onto a later caller's turn.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    current_agent_id,
    current_user_id,
    current_workspace_id,
    detach_agent_identity,
    resolve_scope_context,
)
from pocketpaw_ee.cloud.shared.errors import CloudError

# The authoritative, authenticated tenancy the HTTP route stamps onto the
# RunSpec (current_workspace_id dependency raises 400 if empty, so this is
# always non-empty by the time the worker picks the spec up off Redis).
SPEC_WORKSPACE = "w_trusted"
SPEC_USER = "u_caller"


def _session_doc(*, workspace: str) -> SimpleNamespace:
    """A session scope doc with a controllable ``workspace`` field. The bug is
    triggered when ``workspace`` is "" (or missing)."""
    from pocketpaw_ee.cloud.models.session import Session

    return Session.model_construct(
        id="s_sites",
        sessionId="ws_sites",
        workspace=workspace,
        owner=SPEC_USER,
        agent="agent_pp",
        pocket=None,
        deleted_at=None,
    )


def _pocket_doc(*, workspace: str) -> SimpleNamespace:
    """A pocket scope doc with a controllable ``workspace`` field."""
    return SimpleNamespace(
        id="p_site",
        type="custom",
        workspace=workspace,
        owner=SPEC_USER,
        team=[SPEC_USER],
        agents=["agent_pp"],
        tool_specs=[],
        visibility="workspace",
        shared_with=[],
    )


# ===========================================================================
# RED — the bug. A scope doc with an EMPTY workspace, a spec carrying a valid
# non-empty workspace_id: the active identity must end up populated FROM THE
# TRUSTED SPEC, not blanked out from the doc.
# ===========================================================================


@pytest.mark.asyncio
async def test_session_empty_doc_workspace_falls_back_to_spec():
    """The decisive reproduction: a session doc whose ``workspace`` is empty,
    resolved with the trusted spec workspace, must yield a ScopeContext whose
    workspace_id is the spec's — so the MCP tool's identity guard passes."""
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_session",
        AsyncMock(return_value=_session_doc(workspace="")),
    ):
        ctx = await resolve_scope_context(
            scope="session",
            scope_id="s_sites",
            user_id=SPEC_USER,
            agent_id_hint=None,
            expected_workspace_id=SPEC_WORKSPACE,
        )
    assert ctx.workspace_id == SPEC_WORKSPACE

    # And the contextvar the MCP tool reads ends up populated, not "".
    tokens = attach_agent_identity(workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    try:
        assert current_workspace_id() == SPEC_WORKSPACE
        assert current_user_id() == SPEC_USER
    finally:
        detach_agent_identity(tokens)


@pytest.mark.asyncio
async def test_pocket_empty_doc_workspace_falls_back_to_spec():
    """Same fallback for a pocket-scope doc with an empty workspace."""
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_pocket",
        AsyncMock(return_value=_pocket_doc(workspace="")),
    ):
        ctx = await resolve_scope_context(
            scope="pocket",
            scope_id="p_site",
            user_id=SPEC_USER,
            agent_id_hint=None,
            expected_workspace_id=SPEC_WORKSPACE,
        )
    assert ctx.workspace_id == SPEC_WORKSPACE


# ===========================================================================
# Tenancy safety — the spec must NOT be allowed to spoof a different workspace
# than a doc that DOES carry one.
# ===========================================================================


@pytest.mark.asyncio
async def test_session_doc_workspace_authoritative_when_present():
    """A non-empty doc workspace stays authoritative when the spec AGREES."""
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_session",
        AsyncMock(return_value=_session_doc(workspace=SPEC_WORKSPACE)),
    ):
        ctx = await resolve_scope_context(
            scope="session",
            scope_id="s_sites",
            user_id=SPEC_USER,
            agent_id_hint=None,
            expected_workspace_id=SPEC_WORKSPACE,
        )
    assert ctx.workspace_id == SPEC_WORKSPACE


@pytest.mark.asyncio
async def test_session_spec_disagreeing_with_doc_raises():
    """Cross-tenant guard: a spec whose workspace DISAGREES with a non-empty
    doc workspace must raise, never silently trust the mismatched spec."""
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_session",
        AsyncMock(return_value=_session_doc(workspace="w_real")),
    ):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="session",
                scope_id="s_sites",
                user_id=SPEC_USER,
                agent_id_hint=None,
                expected_workspace_id="w_attacker",
            )


@pytest.mark.asyncio
async def test_pocket_spec_disagreeing_with_doc_raises():
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_pocket",
        AsyncMock(return_value=_pocket_doc(workspace="w_real")),
    ):
        with pytest.raises(CloudError):
            await resolve_scope_context(
                scope="pocket",
                scope_id="p_site",
                user_id=SPEC_USER,
                agent_id_hint=None,
                expected_workspace_id="w_attacker",
            )


# ===========================================================================
# Backward compatibility — callers that DON'T thread expected_workspace_id
# (legacy / non-worker paths) keep today's behavior.
# ===========================================================================


@pytest.mark.asyncio
async def test_no_expected_workspace_keeps_doc_workspace():
    """Without expected_workspace_id the resolver behaves exactly as before:
    the doc's workspace (here non-empty) is used unchanged."""
    with patch(
        "pocketpaw_ee.cloud.chat.agent_service._get_session",
        AsyncMock(return_value=_session_doc(workspace="w_doc")),
    ):
        ctx = await resolve_scope_context(
            scope="session",
            scope_id="s_sites",
            user_id=SPEC_USER,
            agent_id_hint=None,
        )
    assert ctx.workspace_id == "w_doc"


# ===========================================================================
# Defense-in-depth — attach_agent_identity must REJECT empty tenancy so any
# future caller that loses workspace/user fails loudly at the seam instead of
# blanking the contextvar and surfacing deep inside an MCP tool.
# ===========================================================================


def test_attach_agent_identity_rejects_empty_workspace():
    with pytest.raises(ValueError):
        attach_agent_identity(workspace_id="", user_id=SPEC_USER)


def test_attach_agent_identity_rejects_empty_user():
    with pytest.raises(ValueError):
        attach_agent_identity(workspace_id=SPEC_WORKSPACE, user_id="")


# ===========================================================================
# SP2-1 — the running agent's id reaches the in-process MCP layer.
#
# An in-process MCP tool has no FastAPI request scope; it resolves who it is
# running for off the identity ContextVars (the same way the fabric / connector
# servers resolve workspace and pocket). These tests drive the real attach seam
# and read the id back from a callee shaped like one of those tools.
# ===========================================================================

RUNNING_AGENT = "agent_pp"


def _mcp_tool_reads_running_agent() -> str | None:
    """Stand-in for an in-process MCP tool handler: no arguments, no request
    scope, resolves the running agent purely from the ambient identity."""
    return current_agent_id()


def test_attach_binds_the_running_agent_for_mcp_tools():
    tokens = attach_agent_identity(
        workspace_id=SPEC_WORKSPACE, user_id=SPEC_USER, agent_id=RUNNING_AGENT
    )
    try:
        assert _mcp_tool_reads_running_agent() == RUNNING_AGENT
    finally:
        detach_agent_identity(tokens)


def test_agent_id_is_none_when_the_caller_binds_none():
    """Every pre-SP2-1 caller omits ``agent_id``; they must keep binding None."""
    tokens = attach_agent_identity(workspace_id=SPEC_WORKSPACE, user_id=SPEC_USER)
    try:
        assert _mcp_tool_reads_running_agent() is None
        # The rest of the identity is untouched by the wider tuple.
        assert current_workspace_id() == SPEC_WORKSPACE
        assert current_user_id() == SPEC_USER
    finally:
        detach_agent_identity(tokens)


def test_detach_leaves_no_agent_id_behind():
    """Leak guard: an agent id that survives its own run would bleed onto the
    next caller in the same context — the failure mode the warm-client role
    bleed already cost us once."""
    assert current_agent_id() is None
    tokens = attach_agent_identity(
        workspace_id=SPEC_WORKSPACE, user_id=SPEC_USER, agent_id=RUNNING_AGENT
    )
    assert current_agent_id() == RUNNING_AGENT
    detach_agent_identity(tokens)
    assert current_agent_id() is None


def test_nested_binds_restore_the_outer_agent():
    """A second agent bound inside the first's window must not outlive it."""
    outer = attach_agent_identity(
        workspace_id=SPEC_WORKSPACE, user_id=SPEC_USER, agent_id="agent_outer"
    )
    try:
        inner = attach_agent_identity(
            workspace_id=SPEC_WORKSPACE, user_id=SPEC_USER, agent_id="agent_inner"
        )
        assert current_agent_id() == "agent_inner"
        detach_agent_identity(inner)
        assert current_agent_id() == "agent_outer"
    finally:
        detach_agent_identity(outer)
    assert current_agent_id() is None


@pytest.mark.asyncio
async def test_agent_id_propagates_into_a_spawned_task():
    """MCP tool calls run inside tasks the SDK spawns under the run, so the
    binding has to survive ``create_task``'s context copy — the same property
    workspace/pocket rely on."""

    async def _tool_call() -> str | None:
        await asyncio.sleep(0)
        return _mcp_tool_reads_running_agent()

    tokens = attach_agent_identity(
        workspace_id=SPEC_WORKSPACE, user_id=SPEC_USER, agent_id=RUNNING_AGENT
    )
    try:
        assert await asyncio.create_task(_tool_call()) == RUNNING_AGENT
    finally:
        detach_agent_identity(tokens)
