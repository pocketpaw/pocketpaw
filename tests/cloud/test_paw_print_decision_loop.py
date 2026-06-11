# tests/cloud/test_paw_print_decision_loop.py — gap2: the closed customer
# decision loop, end to end.
# Created: 2026-06-11 (gap2) — Proves the loop the module promised but never
# wired: a customer event raises a PENDING Instinct proposal + parked decision;
# approving it delivers the reply back, retrievable for that (widget, customer);
# rejecting it declines with the reason. Also covers tenancy (the proposal is
# workspace-scoped to the widget owner), the public poll endpoint (CORS-gated),
# and the best-effort guarantee (a loop failure never fails ingest).
#
# asyncio_mode = "auto" (see pyproject) → every ``async def test_*`` is run by
# pytest-asyncio without a per-test marker. The TestClient calls are synchronous
# (Starlette spins its own loop), so mixing sync HTTP calls with awaited store
# reads inside one async test is fine and matches the repo's existing pattern.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.paw_print.decision_loop import (
    deliver_customer_decision,
    propose_customer_decision,
)
from pocketpaw_ee.paw_print.router import router

from pocketpaw.instinct.store import InstinctStore
from pocketpaw.paw_print.models import (
    PawPrintBlock,
    PawPrintEvent,
    PawPrintSpec,
    PawPrintWidget,
)
from pocketpaw.paw_print.store import PawPrintStore


def _spec(widget_id: str = "pp_test", pocket_id: str = "pocket-1") -> PawPrintSpec:
    return PawPrintSpec(
        widget_id=widget_id,
        pocket_id=pocket_id,
        blocks=[PawPrintBlock(type="text", content="Bright Smile Dental")],
    )


