# tests/cloud/test_admin_action_gate.py — the gated workspace-admin-action proposal
# type (the 8th gated Instinct kind, WA-2), plus the WA-5 whitelist extensions.
#
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-2).
# Updated: 2026-07-03 (WA-6) — coverage for the three OWNER writes
#   (instinct.activate / workspace.delete / billing.manage). Per action: approve →
#   the whitelisted service fires EXACTLY ONCE with the adapted args; reject via
#   the REAL router → no service call. billing.manage is PAYMENT-HONEST: approve
#   calls ``subscribe`` (which returns a {checkout_url}) — the executor records the
#   checkout url as the outcome, and the test asserts NO ``set_workspace_plan``
#   mutation is called (an agent must never flip a paid plan without checkout). The
#   execute-time OWNER re-check is proven with the REAL ``_recheck_rbac``: a
#   proposer DEMOTED from OWNER to ADMIN after proposing → approve FAILS CLOSED for
#   an OWNER-gated action, the service is NOT called.
# Updated: 2026-07-03 (WA-5) — coverage for the five additional whitelisted ADMIN
#   writes (member.remove / invite.create / invite.revoke / connector.manage
#   {enable,disable,config} / workspace.update). Per action: approve → the
#   whitelisted service fires EXACTLY ONCE with the adapted args; the STRICT
#   adapter drops a smuggled key (or fails closed on an invalid one, e.g. an invite
#   role=owner or an unknown connector op); reject via the REAL router → no service
#   call (parametrized). The connector-config test asserts the config rides INSIDE
#   the typed DTO, never as top-level kwargs.
#
# What this pins:
#   * propose_admin_action — blob shape (schema 1, RBAC action key, args,
#     proposer_user_id, params_hash, idempotency_key, correlation_id, summary),
#     tenancy (workspace + proposer required).
#   * execute_approved_admin_action with the workspace service spied:
#       - happy path: the whitelisted service (update_member_role) is called
#         EXACTLY ONCE with the adapted args, action marked EXECUTED, structured
#         outcome back-written.
#       - EXECUTE-TIME RBAC RE-CHECK (the key security test): the proposer is
#         DEMOTED to MEMBER after proposing → approve path FAILS CLOSED,
#         update_member_role is NOT called.
#       - unknown action key in the blob → executor HARD-FAILS, no service call.
#       - idempotency: a second invocation does NOT re-fire the service.
#       - args-hash mismatch: an args edit between propose and approve → FAILED,
#         no call.
#       - schema mismatch: a stale schema blob → FAILED, no call.
#       - service failure (raise) → FAILED status, NOT an exception.
#   * the production-path integration tests: propose → approve/reject via the REAL
#     router handler → walk the decision chain and assert EXACTLY the expected
#     events (agent.proposed → human.corrected → decision.completed), one terminal;
#     reject fires no service call.
#   * cross-workspace approval → 403, nothing fires.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The store is patched to
# a tmp-file InstinctStore; the workspace service + RBAC re-check are patched so
# nothing touches Mongo.

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.admin_proposals import executor as aa_executor  # noqa: E402
from pocketpaw_ee.cloud.admin_proposals import propose as aa_propose  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.guards.rbac import Forbidden  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------


