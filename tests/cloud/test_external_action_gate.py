# tests/cloud/test_external_action_gate.py — the gated external-action proposal
# type (the third gated Instinct kind, alongside _pocket_write + _code_change).
#
# Created: 2026-06-11 (feat/external-action-proposal).
#
# What this pins:
#   * propose_external_action — blob shape (schema 1, connector ref, action,
#     params, params_hash, idempotency_key, correlation_id, summary), tenancy
#     (workspace required), and that NO connector secret is written.
#   * execute_approved_external_action with a FAKE connector service:
#       - happy path: connector called with the proposed params, action marked
#         EXECUTED, structured outcome back-written.
#       - idempotency: a second invocation does NOT re-fire the connector call.
#       - hash mismatch: a params edit between propose and approve → FAILED, no
#         call fired.
#       - schema mismatch: a stale schema blob → FAILED, no call fired.
#       - connector failure (success=False) → FAILED status, NOT an exception.
#       - connector crash (raise) → FAILED status, NOT an exception.
#   * the MANDATORY production-path integration test: propose → approve via the
#     REAL router handler → executor runs (fake connector) → walk the decision
#     chain and assert EXACTLY the expected events (agent.proposed →
#     human.corrected → decision.completed), no double terminal.
#   * reject path: the router emits human.corrected + decision.completed
#     (rejected); the executor is NEVER called.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The store is patched
# to a tmp-file InstinctStore; the connector service's `execute` is monkeypatched
# to a fake so nothing touches a real connector. The integration test wires a
# fresh on-disk journal + DecisionGraph (same fixture shape as
# tests/ee/test_instinct_decision_events.py) and drives the router over a
# TestClient with stubbed auth deps.

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
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.decisions.service import (  # noqa: E402
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path  # noqa: E402
from pocketpaw_ee.cloud.external_actions import executor as ea_executor  # noqa: E402
from pocketpaw_ee.cloud.external_actions import propose as ea_propose  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402
from soul_protocol.engine.journal import open_journal  # noqa: E402

import pocketpaw.journal_dep as journal_dep  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------


class _FakeExecuteResponse:
    """Duck-typed ExecuteActionResponse the executor reads (.success / .data /
    .error)."""

    def __init__(self, *, success: bool, data: Any = None, error: str | None = None):
        self.success = success
        self.data = data
        self.error = error
        self.records_affected = 0
        self.execution_mode = "cloud"


class FakeConnectorService:
    """Records connector calls + returns a scripted response. Never touches a
    real connector. Injected by monkeypatching
    ``connectors.service.execute``."""

    def __init__(
        self, *, response: _FakeExecuteResponse | None = None, raises: Exception | None = None
    ):
        self.calls: list[dict[str, Any]] = []
        self._response = response or _FakeExecuteResponse(success=True, data={"ok": True})
        self._raises = raises

    async def execute(self, workspace_id, name, body, *, user_id=None):
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "name": name,
                "action": body.action,
                "params": dict(body.params),
                "scope": body.scope,
                "pocket_id": body.pocket_id,
                "user_id": user_id,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired everywhere the gate reads it
    (the propose helper + the executor both lazy-import
    ``pocketpaw.stores.get_instinct_store``)."""
    st = InstinctStore(tmp_path / "instinct_external_action_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


def _patch_connector(monkeypatch, fake: FakeConnectorService) -> None:
    """Patch the cloud connector service's ``execute`` to the fake."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.connectors.service.execute",
        fake.execute,
    )


# ---------------------------------------------------------------------------
# propose — blob shape + tenancy
# ---------------------------------------------------------------------------