def _widget_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pocket_id": "pocket-1",
        "owner": "ws:bright-smile",
        "name": "Appointment Widget",
        "spec": _spec().model_dump(),
        "allowed_domains": ["brightsmiledental.com"],
        "rate_limit_per_min": 20,
        "per_customer_limit_per_min": 10,
        "event_mapping": {
            "appointment_request": {
                "creates": "AppointmentRequest",
                "fields": {"when": "{{ payload.when }}", "patient": "{{ customer_ref }}"},
            },
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Wire isolated Instinct + PawPrint stores and patch the singletons.

    The decision_loop functions lazy-import ``get_instinct_store`` /
    ``get_paw_print_store`` from ``pocketpaw.stores``, so patching there reaches
    both the ingest (propose) and the approve (deliver) sides.
    """
    pp_store = PawPrintStore(tmp_path / "paw_print_loop.db")
    instinct_store = InstinctStore(tmp_path / "instinct_loop.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: instinct_store)
    monkeypatch.setattr("pocketpaw.stores.get_paw_print_store", lambda: pp_store)
    return pp_store, instinct_store


@pytest.fixture
def client(stores, monkeypatch):
    pp_store, _ = stores
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr("pocketpaw_ee.paw_print.router._store", lambda: pp_store)
    return TestClient(app)


def _create_widget(client: TestClient, **overrides: Any) -> dict[str, Any]:
    res = client.post("/paw-print/widgets", json=_widget_payload(**overrides))
    assert res.status_code == 201, res.text
    return res.json()


def _ingest(client: TestClient, widget_id: str, customer_ref: str = "patient_42") -> dict[str, Any]:
    res = client.post(
        f"/paw-print/events/{widget_id}",
        json={
            "type": "appointment_request",
            "payload": {"when": "Tuesday 3pm"},
            "customer_ref": customer_ref,
        },
        headers={"Origin": "https://brightsmiledental.com"},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _poll(client: TestClient, widget_id: str, customer_ref: str = "patient_42"):
    return client.get(
        f"/paw-print/events/{widget_id}/decision/{customer_ref}",
        headers={"Origin": "https://brightsmiledental.com"},
    )


# ---------------------------------------------------------------------------
# Open-the-loop: a mapped event raises a proposal + parks a PENDING decision
# ---------------------------------------------------------------------------


class TestOpenLoop:
    async def test_mapped_event_raises_instinct_proposal(self, client, stores) -> None:
        _, instinct_store = stores
        created = _create_widget(client)
        body = _ingest(client, created["id"])

        action_id = body["instinct_action_id"]
        assert action_id, "ingest of a mapped event must raise an Instinct proposal"

        # The proposal lands in the OWNER's workspace-scoped pending list.
        pending = await instinct_store.pending(workspace_id="ws:bright-smile")
        assert len(pending) == 1
        assert pending[0].id == action_id
        blob = pending[0].parameters["_customer_reply"]
        assert blob["widget_id"] == created["id"]
        assert blob["customer_ref"] == "patient_42"
        assert blob["event_type"] == "appointment_request"
        assert blob["workspace_id"] == "ws:bright-smile"

    async def test_proposal_is_workspace_scoped(self, client, stores) -> None:
        """A different tenant must NOT see this proposal (deny-by-default)."""
        _, instinct_store = stores
        created = _create_widget(client)
        _ingest(client, created["id"])

        other = await instinct_store.pending(workspace_id="ws:someone-else")
        assert other == []

    async def test_decision_parked_pending_after_ingest(self, client) -> None:
        created = _create_widget(client)
        _ingest(client, created["id"])

        # The customer surface polls and sees "we're looking into it".
        res = _poll(client, created["id"])
        assert res.status_code == 200
        body = res.json()
        assert body["found"] is True
        assert body["state"] == "pending"
        assert body["reply"] == ""

    async def test_unmapped_event_does_not_raise_proposal(self, client, stores) -> None:
        """Only mapped events open the loop — telemetry must not flood The Tray."""
        _, instinct_store = stores
        created = _create_widget(client)
        res = client.post(
            f"/paw-print/events/{created['id']}",
            json={"type": "page_view", "payload": {}, "customer_ref": "patient_42"},
            headers={"Origin": "https://brightsmiledental.com"},
        )
        assert res.status_code == 200
        assert res.json()["instinct_action_id"] is None
        assert await instinct_store.pending(workspace_id="ws:bright-smile") == []


# ---------------------------------------------------------------------------
# Close-the-loop: approving delivers the reply; rejecting declines
# ---------------------------------------------------------------------------


class TestCloseLoop:
    async def test_approval_delivers_decision_back(self, client, stores) -> None:
        _, instinct_store = stores
        created = _create_widget(client)
        body = _ingest(client, created["id"])
        action_id = body["instinct_action_id"]

        # The human approves on the Instinct surface (store-level approve, then
        # the delivery hook the router calls).
        approved = await instinct_store.approve(action_id, approver="user:dr_jones")
        assert approved is not None
        await deliver_customer_decision(approved, declined=False)

        # The customer polls and now reads the DELIVERED reply.
        res = _poll(client, created["id"])
        assert res.status_code == 200
        out = res.json()
        assert out["found"] is True
        assert out["state"] == "delivered"
        assert out["reply"]  # non-empty operator reply
        assert out["decided_by"] == "user:dr_jones"

    async def test_edited_reply_is_what_customer_reads(self, client, stores) -> None:
        """If the operator edits the recommendation, that wording is delivered."""
        import aiosqlite

        _, instinct_store = stores
        created = _create_widget(client)
        body = _ingest(client, created["id"])
        action_id = body["instinct_action_id"]

        # Operator edits the recommendation before approving (the delivery hook
        # prefers the approved Action's recommendation as the customer reply).
        edited = "Confirmed for Tuesday 3pm — see you then!"
        async with aiosqlite.connect(instinct_store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET recommendation = ? WHERE id = ?",
                (edited, action_id),
            )
            await db.commit()

        approved = await instinct_store.approve(action_id, approver="user:dr_jones")
        await deliver_customer_decision(approved, declined=False)

        res = _poll(client, created["id"])
        assert res.json()["reply"] == edited

    async def test_rejection_declines_with_reason(self, client, stores) -> None:
        _, instinct_store = stores
        created = _create_widget(client)
        body = _ingest(client, created["id"])
        action_id = body["instinct_action_id"]

        rejected = await instinct_store.reject(
            action_id,
            reason="No slots Tuesday — please pick another day",
            rejector="user:dr_jones",
        )
        assert rejected is not None
        await deliver_customer_decision(rejected, declined=True)

        res = _poll(client, created["id"])
        out = res.json()
        assert out["state"] == "declined"
        assert "No slots Tuesday" in out["reply"]


# ---------------------------------------------------------------------------
# Robustness: best-effort + CORS on the poll endpoint
# ---------------------------------------------------------------------------


class TestRobustness:
    async def test_poll_unknown_customer_returns_not_found(self, client) -> None:
        created = _create_widget(client)
        res = _poll(client, created["id"], customer_ref="never_seen")
        assert res.status_code == 200
        assert res.json()["found"] is False

    async def test_poll_disallowed_origin_rejected(self, client) -> None:
        created = _create_widget(client)
        _ingest(client, created["id"])
        res = client.get(
            f"/paw-print/events/{created['id']}/decision/patient_42",
            headers={"Origin": "https://evil.example"},
        )
        assert res.status_code == 403

    async def test_proposal_failure_does_not_break_ingest(self, stores, monkeypatch) -> None:
        """If the Instinct store explodes, the loop swallows it — the ingest
        response is never broken by a decision-loop failure."""
        pp_store, _ = stores
        widget = PawPrintWidget(
            pocket_id="pocket-1",
            owner="ws:bright-smile",
            name="W",
            spec=_spec(),
            event_mapping={},
        )
        event = PawPrintEvent(
            widget_id=widget.id,
            type="appointment_request",
            payload={},
            customer_ref="p1",
        )

        def _boom():
            raise RuntimeError("instinct store down")

        monkeypatch.setattr("pocketpaw.stores.get_instinct_store", _boom)
        result = await propose_customer_decision(
            widget=widget, event=event, paw_print_store=pp_store
        )
        assert result is None

    async def test_deliver_no_parked_row_is_noop(self, stores) -> None:
        """Delivering for an action with no parked decision row degrades cleanly."""
        from pocketpaw.instinct.models import ActionCategory, ActionTrigger

        _, instinct_store = stores
        action = await instinct_store.propose(
            pocket_id="pocket-1",
            title="orphan",
            description="",
            recommendation="hi",
            trigger=ActionTrigger(type="connector", source="x", reason="y"),
            category=ActionCategory.EXTERNAL,
            parameters={
                "_customer_reply": {
                    "schema": 1,
                    "widget_id": "pp_x",
                    "customer_ref": "c1",
                    "event_type": "t",
                    "workspace_id": "ws:bright-smile",
                    "default_reply": "ok",
                }
            },
        )
        approved = await instinct_store.approve(action.id, approver="u")
        # No create_decision was called → set_decision returns None → no raise.
        await deliver_customer_decision(approved, declined=False)