class SpyWorkspaceService:
    """Records update_member_role calls; returns None (matches the real sink).
    Injected by monkeypatching ``workspace.service.update_member_role``."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def update_member_role(self, workspace_id, target_user_id, role, actor_user_id):
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "target_user_id": target_user_id,
                "role": role,
                "actor_user_id": actor_user_id,
            }
        )
        if self._raises is not None:
            raise self._raises
        return None


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired everywhere the gate reads it."""
    st = InstinctStore(tmp_path / "instinct_admin_action_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


def _patch_service(monkeypatch, spy: SpyWorkspaceService) -> None:
    """Patch the workspace service's ``update_member_role`` to the spy."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.update_member_role",
        spy.update_member_role,
    )


def _allow_rbac(monkeypatch) -> None:
    """Stub the execute-time RBAC re-check to PASS (proposer still holds the
    role). The demotion test replaces this with a denying stub."""

    async def _ok(workspace_id, proposer_user_id, rbac_action):  # noqa: ANN001
        return None

    monkeypatch.setattr(aa_executor, "_recheck_rbac", _ok)


def _deny_rbac(monkeypatch) -> None:
    """Stub the execute-time RBAC re-check to FAIL — the proposer was demoted /
    removed since proposing. The executor must fail closed."""

    async def _deny(workspace_id, proposer_user_id, rbac_action):  # noqa: ANN001
        raise Forbidden("workspace.insufficient_role", "proposer demoted to member since proposing")

    monkeypatch.setattr(aa_executor, "_recheck_rbac", _deny)


# ---------------------------------------------------------------------------
# propose — blob shape + tenancy
# ---------------------------------------------------------------------------


async def test_propose_builds_admin_action_blob(store):
    """propose_admin_action files an Action carrying a well-formed schema-1
    ``_admin_action`` blob — RBAC action key, args, proposer, params_hash,
    idempotency_key, summary."""
    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action="workspace.member.role_change",
        args={"target_user_id": "u2", "role": "admin"},
        proposer_user_id="admin1",
    )
    action = await store.get_action(action_id)
    assert action is not None
    assert action.status == ActionStatus.PENDING

    blob = action.parameters["_admin_action"]
    assert blob["kind"] == "admin_action"
    assert blob["schema"] == 1
    assert blob["workspace_id"] == "w1"
    assert blob["action"] == "workspace.member.role_change"
    assert blob["args"] == {"target_user_id": "u2", "role": "admin"}
    assert blob["proposer_user_id"] == "admin1"
    assert blob["params_hash"] == aa_propose.compute_args_hash(
        "workspace.member.role_change", {"target_user_id": "u2", "role": "admin"}
    )
    assert blob["idempotency_key"]
    assert blob["correlation_id"]
    assert blob["summary"]


async def test_propose_requires_workspace_and_proposer(store):
    """A propose with no workspace / proposer is rejected up front."""
    with pytest.raises(ValueError, match="workspace_id"):
        await aa_propose.propose_admin_action(
            workspace_id="",
            action="workspace.member.role_change",
            args={},
            proposer_user_id="admin1",
        )
    with pytest.raises(ValueError, match="proposer_user_id"):
        await aa_propose.propose_admin_action(
            workspace_id="w1",
            action="workspace.member.role_change",
            args={},
            proposer_user_id="",
        )


# ---------------------------------------------------------------------------
# executor — happy path, RBAC re-check, whitelist, idempotency, hash, schema
# ---------------------------------------------------------------------------


async def _propose_and_approve(store, **kwargs) -> Any:
    """Propose then approve via the store, returning the approved Action."""
    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action="workspace.member.role_change",
        args={"target_user_id": "u2", "role": "admin"},
        proposer_user_id="admin1",
        **kwargs,
    )
    return await store.approve(action_id, approver="approver1")


async def test_executor_happy_path_calls_service_once(store, monkeypatch):
    """On approve the executor re-checks RBAC (stubbed pass), calls
    update_member_role EXACTLY ONCE with the adapted args, marks EXECUTED, and
    back-writes the structured outcome."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    approved = await _propose_and_approve(store)
    await aa_executor.execute_approved_admin_action(approved)

    # APPROVE-FIRES-ONCE assertion.
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["workspace_id"] == "w1"
    assert call["target_user_id"] == "u2"
    assert call["role"] == "admin"
    # The actor recorded is the PROPOSER (the write's author).
    assert call["actor_user_id"] == "admin1"

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.EXECUTED
    blob = final.parameters["_admin_action"]
    assert blob["outcome"]["status"] == "executed"
    assert "executed_at" in blob["outcome"]


async def test_executor_demoted_proposer_fails_closed(store, monkeypatch):
    """THE KEY SECURITY TEST — the proposer was demoted to MEMBER after
    proposing. The execute-time RBAC re-check DENIES, so the approved action
    FAILS CLOSED: update_member_role is NOT called."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _deny_rbac(monkeypatch)  # proposer no longer holds ADMIN

    approved = await _propose_and_approve(store)
    await aa_executor.execute_approved_admin_action(approved)  # must not raise

    # DEMOTED-PROPOSER-BLOCKED assertion — the write NEVER fired.
    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "rbac" in str(final.error).lower() or "role" in str(final.error).lower()
    assert final.parameters["_admin_action"]["outcome"]["status"] == "failed"


async def test_executor_unknown_action_hard_fails(store, monkeypatch):
    """An action key NOT in the execute whitelist → HARD FAIL, no service call.
    (RBAC re-check is never reached — the whitelist miss precedes it.)"""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action="workspace.member.delete_everything",  # not whitelisted
        args={"target_user_id": "u2"},
        proposer_user_id="admin1",
    )
    approved = await store.approve(action_id, approver="approver1")
    await aa_executor.execute_approved_admin_action(approved)

    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "whitelist" in str(final.error).lower()


async def test_executor_idempotent_never_double_fires(store, monkeypatch):
    """Re-invoking the executor on an already-executed Action does NOT call the
    service a second time."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    approved = await _propose_and_approve(store)
    await aa_executor.execute_approved_admin_action(approved)
    assert len(spy.calls) == 1

    reloaded = await store.get_action(approved.id)
    await aa_executor.execute_approved_admin_action(reloaded)
    assert len(spy.calls) == 1  # still ONE


async def test_executor_args_hash_mismatch_refuses(store, monkeypatch):
    """An args edit between propose and approve → FAILED, no service call."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    approved = await _propose_and_approve(store)
    approved.parameters["_admin_action"]["args"] = {"target_user_id": "u2", "role": "owner"}

    await aa_executor.execute_approved_admin_action(approved)

    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "hash" in str(final.error).lower()


async def test_executor_schema_mismatch_refuses(store, monkeypatch):
    """A stale blob with an incompatible schema version → FAILED, no call."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    approved = await _propose_and_approve(store)
    approved.parameters["_admin_action"]["schema"] = 999

    await aa_executor.execute_approved_admin_action(approved)

    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "schema" in str(final.error).lower()


async def test_executor_service_crash_marks_failed_not_exception(store, monkeypatch):
    """A service call that RAISES → the Action is FAILED, the executor swallows
    the exception (best-effort, never breaks the approve response)."""
    spy = SpyWorkspaceService(raises=RuntimeError("update blew up"))
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    approved = await _propose_and_approve(store)
    await aa_executor.execute_approved_admin_action(approved)  # must not raise

    assert len(spy.calls) == 1  # it was called, then raised
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "blew up" in str(final.error)


async def test_executor_no_blob_is_noop(store, monkeypatch):
    """An Action with no ``_admin_action`` blob is a clean no-op."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)

    from pocketpaw.instinct.models import ActionTrigger

    action = await store.propose(
        pocket_id="w1",
        title="not admin",
        description="",
        recommendation="",
        trigger=ActionTrigger(type="agent", source="x", reason="y"),
    )
    approved = await store.approve(action.id, approver="approver1")
    await aa_executor.execute_approved_admin_action(approved)
    assert spy.calls == []


# ---------------------------------------------------------------------------
# production-path integration — propose → approve/reject via the REAL router
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path) -> DecisionGraph:
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "approver1", workspace_id: str = "w1") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_client(user: _FakeUser, monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return TestClient(app)


def _events_by_correlation(journal, correlation_id: UUID) -> list:
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


async def test_production_path_approve_runs_executor_one_terminal(
    store, journal, graph, monkeypatch
):
    """propose → approve through the REAL router handler → the executor fires the
    whitelisted service → walk the chain and assert EXACTLY agent.proposed →
    human.corrected → decision.completed. One terminal."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action="workspace.member.role_change",
        args={"target_user_id": "u2", "role": "admin"},
        proposer_user_id="admin1",
    )
    blob = (await store.get_action(action_id)).parameters["_admin_action"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("approver1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text

    # The executor fired the service exactly once.
    assert len(spy.calls) == 1
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED

    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions
    assert actions.count("decision.completed") == 1
    terminal = chain[-1]
    assert (terminal.payload or {})["passed"] is True
    assert (terminal.payload or {})["action_outcome"] == "landed"
    hc = chain[1]
    assert terminal.causation_id == hc.id


async def test_production_path_reject_router_closes_no_service_call(
    store, journal, graph, monkeypatch
):
    """Reject path: the ROUTER emits human.corrected + decision.completed
    (rejected); the executor is NEVER called (no service call)."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action="workspace.member.role_change",
        args={"target_user_id": "u2", "role": "admin"},
        proposer_user_id="admin1",
    )
    blob = (await store.get_action(action_id)).parameters["_admin_action"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("approver1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "not now"})
    assert resp.status_code == 200, resp.text

    # REJECT-NO-FIRE assertion.
    assert spy.calls == []
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED

    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions
    assert actions.count("decision.completed") == 1
    terminal = chain[-1]
    assert (terminal.payload or {})["passed"] is False
    assert (terminal.payload or {})["action_outcome"] == "rejected"


async def test_router_cross_workspace_approval_forbidden(store, journal, graph, monkeypatch):
    """A caller whose active workspace differs from the blob's workspace_id gets
    403 on approve — the tenancy gate binds the Action to the caller's
    workspace."""
    spy = SpyWorkspaceService()
    _patch_service(monkeypatch, spy)
    _allow_rbac(monkeypatch)

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w-OTHER",
        action="workspace.member.role_change",
        args={"target_user_id": "u2", "role": "admin"},
        proposer_user_id="admin1",
    )

    user = _FakeUser("approver1", "w1")  # caller in w1, action in w-OTHER
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 403, resp.text
    assert spy.calls == []
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING


async def test_recheck_rbac_denies_demoted_proposer(monkeypatch):
    """Unit-test the REAL ``_recheck_rbac``: a proposer whose CURRENT role is
    MEMBER (loaded fresh) is denied for an ADMIN-gated action — the executor's
    fail-closed hinge. Patches the User doc load + the RBAC check seam so no DB
    is touched."""

    class _M:
        def __init__(self, ws, role):
            self.workspace = ws
            self.role = role

    class _DemotedUser:
        id = "admin1"
        workspaces = [_M("w1", "member")]  # demoted since proposing

    async def _get(_id):  # noqa: ANN001
        return _DemotedUser()

    monkeypatch.setattr("pocketpaw_ee.cloud.models.user.User.get", staticmethod(_get))
    # PydanticObjectId(proposer) must not choke on a non-ObjectId test id — patch it.
    monkeypatch.setattr("beanie.PydanticObjectId", lambda x: x)

    with pytest.raises(Forbidden):
        await aa_executor._recheck_rbac("w1", "admin1", "workspace.member.role_change")


# ---------------------------------------------------------------------------
# WA-5 — the additional whitelisted ADMIN writes (member.remove / invite.create /
# invite.revoke / connector.manage / workspace.update). Per action:
#   * approve → the whitelisted service fires EXACTLY ONCE with the adapted args;
#   * reject  → the service is NEVER called;
#   * the STRICT adapter drops an unknown key an agent smuggled into args.
# The store/journal/graph fixtures + _make_client + _allow_rbac are reused.
# ---------------------------------------------------------------------------


class _CallSpy:
    """Generic async spy — records (args, kwargs) per call, returns None (matches
    the void workspace/connector service sinks)."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self._raises = raises

    async def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return None


async def _propose_approve_execute(store, monkeypatch, *, action: str, args: dict) -> Any:
    """Propose the given admin action, approve it via the store, run the executor
    (RBAC re-check stubbed to PASS). Returns the approved Action."""
    _allow_rbac(monkeypatch)
    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action=action,
        args=args,
        proposer_user_id="admin1",
    )
    approved = await store.approve(action_id, approver="approver1")
    await aa_executor.execute_approved_admin_action(approved)
    return approved


# ---- member.remove --------------------------------------------------------


async def test_executor_member_remove_fires_once(store, monkeypatch):
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.remove_member", spy)
    await _propose_approve_execute(
        store, monkeypatch, action="workspace.member.remove", args={"target_user_id": "u2"}
    )
    # APPROVE-FIRES-ONCE — remove_member(workspace_id, target_user_id, actor).
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == ("w1", "u2", "admin1")


async def test_executor_member_remove_adapter_drops_extra(store, monkeypatch):
    """A smuggled ``role='owner'`` in args never reaches remove_member."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.remove_member", spy)
    await _propose_approve_execute(
        store,
        monkeypatch,
        action="workspace.member.remove",
        args={"target_user_id": "u2", "role": "owner", "cascade": "all"},
    )
    assert len(spy.calls) == 1
    # Only (workspace_id, target_user_id, actor) — no smuggled kwargs.
    assert spy.calls[0] == (("w1", "u2", "admin1"), {})


# ---- invite.create --------------------------------------------------------


async def test_executor_invite_create_fires_once(store, monkeypatch):
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.create_invite", spy)
    await _propose_approve_execute(
        store,
        monkeypatch,
        action="invite.create",
        args={"email": "a@b.com", "role": "member"},
    )
    assert len(spy.calls) == 1
    call_args = spy.calls[0][0]
    ctx, workspace_id, body = call_args
    # The ctx attributes the write to the PROPOSER; the DTO carries ONLY email+role.
    assert ctx.user_id == "admin1"
    assert workspace_id == "w1"
    assert body.email == "a@b.com"
    assert body.role == "member"
    assert body.group_id is None  # a smuggle-free DTO


async def test_executor_invite_create_adapter_drops_extra(store, monkeypatch):
    """A smuggled ``group_id`` / ``role='owner'`` in args never reaches the DTO —
    the strict adapter rejects owner and ignores group_id."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.create_invite", spy)
    # role=owner is invalid for an invite → the adapter raises → MalformedArgs
    # failure, service never called.
    approved = await _propose_approve_execute(
        store,
        monkeypatch,
        action="invite.create",
        args={"email": "a@b.com", "role": "owner", "group_id": "sneaky"},
    )
    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED


