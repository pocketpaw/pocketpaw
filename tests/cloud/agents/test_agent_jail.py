# tests/cloud/agents/test_agent_jail.py
# Created 2026-06-26 (ART-2) — locks the per-tenant agent cwd jail contract:
#   * two workspaces resolve to distinct, non-overlapping dirs (isolation)
#   * workspace + session -> <root>/<ws>/agent/<session>/
#   * workspace, no session -> <root>/<ws>/agent/_shared/ (group/DM bridge)
#   * same identity reuses one dir across turns
#   * cloud active + cloud-chat-run marker + no workspace -> RAISES (fail-closed)
#   * cloud active + NO marker + no workspace -> None (non-chat run falls back)
#   * cloud NOT active + no workspace -> None (OSS / dedicated unchanged)
#   * hostile / traversal ids are rejected before they touch the filesystem
#   * a trailing-newline id is rejected (the \Z-vs-$ anchor hardening)
#   * the fail-closed emits a high-severity cloud AuditEvent before it raises
#   * CloudAgentExtension.agent_cwd delegates to the resolver
# Updated 2026-06-27 (fix/cloud-artifacts-reland) — the fail-closed is now gated
# on the per-run mark_cloud_chat_run() marker, not is_multi_tenant_cloud() alone
# (a process-global). The fail-closed tests set the marker so a real cloud CHAT
# run still raises; a new test locks the no-marker -> None fallback (a non-chat
# run merely sharing a cloud-connected process must NOT hard-fail).
"""Per-tenant agent working-directory jail (ART-2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pocketpaw_ee.cloud import agent_jail
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    detach_agent_identity,
    mark_cloud_chat_run,
)


@pytest.fixture(autouse=True)
def _jail_root(tmp_path, monkeypatch):
    """Anchor the jail under tmp_path so tests never touch the real home dir."""
    root = tmp_path / "jail"
    monkeypatch.setenv("POCKETPAW_WORKSPACE_JAIL_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def _clean_identity():
    """Reset the identity ContextVars to None around each test.

    Guarantees the no-workspace cases see a clean slate regardless of any
    binding a prior test in the suite forgot to detach.
    """
    from pocketpaw_ee.cloud.chat import agent_service as svc

    tokens = (
        svc._active_workspace_id.set(None),
        svc._active_user_id.set(None),
        svc._active_session_mongo_id.set(None),
        svc._active_pocket_id.set(None),
    )
    try:
        yield
    finally:
        svc._active_workspace_id.reset(tokens[0])
        svc._active_user_id.reset(tokens[1])
        svc._active_session_mongo_id.reset(tokens[2])
        svc._active_pocket_id.reset(tokens[3])


@pytest.fixture
def cloud_active(monkeypatch):
    """Make get_client() report cloud mode active without a real Mongo client."""
    monkeypatch.setattr("pocketpaw_ee.cloud.shared.db._client", object())


@pytest.fixture
def cloud_inactive(monkeypatch):
    """Force get_client() to None (OSS / dedicated — no cloud DB)."""
    monkeypatch.setattr("pocketpaw_ee.cloud.shared.db._client", None)


def _bind(workspace_id: str, *, user_id: str = "u1", session_mongo_id: str | None = None):
    return attach_agent_identity(
        workspace_id=workspace_id,
        user_id=user_id,
        session_mongo_id=session_mongo_id,
    )


def test_two_workspaces_isolated(_jail_root):
    tok_a = _bind("wsAAA", session_mongo_id="sess1")
    try:
        cwd_a = Path(agent_jail.resolve_agent_cwd())
    finally:
        detach_agent_identity(tok_a)

    tok_b = _bind("wsBBB", session_mongo_id="sess1")
    try:
        cwd_b = Path(agent_jail.resolve_agent_cwd())
    finally:
        detach_agent_identity(tok_b)

    assert cwd_a != cwd_b
    assert "wsAAA" in cwd_a.parts and "wsBBB" not in cwd_a.parts
    assert "wsBBB" in cwd_b.parts and "wsAAA" not in cwd_b.parts
    # Both under the jail root; neither nested inside the other (no co-mingling).
    assert _jail_root in cwd_a.parents and _jail_root in cwd_b.parents
    assert not str(cwd_a).startswith(str(cwd_b) + "/")
    assert not str(cwd_b).startswith(str(cwd_a) + "/")
    assert cwd_a.is_dir() and cwd_b.is_dir()


def test_workspace_and_session_path(_jail_root):
    tok = _bind("ws1", session_mongo_id="sessX")
    try:
        cwd = Path(agent_jail.resolve_agent_cwd())
    finally:
        detach_agent_identity(tok)
    assert cwd == _jail_root / "ws1" / "agent" / "sessX"
    assert cwd.is_dir()


def test_workspace_without_session_uses_shared(_jail_root):
    tok = _bind("ws1", session_mongo_id=None)
    try:
        cwd = Path(agent_jail.resolve_agent_cwd())
    finally:
        detach_agent_identity(tok)
    assert cwd == _jail_root / "ws1" / "agent" / "_shared"
    assert cwd.is_dir()


def test_same_identity_reuses_dir(_jail_root):
    tok = _bind("ws1", session_mongo_id="sessX")
    try:
        first = agent_jail.resolve_agent_cwd()
        second = agent_jail.resolve_agent_cwd()
    finally:
        detach_agent_identity(tok)
    assert first == second


def test_fail_closed_when_cloud_active_and_no_workspace(cloud_active):
    # Cloud mode active + a live cloud CHAT run (marker set) + no identity bound
    # (contextvar default None) → the mis-tenanting fail-closed fires. The marker
    # is REQUIRED: without it the resolver falls back (see the next test), so a
    # real chat run that lost its workspace must set it to keep the protection.
    with pytest.raises(RuntimeError, match="no resolvable workspace"), mark_cloud_chat_run():
        agent_jail.resolve_agent_cwd()


def test_returns_none_when_cloud_active_but_not_chat_run(cloud_active):
    # Cloud mode active + no workspace BUT no cloud-chat-run marker — a direct
    # backend test / CLI / background job merely sharing a cloud-connected
    # process. The resolver MUST fall back to None, not hard-fail
    # (fix/cloud-artifacts-reland): is_multi_tenant_cloud() is a process-global,
    # so the fail-closed is gated on the per-run marker, which is absent here.
    assert agent_jail.resolve_agent_cwd() is None


def test_returns_none_when_not_cloud(cloud_inactive):
    # No identity bound + cloud DB never initialized → OSS / dedicated path.
    assert agent_jail.resolve_agent_cwd() is None


def test_hostile_workspace_id_rejected(_jail_root):
    tok = _bind("../etc", session_mongo_id="sess1")
    try:
        with pytest.raises(ValueError, match="unsafe workspace_id"):
            agent_jail.resolve_agent_cwd()
    finally:
        detach_agent_identity(tok)


def test_hostile_session_id_rejected(_jail_root):
    tok = _bind("ws1", session_mongo_id="../../root")
    try:
        with pytest.raises(ValueError, match="unsafe session_mongo_id"):
            agent_jail.resolve_agent_cwd()
    finally:
        detach_agent_identity(tok)


def test_trailing_newline_id_rejected(_jail_root):
    # ``$`` matches just before a trailing newline; the ``\Z`` anchor must
    # reject ``"ws1\n"`` so a newline can't slip past the traversal guard.
    tok = _bind("ws1\n", session_mongo_id="sess1")
    try:
        with pytest.raises(ValueError, match="unsafe workspace_id"):
            agent_jail.resolve_agent_cwd()
    finally:
        detach_agent_identity(tok)


def test_extension_delegates(_jail_root):
    from pocketpaw_ee.extensions import CloudAgentExtension

    tok = _bind("ws1", session_mongo_id="sessX")
    try:
        cwd = CloudAgentExtension().agent_cwd()
    finally:
        detach_agent_identity(tok)
    assert Path(cwd) == _jail_root / "ws1" / "agent" / "sessX"


@pytest.mark.asyncio
async def test_fail_closed_emits_high_severity_audit(cloud_active, monkeypatch):
    """The fail-closed (a cloud run that lost its workspace) emits a
    high-severity cloud AuditEvent before it raises — best-effort, fire-and-
    forget on the running loop."""
    import pocketpaw_ee.cloud.audit.service as audit_service

    calls: list[tuple] = []

    async def fake_record(workspace_id, actor_id, action, **kwargs):
        calls.append((workspace_id, actor_id, action, kwargs))

    monkeypatch.setattr(audit_service, "record", fake_record)

    from pocketpaw_ee.cloud.chat import agent_service as svc

    # Bind a user + session but NO workspace (the mis-tenanting condition),
    # under a live cloud CHAT run (marker set) so the fail-closed actually fires.
    utok = svc._active_user_id.set("user-1")
    stok = svc._active_session_mongo_id.set("sess-9")
    try:
        with pytest.raises(RuntimeError, match="no resolvable workspace"), mark_cloud_chat_run():
            agent_jail.resolve_agent_cwd()
        # The emit is scheduled on the loop; give it turns to run.
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        svc._active_user_id.reset(utok)
        svc._active_session_mongo_id.reset(stok)

    assert calls, "expected a fail-closed audit alert to be recorded"
    workspace_id, actor_id, action, kwargs = calls[0]
    assert action == "agent.cwd_jail.fail_closed"
    assert actor_id == "user-1"
    assert kwargs["target_id"] == "sess-9"
    assert kwargs["metadata"]["severity"] == "high"
