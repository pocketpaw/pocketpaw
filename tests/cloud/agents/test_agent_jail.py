# tests/cloud/agents/test_agent_jail.py
# Created 2026-06-26 (ART-2) — locks the per-tenant agent cwd jail contract:
#   * two workspaces resolve to distinct, non-overlapping dirs (isolation)
#   * workspace + session -> <root>/<ws>/agent/<session>/
#   * workspace, no session -> <root>/<ws>/agent/_shared/ (group/DM bridge)
#   * same identity reuses one dir across turns
#   * cloud active + no workspace -> RAISES (fail-closed)
#   * cloud NOT active + no workspace -> None (OSS / dedicated unchanged)
#   * hostile / traversal ids are rejected before they touch the filesystem
#   * CloudAgentExtension.agent_cwd delegates to the resolver
"""Per-tenant agent working-directory jail (ART-2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud import agent_jail
from pocketpaw_ee.cloud.chat.agent_service import (
    attach_agent_identity,
    detach_agent_identity,
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
    # No identity bound (contextvar default None) + cloud mode active.
    with pytest.raises(RuntimeError, match="no resolvable workspace"):
        agent_jail.resolve_agent_cwd()


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


def test_extension_delegates(_jail_root):
    from pocketpaw_ee.extensions import CloudAgentExtension

    tok = _bind("ws1", session_mongo_id="sessX")
    try:
        cwd = CloudAgentExtension().agent_cwd()
    finally:
        detach_agent_identity(tok)
    assert Path(cwd) == _jail_root / "ws1" / "agent" / "sessX"
