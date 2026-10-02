# tests/cloud/test_proposal_chain_characterization.py — pins the Decision-Graph
# chain behaviour of the gated propose paths so the shared proposal-chain helper
# (CN-5, ``cloud/_core/proposals.py``) can replace the per-module copies of
# ``_emit_agent_proposed`` + ``_persist_chain_ids`` without changing a byte of
# what lands in the journal or on the stored Action.
#
# Created: 2026-10-01 (refactor/canon-proposal-helpers, CN-5) — written against the
#   unrefactored code first, then kept green through the refactor.
#
# Updated: 2026-10-01 (CN-5 review) — every migrated propose module is pinned now:
#   pocket, fabric-objects, instinct-rule, external-action, site-plan (the only
#   non-workspace ``pocket_id``) and fabric-conflict joined admin/ship/growth. The
#   belt MCP server (``extra`` summary + ``record_decision``) is pinned in
#   test_belt_trace.py, which owns the git/allowlist fixtures it needs.
#
# Per module (admin_proposals = reference, ship + growth = the overwrite variant):
#   * ``record_agent_proposed`` is called once with the exact actor, scope and
#     payload;
#   * the stored ``parameters`` dict is exactly ``{KEY: blob}`` with
#     ``correlation_id`` == the chain id and ``proposed_event_id`` == the emitted
#     event id;
#   * an emit failure never fails the propose and leaves ``proposed_event_id`` None.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

EVENT_ID = UUID("11111111-2222-3333-4444-555555555555")
CORR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "proposal-chain-characterization-secret")

    async def _choice(_fabric, stmt, _ws):
        return {"statement_id": stmt.id, "value": stmt.id}

    monkeypatch.setattr("pocketpaw_ee.cloud.fabric_conflicts.propose._statement_choice", _choice)


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_chain_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture
def emits(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=EVENT_ID)

    monkeypatch.setattr("pocketpaw_ee.cloud.decisions.journal_writer.record_agent_proposed", _spy)
    return calls


def _boom(**_kwargs):
    raise RuntimeError("journal down")


def _actor_dict(actor) -> dict[str, Any]:
    return {"kind": actor.kind, "id": actor.id, "scope_context": list(actor.scope_context)}


async def _propose_admin() -> str:
    from pocketpaw_ee.cloud.admin_proposals.propose import propose_admin_action

    return await propose_admin_action(
        workspace_id="ws-1",
        action="workspace.member.role_change",
        args={"target_user_id": "u-2", "role": "admin"},
        proposer_user_id="u-1",
        correlation_id=CORR,
    )


async def _propose_ship() -> str:
    from pocketpaw_ee.cloud.ship.propose import propose_ship_action

    return await propose_ship_action(
        workspace_id="ws-1",
        verb="destroy_app",
        app_id="app-9",
        box_id="box-3",
        target_label="app demo",
        requested_by="u-1",
        correlation_id=CORR,
    )


async def _propose_growth() -> str:
    from pocketpaw_ee.cloud.growth.propose import propose_growth_send

    return await propose_growth_send(
        workspace_id="ws-1",
        draft_id="d-1",
        prospect_id="p-1",
        channel="email",
        prospect_name="Ada",
        prospect_company="Acme",
        preview_subject="Hi",
        preview_body="Hello there",
        requested_by="u-1",
        correlation_id=CORR,
    )


async def _propose_pocket() -> str:
    from pocketpaw_ee.cloud.pocket_proposals.propose import propose_pocket

    return await propose_pocket(
        workspace_id="ws-1",
        user_id="u-1",
        ripple_spec={"version": "1.0", "root": {"id": "root", "type": "container", "children": []}},
        name="Starter dashboard",
        correlation_id=CORR,
    )


async def _propose_fabric_objects() -> str:
    from pocketpaw_ee.cloud.fabric_proposals.propose import propose_fabric_objects

    return await propose_fabric_objects(
        workspace_id="ws-1",
        object_types=[
            {"type_name": "Customer", "properties": [{"name": "name", "type": "string"}]},
            {"type_name": "Order", "properties": [{"name": "total", "type": "number"}]},
        ],
        objects=[
            {
                "type_name": "Customer",
                "properties": {"name": "Acme"},
                "source_connector": "crm",
                "source_id": "c-1",
            },
            {
                "type_name": "Order",
                "properties": {"total": 42},
                "source_connector": "billing",
                "source_id": "o-9",
            },
        ],
        links=[
            {
                "from": {"source_connector": "billing", "source_id": "o-9"},
                "to": {"source_connector": "crm", "source_id": "c-1"},
                "link_type": "placed_by",
            }
        ],
        requested_by="u-1",
        correlation_id=CORR,
    )


async def _propose_instinct_rule() -> str:
    from pocketpaw_ee.cloud.instinct_rule_proposals.propose import propose_instinct_rule

    return await propose_instinct_rule(
        workspace_id="ws-1",
        user_id="u-1",
        rule_spec={
            "name": "Approve big invoices",
            "description": "Flag invoices over 10k.",
            "when": "object.amount > 10000",
            "action": "require_approval",
            "scope": {"workspace_id": "ws-1", "object_type": "Invoice"},
            "confidence": 0.8,
            "provenance": ["audit:row-1"],
        },
        correlation_id=CORR,
    )