async def test_propose_builds_external_action_blob(store):
    """propose_external_action files an Action carrying a well-formed schema-1
    ``_external_action`` blob — connector ref, action, params, params_hash,
    idempotency_key, summary — and NO connector secret."""
    action_id = await ea_propose.propose_external_action(
        workspace_id="w1",
        connector_name="crm",
        action="approveApplication",
        params={"application_id": "app-7", "note": "ok"},
        requested_by="u1",
        scope="workspace",
    )
    action = await store.get_action(action_id)
    assert action is not None
    assert action.status == ActionStatus.PENDING

    blob = action.parameters["_external_action"]
    assert blob["kind"] == "external_action"
    assert blob["schema"] == 1
    assert blob["workspace_id"] == "w1"
    assert blob["connector_name"] == "crm"
    assert blob["action"] == "approveApplication"
    assert blob["params"] == {"application_id": "app-7", "note": "ok"}
    assert blob["requested_by"] == "u1"
    # params_hash is the canonical hash of action + params.
    assert blob["params_hash"] == ea_propose.compute_params_hash(
        "approveApplication", {"application_id": "app-7", "note": "ok"}
    )
    # idempotency key present; correlation_id minted.
    assert blob["idempotency_key"]
    assert blob["correlation_id"]
    assert blob["summary"]
    # NO secret/token anywhere on the blob.
    flat = str(blob).lower()
    assert "token" not in flat
    assert "secret" not in flat
    assert "authorization" not in flat


async def test_propose_requires_workspace(store):
    """A propose with no workspace_id is rejected up front — an external action
    with no tenant to scope it to is unexecutable."""
    with pytest.raises(ValueError, match="workspace_id"):
        await ea_propose.propose_external_action(
            workspace_id="",
            connector_name="crm",
            action="approveApplication",
            params={},
            requested_by="u1",
        )


async def test_propose_custom_idempotency_and_summary(store):
    """A caller-supplied idempotency_key + summary round-trip onto the blob."""
    action_id = await ea_propose.propose_external_action(
        workspace_id="w1",
        connector_name="crm",
        action="sendInvoice",
        params={"id": 5},
        requested_by="u1",
        idempotency_key="my-key-1",
        summary="Send invoice #5 to the customer.",
    )
    blob = (await store.get_action(action_id)).parameters["_external_action"]
    assert blob["idempotency_key"] == "my-key-1"
    assert blob["summary"] == "Send invoice #5 to the customer."


# ---------------------------------------------------------------------------
# executor — happy path, idempotency, hash, schema, failure
# ---------------------------------------------------------------------------


async def _propose_and_approve(store, **kwargs) -> Any:
    """Propose then approve via the store, returning the approved Action."""
    action_id = await ea_propose.propose_external_action(
        workspace_id="w1",
        connector_name="crm",
        action="approveApplication",
        params={"application_id": "app-7"},
        requested_by="u1",
        **kwargs,
    )
    return await store.approve(action_id, approver="u1")


async def test_executor_happy_path_calls_connector(store, monkeypatch):
    """On approve the executor resolves the connector, calls the named action
    with the proposed params, marks EXECUTED, and back-writes the structured
    outcome {status, response_summary, executed_at}."""
    fake = FakeConnectorService(
        response=_FakeExecuteResponse(success=True, data={"decision": "approved"})
    )
    _patch_connector(monkeypatch, fake)

    approved = await _propose_and_approve(store)
    await ea_executor.execute_approved_external_action(approved)

    # The connector was called once with the proposed action + params.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["workspace_id"] == "w1"
    assert call["name"] == "crm"
    assert call["action"] == "approveApplication"
    assert call["params"] == {"application_id": "app-7"}

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.EXECUTED
    blob = final.parameters["_external_action"]
    assert blob["outcome"]["status"] == "executed"
    assert "executed_at" in blob["outcome"]
    assert blob["outcome"]["response_summary"]


