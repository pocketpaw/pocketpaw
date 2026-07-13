# tests/cloud/workspace/test_retention.py — Tests for the per-workspace data
# retention setting + enforcement (compliance-starter).
# Created: 2026-07-10 — covers:
#   * retention_days round-trips through the dedicated get/set path AND the
#     general update() settings merge WITHOUT clobbering sibling settings
#     (regression guard for the destructive full-replace recon flagged).
#   * WorkspaceSettings rejects a non-positive retention_days.
#   * enforce_retention() purges ONLY audit rows older than the cutoff for
#     the RIGHT workspace, and is a no-op when retention is unset.
#   * purge_workspace_audit() is age- + tenant-scoped.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.models.audit_event import AuditEvent as _AuditEventDoc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.models.workspace import WorkspaceSettings
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import (
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture(autouse=True)
def _resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the realtime resolver so ``get_resolver()`` doesn't explode
    (the real one needs ``init_realtime()`` which unit tests don't run)."""
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


def _ctx(user_id: str, workspace_id: str | None = None) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_user(email: str = "owner@x.c") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="U",
        workspaces=[],
    )
    await doc.insert()
    return doc


async def _create_ws(owner: _UserDoc):
    return await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="A", slug="a")
    )


# ---------------------------------------------------------------------------
# retention_days round-trips + no sibling clobber
# ---------------------------------------------------------------------------


async def test_set_retention_round_trips() -> None:
    owner = await _seed_user()
    ws = await _create_ws(owner)

    assert await workspace_service.get_retention(_ctx(str(owner.id)), ws.id) is None

    await workspace_service.set_retention(_ctx(str(owner.id)), ws.id, 30)
    assert await workspace_service.get_retention(_ctx(str(owner.id)), ws.id) == 30

    # None clears the policy (keep forever).
    await workspace_service.set_retention(_ctx(str(owner.id)), ws.id, None)
    assert await workspace_service.get_retention(_ctx(str(owner.id)), ws.id) is None


async def test_set_retention_does_not_clobber_sibling_settings() -> None:
    owner = await _seed_user()
    ws = await _create_ws(owner)

    # Seed sibling settings via the general update path.
    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(settings={"default_agent": "agent-1", "allow_invites": False}),
    )
    # Now set retention through the dedicated path.
    await workspace_service.set_retention(_ctx(str(owner.id)), ws.id, 45)

    doc = await _WorkspaceDoc.get(ws.id)
    assert doc.settings.retention_days == 45
    assert doc.settings.default_agent == "agent-1"  # sibling preserved
    assert doc.settings.allow_invites is False  # sibling preserved


async def test_update_settings_merges_and_does_not_clobber() -> None:
    """The general PATCH settings path must MERGE, not full-replace.

    Regression guard for the destructive ``doc.settings =
    WorkspaceSettings(**body.settings)`` the recon flagged.
    """
    owner = await _seed_user()
    ws = await _create_ws(owner)

    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(settings={"default_agent": "agent-1"}),
    )
    # A partial settings patch that only carries retention_days must NOT
    # wipe default_agent.
    await workspace_service.update(
        _ctx(str(owner.id)),
        ws.id,
        UpdateWorkspaceRequest(settings={"retention_days": 30}),
    )

    doc = await _WorkspaceDoc.get(ws.id)
    assert doc.settings.retention_days == 30
    assert doc.settings.default_agent == "agent-1"  # NOT clobbered


def test_workspace_settings_rejects_non_positive_retention() -> None:
    with pytest.raises(Exception):
        WorkspaceSettings(retention_days=0)
    with pytest.raises(Exception):
        WorkspaceSettings(retention_days=-5)
    # None and positive are fine.
    assert WorkspaceSettings(retention_days=None).retention_days is None
    assert WorkspaceSettings(retention_days=1).retention_days == 1


async def test_set_retention_rejects_non_positive() -> None:
    owner = await _seed_user()
    ws = await _create_ws(owner)
    with pytest.raises(ValidationError):
        await workspace_service.set_retention(_ctx(str(owner.id)), ws.id, 0)


# ---------------------------------------------------------------------------
# Enforcement — purge_workspace_audit + enforce_retention
# ---------------------------------------------------------------------------


async def _seed_audit(
    workspace_id: str, *, at: datetime, action: str = "workspace.updated"
) -> None:
    doc = _AuditEventDoc(
        workspace=workspace_id,
        actor_id="u1",
        action=action,
        target_type="workspace",
        target_id=workspace_id,
        metadata={},
        at=at,
    )
    await doc.insert()


# NOTE ON AGES: the AuditEvent model carries a 365-day TTL index. Real Mongo
# expires TTL rows via a lazy background reaper (never on insert), so a
# 400-day row is insertable and the retention purge (e.g. 30 days) deletes it.
# mongomock, however, applies the TTL at query time — a >365-day seed row just
# vanishes, masking the purge. So the "old" seed rows below sit comfortably
# UNDER 365 days but OVER the retention cutoff, which exercises the purge
# cleanly in-memory. Assertions are tz-agnostic (mongomock stores ``at`` naive)
# — we tag rows by ``action`` rather than compare mixed-tz datetimes.


async def test_purge_workspace_audit_deletes_only_old_and_right_tenant() -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=100)
    recent = now - timedelta(days=1)

    await _seed_audit("ws-A", at=old, action="old.row")
    await _seed_audit("ws-A", at=recent, action="recent.row")
    await _seed_audit("ws-B", at=old, action="old.row")  # other tenant — must survive

    cutoff = now - timedelta(days=30)
    deleted = await audit_service.purge_workspace_audit("ws-A", cutoff)

    assert deleted == 1
    remaining_a = await _AuditEventDoc.find({"workspace": "ws-A"}).to_list()
    assert [r.action for r in remaining_a] == ["recent.row"]  # only the old row went
    # Other tenant's old row untouched despite the same age.
    remaining_b = await _AuditEventDoc.find({"workspace": "ws-B"}).to_list()
    assert len(remaining_b) == 1


async def test_enforce_retention_purges_old_audit() -> None:
    owner = await _seed_user()
    ws = await _create_ws(owner)
    now = datetime.now(UTC)

    await _seed_audit(ws.id, at=now - timedelta(days=100), action="old.row")
    await _seed_audit(ws.id, at=now - timedelta(days=5), action="recent.row")

    await workspace_service.set_retention(_ctx(str(owner.id)), ws.id, 30)
    result = await workspace_service.enforce_retention(ws.id)

    assert result["audit_deleted"] == 1
    actions = [r.action for r in await _AuditEventDoc.find({"workspace": ws.id}).to_list()]
    assert "old.row" not in actions  # the 100-day row was purged
    assert "recent.row" in actions  # the 5-day row survived


async def test_enforce_retention_noop_when_unset() -> None:
    owner = await _seed_user()
    ws = await _create_ws(owner)
    now = datetime.now(UTC)
    await _seed_audit(ws.id, at=now - timedelta(days=100), action="old.row")

    result = await workspace_service.enforce_retention(ws.id)
    assert result["audit_deleted"] == 0
    assert result.get("skipped") == "no_retention_policy"
    # The old row is untouched because retention is unset (keep forever).
    actions = [r.action for r in await _AuditEventDoc.find({"workspace": ws.id}).to_list()]
    assert "old.row" in actions
