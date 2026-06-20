# tests/ee/test_instinct_rule_router.py — S2-R4 (router four-path dispatch + tenancy).
#
# Created: 2026-06-20 (S2-R4 / feat/szd-slice2-discovery). The security gate on the
# ``_instinct_rule`` Instinct proposal type. Clones the cross-workspace-403 discipline
# of ``tests/cloud/test_instinct_approval_security.py`` (the ``_pocket_write`` /
# ``_code_change`` peers) for the governed-rule-create gate, and adds the router-driven
# round-trip the security clone does not need.
#
# THE LOAD-BEARING ASSERTION (RK-5 — the four-path trap): a foreign-workspace
# ``_instinct_rule`` Action is refused with 403 + ``instinct.cross_workspace_approval``
# on ALL FOUR router entry points — ``approve_action``, ``bulk_approve_actions``,
# ``reject_action``, ``bulk_reject_actions``. Missing the guard on even ONE path is a
# cross-tenant approval escalation (the pocketpaw#1183 bug class). Each path gets its
# own explicit test below; do not delete any.
#
# Plus (RK-6 — exactly-one chain-close):
#   * a SAME-workspace approve VIA THE ROUTER lands the rule (asserted through
#     ``rules.service.get_active_rules``) and emits exactly ONE ``decision.completed``
#     (the executor owns the close on approve);
#   * a SAME-workspace reject VIA THE ROUTER closes the chain (the router owns it) with
#     exactly ONE ``decision.completed`` and writes NO rule;
#   * a mixed bulk approve / bulk reject does not cross-fire kinds (an ``_instinct_rule``
#     item routes to the rule executor / rule close; a sibling ``_pocket_write`` item in
#     the same batch routes to its own path).
#
# The 403 tests are sync and drive the router over HTTP via ``TestClient`` (the
# TestClient owns the only event loop, matching the security clone) — they 403 BEFORE
# any executor/DB touch, so no Beanie is needed. The round-trip tests that actually land
# a rule are async (``AsyncClient`` + ``ASGITransport`` + the ``beanie_test_db``
# mongomock fixture) so the router's in-request executor and the post-hoc
# ``get_active_rules`` read share one event loop and one in-memory database.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_instinct_rule_router.py -q

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.instinct_rule_proposals import (  # noqa: E402
    INSTINCT_RULE_PARAM_KEY,
    propose_instinct_rule,
)
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

TRIGGER = {"type": "agent", "source": "claude", "reason": "rule router test"}


# ---------------------------------------------------------------------------
# Fixtures — auth doubles + a CloudError-aware app (cloned from the security test)
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so ``rules.service.create_rule``'s ``emit()`` is quiet.

    ``tests/ee`` has no autouse RecordingBus; mint a minimal one (per
    test_instinct_rule_propose / test_rules_service)."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield bus_mod._bus
    bus_mod._bus = prev  # type: ignore[attr-defined]


def _rule_spec(workspace_id: str, name: str = "Require approval on high-value invoices") -> dict:
    """A valid editable RuleDraft-shaped rule_spec. Tenancy lives in ``scope`` here, but
    the propose helper carries ``workspace_id`` / owner as SEPARATE top-level blob
    fields (so the cross-workspace gate reads the blob's top-level workspace_id)."""
    return {
        "name": name,
        "description": "Flag invoices over 10k for human review.",
        "when": "object.amount > 10000",
        "action": "require_approval",
        "scope": {"workspace_id": workspace_id, "object_type": "Invoice"},
        "confidence": 0.82,
        "provenance": ["audit:row-1", "correction:c-9"],
    }


def _pocket_write_params(workspace_id: str) -> dict:
    """A sibling ``_pocket_write`` blob — the shape the pocket-write bridge stores. Used
    in the mixed-batch tests to prove kinds don't cross-fire."""
    return {
        "_pocket_write": {
            "schema": 2,
            "action": "mark_renewed",
            "method": "POST",
            "path": "/leases/42/renew",
            "params": {"rent": 2000},
            "idempotency_key": "idem-xyz",
            "outcome": "renewal_completed",
            "workspace_id": workspace_id,
            "requested_by": "requester-9",
            "correlation_id": None,
            "parked_policy_event_id": None,
        }
    }


@pytest.fixture
def router_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "instinct_rule_router.db")


