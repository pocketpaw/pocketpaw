# tests/cloud/test_proposal_chain_characterization.py — pins the Decision-Graph
# chain behaviour of the gated propose paths so the shared proposal-chain helper
# (CN-5, ``cloud/_core/proposals.py``) can replace the per-module copies of
# ``_emit_agent_proposed`` + ``_persist_chain_ids`` without changing a byte of
# what lands in the journal or on the stored Action.
#
# Created: 2026-10-01 (refactor/canon-proposal-helpers, CN-5) — written against the
#   unrefactored code first, then kept green through the refactor.
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