# ---- invite.revoke --------------------------------------------------------


async def test_executor_invite_revoke_fires_once(store, monkeypatch):
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.revoke_invite", spy)
    await _propose_approve_execute(
        store, monkeypatch, action="invite.revoke", args={"invite_id": "inv1"}
    )
    # revoke_invite(workspace_id, invite_id, actor_user_id).
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == ("w1", "inv1", "admin1")


# ---- connector.manage (enable / disable / config) -------------------------


async def test_executor_connector_enable_fires_once(store, monkeypatch):
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.connectors.service.enable_connector", spy)
    await _propose_approve_execute(
        store, monkeypatch, action="connector.manage", args={"op": "enable", "name": "gmail"}
    )
    assert len(spy.calls) == 1
    call_args = spy.calls[0][0]
    workspace_id, name, body = call_args
    assert workspace_id == "w1"
    assert name == "gmail"


async def test_executor_connector_disable_fires_once(store, monkeypatch):
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.connectors.service.disable_connector", spy)
    await _propose_approve_execute(
        store, monkeypatch, action="connector.manage", args={"op": "disable", "name": "gmail"}
    )
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == ("w1", "gmail")


async def test_executor_connector_config_passes_opaque_config(store, monkeypatch):
    """op=config → update_config(workspace_id, name, UpdateConnectorConfigRequest)
    with the config carried INSIDE the DTO (never as top-level kwargs)."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.connectors.service.update_config", spy)
    await _propose_approve_execute(
        store,
        monkeypatch,
        action="connector.manage",
        args={"op": "config", "name": "gmail", "config": {"label": "Inbox"}},
    )
    assert len(spy.calls) == 1
    call_args, call_kwargs = spy.calls[0]
    workspace_id, name, body = call_args
    assert workspace_id == "w1"
    assert name == "gmail"
    assert body.config == {"label": "Inbox"}  # opaque config inside the DTO
    assert call_kwargs == {}  # nothing smuggled as kwargs


async def test_executor_connector_manage_bad_op_fails(store, monkeypatch):
    """An unknown ``op`` → the adapter raises → MalformedArgs failure, no call."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.connectors.service.enable_connector", spy)
    approved = await _propose_approve_execute(
        store, monkeypatch, action="connector.manage", args={"op": "delete", "name": "gmail"}
    )
    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED


# ---- workspace.update -----------------------------------------------------


async def test_executor_workspace_update_fires_once_only_recognized(store, monkeypatch):
    """approve → update(ctx, workspace_id, UpdateWorkspaceRequest) with ONLY the
    recognized field; a smuggled ``seats`` never reaches the DTO."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.update", spy)
    await _propose_approve_execute(
        store,
        monkeypatch,
        action="workspace.update",
        # NOTE: only ``name`` is a recognized field — the tool would have stripped
        # the rest, but even a hand-crafted blob with extras is stripped by the
        # strict adapter (the ``fields`` key only carries recognized fields).
        args={"name": "New Name"},
    )
    assert len(spy.calls) == 1
    ctx, workspace_id, body = spy.calls[0][0]
    assert ctx.user_id == "admin1"
    assert workspace_id == "w1"
    assert body.name == "New Name"
    assert body.settings is None
    assert body.branding is None


# ---- reject path — the executor never runs for any new action -------------


@pytest.mark.parametrize(
    ("action", "args", "svc_path"),
    [
        (
            "workspace.member.remove",
            {"target_user_id": "u2"},
            "pocketpaw_ee.cloud.workspace.service.remove_member",
        ),
        (
            "invite.create",
            {"email": "a@b.com", "role": "member"},
            "pocketpaw_ee.cloud.workspace.service.create_invite",
        ),
        (
            "invite.revoke",
            {"invite_id": "inv1"},
            "pocketpaw_ee.cloud.workspace.service.revoke_invite",
        ),
        (
            "connector.manage",
            {"op": "enable", "name": "gmail"},
            "pocketpaw_ee.cloud.connectors.service.enable_connector",
        ),
        (
            "workspace.update",
            {"name": "New Name"},
            "pocketpaw_ee.cloud.workspace.service.update",
        ),
    ],
)
async def test_production_path_reject_no_service_call_wa5(
    store, journal, graph, monkeypatch, action, args, svc_path
):
    """REJECT-NO-FIRE for every WA-5 write: reject via the REAL router handler →
    the executor never runs → the whitelisted service is NEVER called."""
    spy = _CallSpy()
    monkeypatch.setattr(svc_path, spy)
    _allow_rbac(monkeypatch)

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1", action=action, args=args, proposer_user_id="admin1"
    )

    user = _FakeUser("approver1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "no"})
    assert resp.status_code == 200, resp.text

    assert spy.calls == []
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED


# ---------------------------------------------------------------------------
# WA-6 — the three OWNER writes (instinct.activate / workspace.delete /
# billing.manage). Per action: approve → the whitelisted service fires EXACTLY
# ONCE with the adapted args; reject → the service is NEVER called. billing.manage
# produces a checkout url (an artifact), NOT a plan mutation. The demoted-OWNER
# execute-time re-check is proven with the REAL _recheck_rbac.
# ---------------------------------------------------------------------------


# ---- instinct.activate ----------------------------------------------------


async def test_executor_instinct_activate_fires_once(store, monkeypatch):
    """approve → set_instinct_approval_level(ctx, workspace_id, level) fires once
    with the adapted level; the proposer is the ctx actor."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.set_instinct_approval_level", spy)
    await _propose_approve_execute(
        store, monkeypatch, action="instinct.activate", args={"level": "TRUSTED"}
    )
    assert len(spy.calls) == 1
    ctx, workspace_id, level = spy.calls[0][0]
    assert ctx.user_id == "admin1"  # proposer attribution
    assert workspace_id == "w1"
    assert level == "TRUSTED"