def _make_app(user: _FakeUser, monkeypatch) -> FastAPI:
    """Build a FastAPI app over the instinct router with a CloudError handler so a
    ``Forbidden`` maps to a 403 (not a 500), and the license / plan deps stubbed."""
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


def _make_client(router_store: InstinctStore, user: _FakeUser, monkeypatch) -> TestClient:
    return TestClient(_make_app(user, monkeypatch))


def _propose_rule_action(client: TestClient, *, workspace_id: str, name: str = "rule") -> str:
    """Seed a pending Action carrying an ``_instinct_rule`` blob over HTTP and return
    its id. The blob's top-level ``workspace_id`` is what the cross-workspace gate
    reads."""
    resp = client.post(
        "/instinct/actions",
        json={
            "pocket_id": workspace_id,
            "title": f"governed rule {name}",
            "trigger": TRIGGER,
            "parameters": {
                INSTINCT_RULE_PARAM_KEY: {
                    "kind": "instinct_rule",
                    "schema": 1,
                    "workspace_id": workspace_id,
                    "user_id": "user-A",
                    "rule_spec": _rule_spec(workspace_id, name=name),
                    "summary": f"Create the governed rule {name!r}.",
                    "correlation_id": None,
                    "proposed_event_id": None,
                }
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _propose_pocket_write_action(client: TestClient, *, workspace_id: str) -> str:
    resp = client.post(
        "/instinct/actions",
        json={
            "pocket_id": "pocket-A",
            "title": "ws write",
            "trigger": TRIGGER,
            "parameters": _pocket_write_params(workspace_id),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _status_of(client: TestClient, action_id: str) -> str:
    resp = client.get("/instinct/actions", params={"limit": 500})
    assert resp.status_code == 200, resp.text
    for action in resp.json()["actions"]:
        if action["id"] == action_id:
            return action["status"]
    raise AssertionError(f"action {action_id} not found")


# ===========================================================================
# THE FOUR-PATH SECURITY GATE — cross-workspace 403 on every entry point.
# Each path is a separate test: missing the guard on ONE = the escalation bug.
# ===========================================================================


class TestInstinctRuleCrossWorkspace403:
    """``_assert_instinct_rule_workspace`` must refuse a foreign-workspace
    ``_instinct_rule`` Action with 403 + ``instinct.cross_workspace_approval`` on the
    approve, bulk-approve, reject, AND bulk-reject paths. A rule create carries no
    pocket, so without this gate a ws-A admin could approve (and APPLY) a ws-B rule
    into ws-B."""

    def test_single_approve_of_foreign_workspace_rule_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose_rule_action(client, workspace_id="ws-B")
            resp = client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, action_id) == "pending"

    def test_bulk_approve_of_foreign_workspace_rule_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose_rule_action(client, workspace_id="ws-A", name="own")
            foreign = _propose_rule_action(client, workspace_id="ws-B", name="foreign")
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [own, foreign]})
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            # The whole batch is rejected — nothing flipped.
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"

    def test_single_reject_of_foreign_workspace_rule_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            action_id = _propose_rule_action(client, workspace_id="ws-B")
            resp = client.post(f"/instinct/actions/{action_id}/reject", json={"reason": "nope"})
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, action_id) == "pending"

    def test_bulk_reject_of_foreign_workspace_rule_is_403(
        self, router_store: InstinctStore, monkeypatch
    ) -> None:
        client = _make_client(router_store, _FakeUser("user-A", "ws-A"), monkeypatch)
        with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
            own = _propose_rule_action(client, workspace_id="ws-A", name="own")
            foreign = _propose_rule_action(client, workspace_id="ws-B", name="foreign")
            resp = client.post(
                "/instinct/actions/bulk-reject",
                json={"ids": [own, foreign], "reason": "batch nope"},
            )
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
            assert _status_of(client, own) == "pending"
            assert _status_of(client, foreign) == "pending"


# ===========================================================================
# SAME-WORKSPACE round-trip VIA THE ROUTER — the rule lands / no rule on reject,
# with exactly ONE decision.completed per path (RK-6: approve→executor owns,
# reject→router owns, never both).
# ===========================================================================


async def _async_router_client(
    router_store: InstinctStore, user: _FakeUser, monkeypatch
) -> AsyncClient:
    """Async client over the instinct router app, sharing the test event loop with the
    ``beanie_test_db`` mongomock DB so the in-request executor's write is visible to a
    post-hoc ``get_active_rules`` read."""
    app = _make_app(user, monkeypatch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _seed_rule_action(workspace_id: str, *, name: str = "rule") -> str:
    """Seed a pending ``_instinct_rule`` Action through the real propose helper (it mints
    the correlation_id + opens the chain), returning the action id. Caller must have
    pointed ``pocketpaw.stores.get_instinct_store`` at the shared store."""
    return await propose_instinct_rule(
        workspace_id=workspace_id,
        user_id="user-A",
        rule_spec=_rule_spec(workspace_id, name=name),
        summary=f"Stage {name!r}.",
    )


@pytest.fixture
def shared_store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """An InstinctStore wired to BOTH store seams — the router's
    ``pocketpaw_ee.instinct.router._store`` (patched per-test) and the propose/executor's
    ``pocketpaw.stores.get_instinct_store`` (patched here) — so propose + router-approve
    + executor all share one store."""
    st = InstinctStore(tmp_path / "instinct_rule_roundtrip.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: st)
    return st


async def test_same_workspace_approve_via_router_lands_rule_one_close(
    shared_store: InstinctStore, beanie_test_db, monkeypatch
) -> None:
    """A ws-A approver approving a ws-A ``_instinct_rule`` Action THROUGH THE ROUTER
    lands the governed rule (visible via ``rules.service.get_active_rules``) and emits
    exactly ONE ``decision.completed`` (the executor owns the close on approve)."""
    from pocketpaw_ee.cloud.rules import service as rules_service

    closes: list[dict[str, Any]] = []
    import pocketpaw_ee.cloud.decisions.journal_writer as jw

    real_close = jw.record_decision_completed

    def _spy_close(**kwargs: Any) -> Any:
        closes.append(kwargs)
        return real_close(**kwargs)

    monkeypatch.setattr(jw, "record_decision_completed", _spy_close)

    action_id = await _seed_rule_action("ws-A")

    user = _FakeUser("user-A", "ws-A")
    with patch("pocketpaw_ee.instinct.router._store", return_value=shared_store):
        async with await _async_router_client(shared_store, user, monkeypatch) as client:
            resp = await client.post(f"/instinct/actions/{action_id}/approve")
            assert resp.status_code == 200, resp.text
            assert resp.json()["action"]["status"] == "approved"

    # The rule LANDED in ws-A via the router-driven approve.
    active = await rules_service.get_active_rules("ws-A")
    assert len(active) == 1
    assert active[0]["name"] == "rule"
    assert active[0]["action"] == "require_approval"
    assert active[0]["workspace_id"] == "ws-A"

    # The action reached EXECUTED (the executor's mark_executed).
    final = await shared_store.get_action(action_id)
    status_value = getattr(final.status, "value", final.status)
    assert str(status_value) == "executed", final.error

    # Exactly ONE decision.completed — the executor's success close, no router double.
    assert len(closes) == 1
    assert closes[0]["payload"]["passed"] is True
    assert closes[0]["payload"]["action_outcome"] == "landed"


async def test_same_workspace_reject_via_router_closes_chain_no_rule(
    shared_store: InstinctStore, beanie_test_db, monkeypatch
) -> None:
    """A ws-A approver rejecting a ws-A ``_instinct_rule`` Action THROUGH THE ROUTER
    closes the chain (the ROUTER owns the close on reject) with exactly ONE
    ``decision.completed`` and writes NO rule (the executor never runs on reject)."""
    from pocketpaw_ee.cloud.rules import service as rules_service

    closes: list[dict[str, Any]] = []
    import pocketpaw_ee.cloud.decisions.journal_writer as jw

    real_close = jw.record_decision_completed

    def _spy_close(**kwargs: Any) -> Any:
        closes.append(kwargs)
        return real_close(**kwargs)

    monkeypatch.setattr(jw, "record_decision_completed", _spy_close)

    action_id = await _seed_rule_action("ws-A")

    user = _FakeUser("user-A", "ws-A")
    with patch("pocketpaw_ee.instinct.router._store", return_value=shared_store):
        async with await _async_router_client(shared_store, user, monkeypatch) as client:
            resp = await client.post(
                f"/instinct/actions/{action_id}/reject", json={"reason": "not now"}
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "rejected"

    # NO rule was written — reject never runs the executor.
    assert await rules_service.get_active_rules("ws-A") == []

    # Exactly ONE decision.completed — the router's rejected close.
    assert len(closes) == 1
    assert closes[0]["payload"]["passed"] is False
    assert closes[0]["payload"]["action_outcome"] == "rejected"


# ===========================================================================
# MIXED BATCH — kinds never cross-fire. An _instinct_rule item routes to the rule
# executor / rule close; a sibling _pocket_write item routes to its own path.
# ===========================================================================


async def test_bulk_approve_mixed_batch_routes_each_kind(
    shared_store: InstinctStore, beanie_test_db, monkeypatch
) -> None:
    """A bulk-approve of a batch mixing an ``_instinct_rule`` and a ``_pocket_write``
    lands the rule (rule executor fired) and does NOT mis-route the pocket-write through
    the rule path. The pocket-write executor is patched off (its real apply needs pocket
    creds); we assert it was invoked with the right action and the rule landed once."""
    from pocketpaw_ee.cloud.rules import service as rules_service

    pocket_write_calls: list[Any] = []

    async def _fake_execute_write(action: Any) -> None:
        pocket_write_calls.append(action)

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.instinct_bridge.execute_approved_write",
        _fake_execute_write,
    )

    rule_action = await _seed_rule_action("ws-A", name="mixed-rule")

    user = _FakeUser("user-A", "ws-A")
    with patch("pocketpaw_ee.instinct.router._store", return_value=shared_store):
        async with await _async_router_client(shared_store, user, monkeypatch) as client:
            # Seed the sibling pocket-write over the same async client.
            pw_resp = await client.post(
                "/instinct/actions",
                json={
                    "pocket_id": "pocket-A",
                    "title": "ws-A write",
                    "trigger": TRIGGER,
                    "parameters": _pocket_write_params("ws-A"),
                },
            )
            assert pw_resp.status_code == 201, pw_resp.text
            pw_action = pw_resp.json()["id"]

            resp = await client.post(
                "/instinct/actions/bulk-approve", json={"ids": [rule_action, pw_action]}
            )
            assert resp.status_code == 200, resp.text
            assert len(resp.json()["affected"]) == 2

    # The rule landed exactly once (the rule executor fired on the rule item only).
    active = await rules_service.get_active_rules("ws-A")
    assert len(active) == 1
    assert active[0]["name"] == "mixed-rule"

    # The pocket-write item routed to ITS OWN executor — not the rule path.
    assert len(pocket_write_calls) == 1


async def test_bulk_reject_mixed_batch_routes_each_kind(
    shared_store: InstinctStore, beanie_test_db, monkeypatch
) -> None:
    """A bulk-reject of a mixed batch closes each kind on its own reject branch: the
    ``_instinct_rule`` item writes NO rule and the batch flips both items to rejected.
    Each kind's reject branch ``continue``s, so neither leaks into the other."""
    from pocketpaw_ee.cloud.rules import service as rules_service

    rule_action = await _seed_rule_action("ws-A", name="reject-rule")

    user = _FakeUser("user-A", "ws-A")
    with patch("pocketpaw_ee.instinct.router._store", return_value=shared_store):
        async with await _async_router_client(shared_store, user, monkeypatch) as client:
            pw_resp = await client.post(
                "/instinct/actions",
                json={
                    "pocket_id": "pocket-A",
                    "title": "ws-A write",
                    "trigger": TRIGGER,
                    "parameters": _pocket_write_params("ws-A"),
                },
            )
            assert pw_resp.status_code == 201, pw_resp.text
            pw_action = pw_resp.json()["id"]

            resp = await client.post(
                "/instinct/actions/bulk-reject",
                json={"ids": [rule_action, pw_action], "reason": "batch reject"},
            )
            assert resp.status_code == 200, resp.text
            affected = {a["id"] for a in resp.json()["affected"]}
            assert affected == {rule_action, pw_action}

            # Both flipped to rejected.
            listing = await client.get("/instinct/actions", params={"limit": 500})
            by_id = {a["id"]: a["status"] for a in listing.json()["actions"]}
    assert by_id[rule_action] == "rejected"
    assert by_id[pw_action] == "rejected"

    # No rule was written on the reject path.
    assert await rules_service.get_active_rules("ws-A") == []