async def _propose_external() -> str:
    from pocketpaw_ee.cloud.external_actions.propose import propose_external_action

    return await propose_external_action(
        workspace_id="ws-1",
        connector_name="gmail",
        action="send_email",
        params={"to": "a@b.test"},
        requested_by="u-1",
        correlation_id=CORR,
    )


async def _propose_site_plan() -> str:
    from pocketpaw_ee.cloud.site_plan_requests.propose import propose_site_plan_request

    return await propose_site_plan_request(
        workspace_id="ws-1",
        pocket_id="pkt-1",
        site_plan_key="staff",
        requested_by="u-1",
        correlation_id=CORR,
    )


async def _propose_fabric_conflict() -> str:
    from pocketpaw_ee.cloud.fabric_conflicts.propose import propose_fabric_conflict

    conflict = SimpleNamespace(
        winner=SimpleNamespace(id="s-1"),
        rivals=[SimpleNamespace(id="s-2")],
        object_id="o-1",
        object_type="Customer",
        property="arr",
        signature=("s-1", "s-2"),
    )
    return await propose_fabric_conflict(
        workspace_id="ws-1",
        conflict=conflict,
        requested_by="u-1",
        correlation_id=CORR,
        fabric_store=object(),
    )


def _ws_payload(kind: str, intent: str, proposal: dict[str, Any], pocket_id: str = "ws-1"):
    return {
        "intent": intent,
        "action": kind,
        "pocket_id": pocket_id,
        "inputs": [],
        "proposal_kind": kind,
        "proposal": proposal,
    }


CASES = {
    "admin": (
        _propose_admin,
        "_admin_action",
        {
            "intent": "workspace-admin action 'workspace.member.role_change'",
            "action": "admin_action",
            "pocket_id": "ws-1",
            "inputs": [],
            "proposal_kind": "admin_action",
            "proposal": {"rbac_action": "workspace.member.role_change"},
        },
    ),
    "ship": (
        _propose_ship,
        "_ship_action",
        {
            "intent": "destroy app on app demo",
            "action": "ship_action",
            "pocket_id": "ws-1",
            "inputs": [],
            "proposal_kind": "ship_action",
            "proposal": {"verb": "destroy_app", "target": "app demo"},
        },
    ),
    "growth": (
        _propose_growth,
        "_growth_send",
        {
            "intent": "send email outreach to Ada (Acme)",
            "action": "growth_send",
            "pocket_id": "ws-1",
            "inputs": [],
            "proposal_kind": "growth_send",
            "proposal": {"channel": "email", "target": "Ada (Acme)"},
        },
    ),
    "pocket": (
        _propose_pocket,
        "_pocket_create",
        _ws_payload(
            "pocket_create",
            "create the starter Pocket 'Starter dashboard'",
            {"name": "Starter dashboard"},
        ),
    ),
    "fabric_objects": (
        _propose_fabric_objects,
        "_fabric_objects",
        _ws_payload(
            "fabric_objects",
            "create 2 Fabric object(s) across 2 type(s) and 1 link(s)",
            {"type_count": 2, "object_count": 2, "link_count": 1},
        ),
    ),
    "instinct_rule": (
        _propose_instinct_rule,
        "_instinct_rule",
        _ws_payload(
            "instinct_rule",
            "create the governed rule 'Approve big invoices'",
            {"name": "Approve big invoices"},
        ),
    ),
    "external": (
        _propose_external,
        "_external_action",
        _ws_payload(
            "external_action",
            "external action 'send_email' on connector 'gmail'",
            {"connector": "gmail", "connector_action": "send_email"},
        ),
    ),
    "site_plan": (
        _propose_site_plan,
        "_site_plan_request",
        _ws_payload(
            "site_plan_request",
            "put this site on the 'staff' plan",
            {"site_plan_key": "staff", "pocket_id": "pkt-1"},
            pocket_id="pkt-1",
        ),
    ),
    "fabric_conflict": (
        _propose_fabric_conflict,
        "_fabric_conflict",
        _ws_payload(
            "fabric_conflict",
            "arbitrate 2 competing values for Customer.arr",
            {"object_type": "Customer", "property": "arr", "choice_count": 2},
        ),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_emit_payload_and_chain_ids_persisted(case, store, emits):
    propose, key, payload = CASES[case]
    action_id = await propose()

    assert len(emits) == 1
    call = emits[0]
    assert call["correlation_id"] == UUID(CORR)
    assert _actor_dict(call["actor"]) == {
        "kind": "agent",
        "id": "user:u-1",
        "scope_context": ["workspace:ws-1"],
    }
    assert call["scope"] == ["workspace:ws-1"]
    assert call["payload"] == {**payload, "action_id": action_id}
    assert set(call) == {"correlation_id", "actor", "scope", "payload"}

    action = await store.get_action(action_id)
    assert list(action.parameters) == [key]
    blob = action.parameters[key]
    assert blob["correlation_id"] == CORR
    assert blob["proposed_event_id"] == str(EVENT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_emit_failure_is_best_effort(case, store, monkeypatch):
    monkeypatch.setattr("pocketpaw_ee.cloud.decisions.journal_writer.record_agent_proposed", _boom)
    propose, key, _ = CASES[case]
    action_id = await propose()

    blob = (await store.get_action(action_id)).parameters[key]
    assert blob["correlation_id"] == CORR
    assert blob["proposed_event_id"] is None