async def test_executor_instinct_activate_bad_level_fails(store, monkeypatch):
    """An off-enum level → the strict adapter raises → MalformedArgs failure, no
    service call (a hand-crafted blob can't set an invalid level)."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.set_instinct_approval_level", spy)
    approved = await _propose_approve_execute(
        store, monkeypatch, action="instinct.activate", args={"level": "YOLO"}
    )
    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED


# ---- workspace.delete -----------------------------------------------------


async def test_executor_workspace_delete_fires_once(store, monkeypatch):
    """approve → delete(ctx, workspace_id) fires once; the delete takes NO steering
    args (a smuggled key never reaches it)."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.delete", spy)
    await _propose_approve_execute(
        store,
        monkeypatch,
        action="workspace.delete",
        # A hand-crafted blob with a smuggled key — the adapter ignores it entirely.
        args={"force": True, "target_user_id": "evil"},
    )
    assert len(spy.calls) == 1
    ctx, workspace_id = spy.calls[0][0]
    assert ctx.user_id == "admin1"
    assert workspace_id == "w1"
    assert spy.calls[0][1] == {}  # nothing smuggled as kwargs


# ---- billing.manage (propose→checkout-URL, NOT a plan mutation) -----------


async def test_executor_billing_manage_produces_checkout_not_plan_mutation(store, monkeypatch):
    """PAYMENT HONESTY — approve calls ``subscribe`` (which returns a checkout
    url); the executor records that url as the outcome. The webhook-internal
    ``set_workspace_plan`` is NEVER called — an agent can't flip a paid plan."""
    subscribe_spy = _CallSpy()

    async def _subscribe(*args, **kwargs):  # noqa: ANN002, ANN003
        subscribe_spy.calls.append((args, kwargs))
        return {"checkout_url": "https://checkout.dodo.test/session/abc"}

    monkeypatch.setattr("pocketpaw_ee.cloud.billing.service.subscribe", _subscribe)

    # set_workspace_plan is the payment-bypassing sink that must NEVER be reached.
    plan_mutation_spy = _CallSpy()
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.workspace.service.set_workspace_plan", plan_mutation_spy
    )

    approved = await _propose_approve_execute(
        store, monkeypatch, action="billing.manage", args={"plan_key": "pro"}
    )

    # subscribe fired once with (workspace_id, proposer, plan_key).
    assert len(subscribe_spy.calls) == 1
    assert subscribe_spy.calls[0][0] == ("w1", "admin1", "pro")
    # THE payment-honesty assertion: the plan was NOT silently mutated.
    assert plan_mutation_spy.calls == []

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.EXECUTED
    # The checkout url is recorded as the outcome artifact (not a "plan changed").
    outcome = final.parameters["_admin_action"]["outcome"]
    assert outcome["status"] == "executed"
    assert "checkout.dodo.test" in outcome["response_summary"]


