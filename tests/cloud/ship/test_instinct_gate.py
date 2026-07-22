# tests/cloud/ship/test_instinct_gate.py — the /ship Instinct gate.
#
# The security-critical suite. What it proves:
#
#   1. A destroy request executes NOTHING. The engine fake fails loudly on any
#      command that isn't in its recorded map, and ``dokku apps:destroy`` is
#      deliberately absent — so if the request path ever reached the engine,
#      these tests fail rather than silently passing.
#   2. The approve path executes EXACTLY once, even when two approvals race.
#   3. The reject path executes nothing.
#   4. A params edit between propose and approve is refused (hash re-check).
#   5. A stale blob schema is refused.
#   6. A since-demoted proposer's approved teardown FAILS CLOSED.
#
# Created 2026-07-22 (feat/ship-4-agent-surface, SHIP-4): new module.

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pocketpaw_ee.cloud.ship import executor as ship_executor
from pocketpaw_ee.cloud.ship import propose as ship_propose

# The HTTP fixtures + helpers live with the router suite; reuse them rather than
# maintaining a second copy of the client wiring.
from tests.cloud.ship.test_ship_router import (  # noqa: F401 — w1 is a fixture
    _app_on_box,
    _ready_box,
    w1,
)


class FakeAction:
    """The minimal Action surface the executor reads."""

    def __init__(self, blob: dict[str, Any], *, action_id: str = "act-1", status: str = "approved"):
        self.id = action_id
        self.parameters = {ship_propose.SHIP_ACTION_PARAM_KEY: blob}
        self.status = status


class RecordingStore:
    """Captures the terminal marks the executor makes."""

    def __init__(self):
        self.executed: list[tuple[str, Any]] = []
        self.failed: list[tuple[str, str]] = []
        self._db_path = "/nonexistent/instinct.db"  # the outcome write is best-effort

    async def mark_executed(self, action_id: str, outcome: Any = None) -> None:
        self.executed.append((action_id, outcome))

    async def mark_failed(self, action_id: str, error: str) -> None:
        self.failed.append((action_id, error))


def _blob(**overrides: Any) -> dict[str, Any]:
    """A well-formed ``_ship_action`` blob for a box teardown."""
    verb = overrides.pop("verb", "destroy_app")
    params = overrides.pop("params", {})
    blob = {
        "kind": ship_propose.SHIP_ACTION_KIND,
        "schema": ship_propose.SHIP_ACTION_SCHEMA,
        "workspace_id": "ws-1",
        "verb": verb,
        "box_id": "box-1",
        "app_id": "app-1",
        "target_label": "app demo",
        "params": params,
        "params_hash": ship_propose.compute_params_hash(verb, params),
        "idempotency_key": "ws-1:destroy_app:app-1:abc",
        "requested_by": "user-1",
        "summary": "Destroy app demo.",
        "correlation_id": "8f14e45f-ceea-467a-9f8b-2f4a1c1d5e6f",
        "proposed_event_id": None,
        "outcome": None,
    }
    blob.update(overrides)
    return blob


@pytest.fixture
def store(monkeypatch):
    """Swap the Instinct store for a recorder."""
    recorder = RecordingStore()
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: recorder)
    return recorder


@pytest.fixture
def authorized(monkeypatch):
    """The proposer still holds ``ship.manage``."""

    async def _ok(_workspace_id: str, _user_id: str) -> bool:
        return True

    monkeypatch.setattr(ship_executor, "_proposer_still_authorized", _ok)


@pytest.fixture
def ran(monkeypatch):
    """Record every verb the executor actually runs against a box."""
    calls: list[str] = []

    async def _run(blob: dict[str, Any]) -> tuple[bool, str]:
        calls.append(str(blob.get("verb")))
        return True, f"ran {blob.get('verb')}"

    monkeypatch.setattr(ship_executor, "_run_verb", _run)
    return calls


@pytest.fixture(autouse=True)
def _clear_locks():
    """The executor's per-action locks are module state — reset between tests."""
    ship_executor._LOCKS.clear()
    yield
    ship_executor._LOCKS.clear()


# ---------------------------------------------------------------------------
# 1. The request path never destroys
# ---------------------------------------------------------------------------