async def test_executor_idempotent_never_double_fires(store, monkeypatch):
    """Re-invoking the executor on an already-executed Action does NOT call the
    connector a second time."""
    fake = FakeConnectorService()
    _patch_connector(monkeypatch, fake)

    approved = await _propose_and_approve(store)
    await ea_executor.execute_approved_external_action(approved)
    assert len(fake.calls) == 1

    # Re-fetch the (now executed) Action and re-invoke — the idempotency guard
    # short-circuits before the connector call.
    reloaded = await store.get_action(approved.id)
    await ea_executor.execute_approved_external_action(reloaded)
    assert len(fake.calls) == 1  # still ONE


async def test_executor_hash_mismatch_refuses(store, monkeypatch):
    """A params edit between propose and approve (the stored hash no longer
    matches the params) → FAILED, no connector call."""
    fake = FakeConnectorService()
    _patch_connector(monkeypatch, fake)

    approved = await _propose_and_approve(store)
    # Tamper with the params on the in-memory Action AFTER approval — the stored
    # params_hash no longer matches.
    approved.parameters["_external_action"]["params"] = {"application_id": "DIFFERENT"}

    await ea_executor.execute_approved_external_action(approved)

    assert fake.calls == []  # nothing fired
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "hash" in str(final.error).lower()
    assert final.parameters["_external_action"]["outcome"]["status"] == "failed"


async def test_executor_schema_mismatch_refuses(store, monkeypatch):
    """A stale blob with an incompatible schema version → FAILED, no call."""
    fake = FakeConnectorService()
    _patch_connector(monkeypatch, fake)

    approved = await _propose_and_approve(store)
    approved.parameters["_external_action"]["schema"] = 999  # incompatible

    await ea_executor.execute_approved_external_action(approved)

    assert fake.calls == []
    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "schema" in str(final.error).lower()


async def test_executor_connector_failure_marks_failed_not_exception(store, monkeypatch):
    """A connector that reports success=False → the Action is FAILED (not a
    phantom success), and the executor does NOT raise."""
    fake = FakeConnectorService(
        response=_FakeExecuteResponse(success=False, error="upstream 422 invalid application")
    )
    _patch_connector(monkeypatch, fake)

    approved = await _propose_and_approve(store)
    # Must NOT raise.
    await ea_executor.execute_approved_external_action(approved)

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "422" in str(final.error)
    blob = final.parameters["_external_action"]
    assert blob["outcome"]["status"] == "failed"
    assert "422" in blob["outcome"]["response_summary"]


async def test_executor_connector_crash_marks_failed_not_exception(store, monkeypatch):
    """A connector adapter that RAISES → the Action is FAILED, the executor
    swallows the exception (best-effort, never breaks the approve response)."""
    fake = FakeConnectorService(raises=RuntimeError("connector blew up"))
    _patch_connector(monkeypatch, fake)

    approved = await _propose_and_approve(store)
    await ea_executor.execute_approved_external_action(approved)  # must not raise

    final = await store.get_action(approved.id)
    assert final.status == ActionStatus.FAILED
    assert "blew up" in str(final.error)
    assert final.parameters["_external_action"]["outcome"]["status"] == "failed"


async def test_executor_no_blob_is_noop(store, monkeypatch):
    """An Action with no ``_external_action`` blob is a clean no-op (no chain was
    ever opened)."""
    fake = FakeConnectorService()
    _patch_connector(monkeypatch, fake)

    from pocketpaw.instinct.models import ActionTrigger

    action = await store.propose(
        pocket_id="w1",
        title="not external",
        description="",
        recommendation="",
        trigger=ActionTrigger(type="agent", source="x", reason="y"),
    )
    approved = await store.approve(action.id, approver="u1")
    await ea_executor.execute_approved_external_action(approved)
    assert fake.calls == []