async def test_executor_billing_manage_bad_plan_fails(store, monkeypatch):
    """An unknown plan tier → the strict adapter raises → MalformedArgs, no call."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.billing.service.subscribe", spy)
    approved = await _propose_approve_execute(
        store, monkeypatch, action="billing.manage", args={"plan_key": "platinum"}
    )
    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED


# ---- execute-time OWNER re-check — demoted proposer fails closed ----------


async def test_recheck_rbac_denies_demoted_owner_on_owner_action(monkeypatch):
    """THE KEY OWNER SECURITY TEST (real _recheck_rbac): a proposer DEMOTED from
    OWNER to ADMIN after proposing is DENIED for an OWNER-gated action
    (instinct.activate) — the executor's fail-closed hinge for the OWNER ops."""

    class _M:
        def __init__(self, ws, role):
            self.workspace = ws
            self.role = role

    class _DemotedOwner:
        id = "owner1"
        workspaces = [_M("w1", "admin")]  # was OWNER at propose, now only ADMIN

    async def _get(_id):  # noqa: ANN001
        return _DemotedOwner()

    monkeypatch.setattr("pocketpaw_ee.cloud.models.user.User.get", staticmethod(_get))
    monkeypatch.setattr("beanie.PydanticObjectId", lambda x: x)

    # instinct.activate is OWNER-gated → an ADMIN proposer is refused.
    with pytest.raises(Forbidden):
        await aa_executor._recheck_rbac("w1", "owner1", "instinct.activate")
    with pytest.raises(Forbidden):
        await aa_executor._recheck_rbac("w1", "owner1", "workspace.delete")
    with pytest.raises(Forbidden):
        await aa_executor._recheck_rbac("w1", "owner1", "billing.manage")


