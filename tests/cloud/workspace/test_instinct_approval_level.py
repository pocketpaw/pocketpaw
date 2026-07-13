# tests/cloud/workspace/test_instinct_approval_level.py
# Created: 2026-06-19 (feat/instinct-gate-integration, security-review FIX 1) —
# coverage for the OWNER-ONLY activation route that turns the layered Instinct
# gate's triager on for a workspace (`PATCH /workspaces/{id}/instinct/
# approval-level`). Enabling a non-ASK level activates AUTO-APPROVAL of agent
# WRITE actions, so this is the single most security-sensitive write in the
# gate: it MUST be the most-restrictive guard, validate the level against the
# `ApprovalLevel` enum, audit the change at WARNING severity, and round-trip
# through `resolve_workspace_approval_level`.
#
# Pinned here (service-level — the Beanie writer):
#   * owner CAN set a valid level → persisted, audited (WARNING, old→new),
#     and read back by `resolve_workspace_approval_level`
#   * an invalid level (not ASK/TRIAGE/TRUSTED) → ValidationError (422)
#   * the route is registered at the OWNER-only RBAC action `instinct.activate`
#     (the strongest workspace guard) — a non-owner is denied
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import CreateWorkspaceRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture(autouse=True)
def _resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub the realtime resolver so create()/save() don't need init_realtime."""
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


async def _seed_user(email: str = "owner@x.c") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Owner",
        workspaces=[],
    )
    await doc.insert()
    return doc


def _ctx(user_id: str) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=None,
        request_id="test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _make_workspace() -> tuple[str, str]:
    """Seed an owner User + a workspace. Returns (workspace_id, owner_user_id)."""
    owner = await _seed_user()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )
    return ws.id, str(owner.id)


# ---------------------------------------------------------------------------
# Happy path — owner sets a valid level, it is audited + read back.
# ---------------------------------------------------------------------------


async def test_owner_sets_valid_level_persists_and_reads_back() -> None:
    """A valid level is written through the service and resolves on read."""
    ws_id, owner_id = await _make_workspace()

    # Default before any set — global "ASK".
    assert await pockets_service.resolve_workspace_approval_level(ws_id) == "ASK"

    await workspace_service.set_instinct_approval_level(_ctx(owner_id), ws_id, "TRIAGE")

    # Read back via the canonical resolver the gate uses.
    assert await pockets_service.resolve_workspace_approval_level(ws_id) == "TRIAGE"


async def test_set_level_emits_warning_audit_old_to_new(monkeypatch) -> None:
    """The activation switch emits an append-only WARNING audit event carrying
    actor, workspace_id, and the old→new level — this is the switch that
    enables auto-approval of agent writes, so it must be loud and attributable."""
    captured: list[object] = []

    from pocketpaw.security import audit as audit_mod

    class _FakeLogger:
        def log(self, event):
            captured.append(event)

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: _FakeLogger())

    ws_id, owner_id = await _make_workspace()
    await workspace_service.set_instinct_approval_level(_ctx(owner_id), ws_id, "TRIAGE")

    activation = [e for e in captured if getattr(e, "action", "") == "instinct.approval_level.set"]
    assert activation, f"no activation audit event found in {captured}"
    ev = activation[0]
    assert str(getattr(ev, "severity", "")).lower().endswith("warning")
    ctx = getattr(ev, "context", {}) or {}
    assert ctx.get("workspace_id") == ws_id
    assert ctx.get("old_level") == "ASK"
    assert ctx.get("new_level") == "TRIAGE"
    assert getattr(ev, "actor", "") == owner_id


@pytest.mark.parametrize("level", ["TRUSTED", "ASK"])
async def test_owner_sets_other_valid_levels(level: str) -> None:
    """All three enum values are accepted by the service."""
    ws_id, owner_id = await _make_workspace()
    await workspace_service.set_instinct_approval_level(_ctx(owner_id), ws_id, level)
    assert await pockets_service.resolve_workspace_approval_level(ws_id) == level


# ---------------------------------------------------------------------------
# Validation — an out-of-enum value is rejected (maps to 422 at the boundary).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["ask", "YOLO", "", "auto", "trusted "])
async def test_invalid_level_rejected(bad: str) -> None:
    """A value not in the ApprovalLevel enum raises ValidationError (422).

    The level must NEVER be coerced to a fallback here — an invalid set must
    fail loudly, not silently park a typo on the document (which the gate
    would then read and route ASK on, masking the misconfiguration)."""
    ws_id, owner_id = await _make_workspace()
    with pytest.raises(ValidationError):
        await workspace_service.set_instinct_approval_level(_ctx(owner_id), ws_id, bad)
    # And the stored value is unchanged (still the ASK default).
    assert await pockets_service.resolve_workspace_approval_level(ws_id) == "ASK"


async def test_unknown_workspace_raises_not_found() -> None:
    """Setting the level on a missing workspace is a 404, not a silent no-op."""
    from pocketpaw_ee.cloud._core.errors import NotFound

    with pytest.raises(NotFound):
        await workspace_service.set_instinct_approval_level(
            _ctx("000000000000000000000001"), "000000000000000000000000", "TRIAGE"
        )


# ---------------------------------------------------------------------------
# Authz — the route is registered at the OWNER-only RBAC action.
# ---------------------------------------------------------------------------


def test_activation_action_is_owner_only() -> None:
    """The dedicated RBAC action gating the route is OWNER-level — the most
    restrictive workspace guard. Auto-approval of agent writes must never be
    flippable by a mere admin or member."""
    from pocketpaw_ee.guards.actions import ACTIONS
    from pocketpaw_ee.guards.rbac import WorkspaceRole

    rule = ACTIONS["instinct.activate"]
    assert rule.minimum == WorkspaceRole.OWNER


def test_activation_route_uses_owner_guard() -> None:
    """The PATCH approval-level route on the workspace router is guarded by
    `require_action('instinct.activate')` — pinned structurally so a future
    refactor can't silently downgrade the guard to a weaker action."""
    from pocketpaw_ee.cloud.workspace import router as ws_router

    route = next(
        r
        for r in ws_router.router.routes
        if getattr(r, "path", "").endswith("/instinct/approval-level")
        and "PATCH" in getattr(r, "methods", set())
    )
    guard_names = [getattr(d.call, "__name__", "") for d in route.dependant.dependencies]
    assert any("instinct_activate" in n for n in guard_names), guard_names