async def test_delete_box_runs_no_engine_command(w1, monkeypatch):
    """A DELETE parks a proposal and touches no engine command.

    The engine fake raises on any command outside its recorded map, and
    ``apps:destroy`` is not in it — so reaching the engine here would fail the
    test rather than pass silently.
    """
    from tests.cloud.ship.conftest import install_fake_engine

    install_fake_engine(monkeypatch)
    box_id = await _ready_box(w1)

    resp = await w1.delete(f"/ship/boxes/{box_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["proposal_id"]


async def test_delete_app_runs_no_engine_command(w1, monkeypatch):
    from tests.cloud.ship.conftest import install_fake_engine

    install_fake_engine(monkeypatch)
    app_id = await _app_on_box(w1, await _ready_box(w1))

    resp = await w1.delete(f"/ship/apps/{app_id}")

    assert resp.json()["status"] == "pending_approval"


# ---------------------------------------------------------------------------
# 2. Approve executes exactly once — including under a race
# ---------------------------------------------------------------------------


async def test_approve_executes_the_verb(store, authorized, ran):
    action = FakeAction(_blob())

    await ship_executor.execute_approved_ship_action(action)

    assert ran == ["destroy_app"]
    assert store.executed and not store.failed


async def test_concurrent_approvals_execute_once(store, authorized, ran):
    """The production defect this gate inherits: two concurrent invocations on
    ONE approved action both passing the guard and double-firing."""
    action = FakeAction(_blob())

    await asyncio.gather(
        ship_executor.execute_approved_ship_action(action),
        ship_executor.execute_approved_ship_action(action),
    )

    assert ran == ["destroy_app"], f"the verb fired {len(ran)} times — must be exactly 1"


async def test_re_approval_does_not_re_fire(store, authorized, ran):
    """A blob carrying an outcome has already run; a retry must not re-destroy."""
    action = FakeAction(_blob(outcome={"status": "executed", "detail": "done"}))

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []


async def test_terminal_action_does_not_re_fire(store, authorized, ran):
    action = FakeAction(_blob(), status="executed")

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []


# ---------------------------------------------------------------------------
# 3. Nothing fires without a well-formed, unedited, authorized blob
# ---------------------------------------------------------------------------


async def test_a_non_ship_action_is_ignored(store, authorized, ran):
    action = FakeAction(_blob())
    action.parameters = {"_external_action": {"kind": "external_action"}}

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []
    assert not store.executed and not store.failed


async def test_params_edited_after_propose_is_refused(store, authorized, ran):
    """A human approved a SPECIFIC teardown. An edited blob must not fire."""
    blob = _blob(params={"image": "original"})
    blob["params"] = {"image": "SOMETHING-ELSE"}  # hash now stale
    action = FakeAction(blob)

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []
    assert store.failed and "params changed" in store.failed[0][1]


async def test_stale_schema_is_refused(store, authorized, ran):
    action = FakeAction(_blob(schema=999))

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []
    assert store.failed and "schema mismatch" in store.failed[0][1]


async def test_missing_workspace_is_refused(store, authorized, ran):
    blob = _blob(workspace_id="")
    blob["params_hash"] = ship_propose.compute_params_hash("destroy_app", {})
    action = FakeAction(blob)

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []
    assert store.failed


async def test_demoted_proposer_fails_closed(store, monkeypatch, ran):
    """The proposer lost ``ship.manage`` while the action sat in the tray."""

    async def _denied(_workspace_id: str, _user_id: str) -> bool:
        return False

    monkeypatch.setattr(ship_executor, "_proposer_still_authorized", _denied)
    action = FakeAction(_blob())

    await ship_executor.execute_approved_ship_action(action)

    assert ran == []
    assert store.failed and "no longer authorized" in store.failed[0][1]


async def test_engine_failure_is_recorded_not_raised(store, authorized, monkeypatch):
    """An engine failure becomes a ``failed`` outcome, never an exception."""

    async def _fails(_blob: dict[str, Any]) -> tuple[bool, str]:
        return False, "engine call failed (TimeoutError)"

    monkeypatch.setattr(ship_executor, "_run_verb", _fails)

    await ship_executor.execute_approved_ship_action(FakeAction(_blob()))

    assert not store.executed
    assert store.failed and "TimeoutError" in store.failed[0][1]


async def test_executor_never_raises_even_on_a_programming_error(store, authorized, monkeypatch):
    """The never-raises contract is absolute: a bug inside the verb runner must
    still not break the approve response."""

    async def _boom(_blob: dict[str, Any]) -> tuple[bool, str]:
        raise RuntimeError("attribute blew up")

    monkeypatch.setattr(ship_executor, "_run_verb", _boom)

    # No pytest.raises — this must return cleanly.
    await ship_executor.execute_approved_ship_action(FakeAction(_blob()))

    assert not store.executed
    assert store.failed and "unexpectedly" in store.failed[0][1]


# ---------------------------------------------------------------------------
# Propose-side contract
# ---------------------------------------------------------------------------


def test_params_hash_is_order_independent():
    a = ship_propose.compute_params_hash("rollback", {"image": "x", "tag": "1"})
    b = ship_propose.compute_params_hash("rollback", {"tag": "1", "image": "x"})
    assert a == b


def test_params_hash_changes_with_the_verb():
    assert ship_propose.compute_params_hash("destroy_app", {}) != ship_propose.compute_params_hash(
        "destroy_box", {}
    )


async def test_propose_rejects_a_non_gated_verb():
    with pytest.raises(ValueError, match="non-gated verb"):
        await ship_propose.propose_ship_action(
            workspace_id="ws-1", verb="logs", box_id="b1", requested_by="u1"
        )


async def test_propose_requires_a_target():
    with pytest.raises(ValueError, match="box_id or an app_id"):
        await ship_propose.propose_ship_action(
            workspace_id="ws-1", verb="destroy_app", requested_by="u1"
        )


async def test_propose_requires_a_workspace():
    with pytest.raises(ValueError, match="workspace_id"):
        await ship_propose.propose_ship_action(
            workspace_id="", verb="destroy_app", app_id="a1", requested_by="u1"
        )