# ---------------------------------------------------------------------------
# MANDATORY production-path integration test — propose → approve via the REAL
# router → executor runs → walk the chain, assert EXACTLY one terminal.
# ---------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal wired into the lazy
    ``pocketpaw.journal_dep.get_journal`` lookup (same shape as the Slice 3
    tests). ``journal_writer.record_decision_event`` resolves the journal
    through that dep so production code + tests share the singleton."""
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
    """Fresh DecisionGraph + decisions.db as the process-global singleton."""
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
    def __init__(self, user_id: str = "u1", workspace_id: str = "w1") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_client(user: _FakeUser, monkeypatch) -> TestClient:
    """Build a TestClient over the Instinct router with auth deps stubbed and a
    CloudError handler so a Forbidden maps to 403."""
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
    """THE mandatory chain-doubling guard: propose → approve through the REAL
    router handler → the executor fires the connector → walk the decision chain
    and assert EXACTLY agent.proposed → human.corrected → decision.completed.
    No second terminal (the router must NOT also close on approve)."""
    fake = FakeConnectorService(
        response=_FakeExecuteResponse(success=True, data={"decision": "approved"})
    )
    _patch_connector(monkeypatch, fake)

    # Propose through the real helper (mints correlation_id + agent.proposed).
    action_id = await ea_propose.propose_external_action(
        workspace_id="w1",
        connector_name="crm",
        action="approveApplication",
        params={"application_id": "app-7"},
        requested_by="u1",
    )
    blob = (await store.get_action(action_id)).parameters["_external_action"]
    corr = UUID(blob["correlation_id"])

    # Approve via the REAL router handler (the production path that fires the
    # executor). The router's _store indirection points at our tmp store.
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 200, resp.text

    # The executor fired the connector exactly once.
    assert len(fake.calls) == 1
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED

    # Walk the chain — EXACTLY the three expected events, in order, ONE terminal.
    chain = _events_by_correlation(journal, corr)
    actions = [e.action for e in chain]
    assert actions == [
        "agent.proposed",
        "human.corrected",
        "decision.completed",
    ], actions
    # Exactly one decision.completed (no double close).
    assert actions.count("decision.completed") == 1
    # The terminal is the executor's success close.
    terminal = chain[-1]
    assert (terminal.payload or {})["passed"] is True
    assert (terminal.payload or {})["action_outcome"] == "landed"
    # human.corrected → decision.completed causation chain.
    hc = chain[1]
    assert terminal.causation_id == hc.id


async def test_production_path_reject_router_closes_executor_never_called(
    store, journal, graph, monkeypatch
):
    """Reject path: the ROUTER emits human.corrected + decision.completed
    (rejected); the executor is NEVER called (no connector call)."""
    fake = FakeConnectorService()
    _patch_connector(monkeypatch, fake)

    action_id = await ea_propose.propose_external_action(
        workspace_id="w1",
        connector_name="crm",
        action="approveApplication",
        params={"application_id": "app-7"},
        requested_by="u1",
    )
    blob = (await store.get_action(action_id)).parameters["_external_action"]
    corr = UUID(blob["correlation_id"])

    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "not now"})
    assert resp.status_code == 200, resp.text

    # The executor never ran — no connector call.
    assert fake.calls == []
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.REJECTED

    # The router owns the close on reject: agent.proposed → human.corrected →
    # decision.completed(rejected). Exactly one terminal.
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
    """A caller whose active workspace differs from the blob's workspace_id
    gets 403 on approve — the tenancy gate binds the Action to the caller's
    workspace."""
    fake = FakeConnectorService()
    _patch_connector(monkeypatch, fake)

    action_id = await ea_propose.propose_external_action(
        workspace_id="w-OTHER",
        connector_name="crm",
        action="approveApplication",
        params={"application_id": "app-7"},
        requested_by="u1",
    )

    # Caller is in w1 but the action belongs to w-OTHER.
    user = _FakeUser("u1", "w1")
    client = _make_client(user, monkeypatch)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: store)
    resp = client.post(f"/instinct/actions/{action_id}/approve")
    assert resp.status_code == 403, resp.text
    # Nothing fired.
    assert fake.calls == []
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.PENDING