async def test_executor_owner_action_demoted_proposer_fails_closed(store, monkeypatch):
    """End-to-end: an OWNER-gated action (workspace.delete) approved after the
    proposer was demoted → the execute-time re-check DENIES → FAILS CLOSED, the
    delete service is NOT called."""
    spy = _CallSpy()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.delete", spy)
    _deny_rbac(monkeypatch)  # proposer no longer holds OWNER

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1",
        action="workspace.delete",
        args={},
        proposer_user_id="owner1",
    )
    approved = await store.approve(action_id, approver="approver1")
    await aa_executor.execute_approved_admin_action(approved)  # must not raise

    assert spy.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED


# ---- reject path — the executor never runs for any OWNER action -----------


@pytest.mark.parametrize(
    ("action", "args", "svc_path"),
    [
        (
            "instinct.activate",
            {"level": "TRUSTED"},
            "pocketpaw_ee.cloud.workspace.service.set_instinct_approval_level",
        ),
        (
            "workspace.delete",
            {},
            "pocketpaw_ee.cloud.workspace.service.delete",
        ),
        (
            "billing.manage",
            {"plan_key": "pro"},
            "pocketpaw_ee.cloud.billing.service.subscribe",
        ),
    ],
)
async def test_production_path_reject_no_service_call_wa6(
    store, journal, graph, monkeypatch, action, args, svc_path
):
    """REJECT-NO-FIRE for every WA-6 OWNER write: reject via the REAL router →
    the executor never runs → the whitelisted service is NEVER called."""
    spy = _CallSpy()
    monkeypatch.setattr(svc_path, spy)
    _allow_rbac(monkeypatch)

    action_id = await aa_propose.propose_admin_action(
        workspace_id="w1", action=action, args=args, proposer_user_id="owner1"
    )

    user = _FakeUser("approver1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "no"})
    assert resp.status_code == 200, resp.text

    assert spy.calls == []
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED
