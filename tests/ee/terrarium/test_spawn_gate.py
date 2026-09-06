# tests/ee/terrarium/test_spawn_gate.py — reproduction is HUMAN-GATED.
#
# The propose side (a citizen's ``spawn`` verb) must leave a zero-cost ``gate``
# Event and NO child; the approve side (``executor.execute_approved_spawn``)
# mints the child, charges the parent, and re-validates the parent's balance and
# state AT APPROVAL TIME — the Action can sit in the tray while the world moves.

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import executor, service  # noqa: E402
from pocketpaw_ee.terrarium import llm as citizen_llm

from .conftest import create_universe  # noqa: E402


def _action(blob: dict) -> SimpleNamespace:
    return SimpleNamespace(id="act-1", parameters={service.WORLD_SPAWN_PARAM_KEY: blob})


async def _spawn_blob(client, uni_id: str) -> dict:
    citizen = client.get(f"/terrarium/universes/{uni_id}/citizens").json()["citizens"][0]
    return {
        "kind": "world_spawn",
        "schema": service.WORLD_SCHEMA,
        "universe_id": uni_id,
        "parent_id": citizen["id"],
        "parent": citizen["name"],
        "parent_did": citizen["did"],
        "child_name": "Ilo",
        "workspace_id": "ws-terra",
        "requested_by": "u-terra",
    }


# The spawn cost (150) is above the seed endowment (120), so these tests open
# with a richer world — a citizen must be able to AFFORD the request before it
# is worth filing a gate for.
RICH = {"daily": 400, "decay_weekly": 0.0}


async def test_the_spawn_verb_gates_and_creates_no_child(client):
    uni = create_universe(client, founders=1, endowment=RICH)
    citizen_llm.set_mock_decision(
        {"thought": "the world needs more of us", "acts": [{"verb": "spawn", "name": "Ilo"}]}
    )
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    assert res.status_code == 200, res.text

    gates = [e for e in res.json()["events"] if e["kind"] == "gate"]
    assert len(gates) == 1 and gates[0]["cost"] == 0

    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert len(citizens) == 1, "no child exists until a human approves"
    # And no credits moved beyond the think charge — the spawn cost is only
    # taken when a human approves.
    assert citizens[0]["balance"] == RICH["daily"] - 2


async def test_an_approved_spawn_mints_the_child(client):
    uni = create_universe(client, founders=1, endowment=RICH)
    blob = await _spawn_blob(client, uni["id"])

    result = await executor.execute_approved_spawn(_action(blob))
    assert result["ok"] is True, result

    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert len(citizens) == 2
    child = next(c for c in citizens if c["name"] == "Ilo")
    parent = next(c for c in citizens if c["name"] != "Ilo")
    assert child["generation"] == 2
    assert child["parent_did"] == parent["did"]
    assert child["charter"] is None, "the child writes its own charter on its first tick"
    assert child["balance"] > 0
    assert parent["balance"] == RICH["daily"] - 150, "the parent paid the spawn cost"

    events = client.get(f"/terrarium/universes/{uni['id']}/events?limit=500").json()["events"]
    spawned = [e for e in events if e["kind"] == "spawn"]
    assert len(spawned) == 1 and spawned[0]["cost"] == -150


async def test_a_broke_parent_is_re_validated_at_approval_time(client):
    """The Action sat in the tray while the parent spent itself dry."""
    uni = create_universe(client, founders=1, endowment={"daily": 10, "decay_weekly": 0.0})
    blob = await _spawn_blob(client, uni["id"])

    result = await executor.execute_approved_spawn(_action(blob))
    assert result["ok"] is False
    assert "cannot afford" in result["reason"]
    assert len(client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]) == 1


async def test_a_sleeping_parent_cannot_spawn(client):
    uni = create_universe(client, founders=1, endowment={"daily": 3, "decay_weekly": 0.0})
    blob = await _spawn_blob(client, uni["id"])
    citizen_llm.set_mock_decision({"thought": "quiet", "acts": []})
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")

    result = await executor.execute_approved_spawn(_action(blob))
    assert result["ok"] is False and "hibernating" in result["reason"]


async def test_a_cross_workspace_blob_is_refused(client):
    uni = create_universe(client, founders=1)
    blob = await _spawn_blob(client, uni["id"])
    blob["workspace_id"] = "ws-somebody-else"

    result = await executor.execute_approved_spawn(_action(blob))
    assert result["ok"] is False and "workspace mismatch" in result["reason"]


async def test_a_non_spawn_action_is_ignored():
    assert executor.world_spawn_blob(SimpleNamespace(parameters={"_belt_plan": {}})) is None
    assert executor.world_spawn_blob(SimpleNamespace(parameters=None)) is None
    result = await executor.execute_approved_spawn(SimpleNamespace(parameters={}))
    assert result == {"ok": False, "reason": "not a world_spawn action"}
