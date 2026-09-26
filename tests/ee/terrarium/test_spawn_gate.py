# tests/ee/terrarium/test_spawn_gate.py — reproduction is HUMAN-GATED.
#
# The propose side (a citizen's ``spawn`` verb) must leave a zero-cost ``gate``
# Event and NO child; the approve side (``executor.execute_approved_spawn``)
# mints the child, charges the parent, and re-validates the parent's balance and
# state AT APPROVAL TIME — the Action can sit in the tray while the world moves.
# A child's name must be unique in its universe (citizens resolve by name inside
# a tick): refused at filing (a zero-cost gate, no Action) and again at approval.

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


async def test_the_spawn_verb_gates_and_creates_no_child(client, instinct_store):
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

    # The gate is real: a pending world_spawn Action is readable from the store.
    pending = await instinct_store.list_actions()
    spawns = [
        a
        for a in pending
        if isinstance(a.parameters, dict) and service.WORLD_SPAWN_PARAM_KEY in a.parameters
    ]
    assert len(spawns) == 1, "the spawn verb filed no Instinct Action"
    blob = spawns[0].parameters[service.WORLD_SPAWN_PARAM_KEY]
    assert blob["kind"] == "world_spawn"
    assert blob["child_name"] == "Ilo"
    assert blob["parent_id"] == citizens[0]["id"]


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


async def test_a_child_named_after_a_citizen_is_refused_at_filing(client, instinct_store):
    """Settle and the transfers loop resolve citizens by name, so a second
    "Mira" would be paid for the first one's offers. Filing side."""
    uni = create_universe(client, founders=2, endowment=RICH)
    taken = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][1]["name"]
    citizen_llm.set_mock_decision({"thought": "twins", "acts": [{"verb": "spawn", "name": taken}]})
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    assert res.status_code == 200, res.text

    reasons = [
        e["data"]["reason"] for e in res.json()["events"] if (e.get("data") or {}).get("reason")
    ]
    assert reasons.count("name taken") == 2, "both citizens asked, both were refused"
    pending = await instinct_store.list_actions()
    assert not [
        a
        for a in pending
        if isinstance(a.parameters, dict) and service.WORLD_SPAWN_PARAM_KEY in a.parameters
    ], "no Action is filed for a taken name"


async def test_a_child_named_after_a_citizen_is_refused_at_approval(client):
    """Approval side: the Action may predate a founder or child of that name."""
    uni = create_universe(client, founders=2, endowment=RICH)
    blob = await _spawn_blob(client, uni["id"])
    blob["child_name"] = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()[
        "citizens"
    ][1]["name"]

    result = await executor.execute_approved_spawn(_action(blob))
    assert result["ok"] is False and "already a citizen" in result["reason"]
    assert len(client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]) == 2


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


# --- the gate is visible from the world it happened in --------------------
#
# A citizen asking for a child files a real Instinct Action, but until this
# route existed only the tray knew. From the observatory the request was
# invisible, so nobody approved it and no universe ever reached generation 2.


async def test_the_gates_route_shows_a_pending_spawn_for_this_universe(client, instinct_store):
    uni = create_universe(client, founders=1, endowment=RICH)
    citizen_llm.set_mock_decision(
        {"thought": "the world needs another", "acts": [{"verb": "spawn", "name": "Nim"}]}
    )
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")

    gates = client.get(f"/terrarium/universes/{uni['id']}/gates").json()["gates"]

    assert len(gates) == 1, gates
    g = gates[0]
    assert g["kind"] == "world_spawn"
    assert g["child_name"] == "Nim"
    assert g["parent"]
    assert g["action_id"]
    # A viewer reads this room; it must not carry identity or requester ids.
    assert "parent_did" not in g and "requested_by" not in g


async def test_another_universes_gate_never_shows_here(client, instinct_store):
    a = create_universe(client, founders=1, endowment=RICH)
    b = create_universe(client, founders=1, endowment=RICH)
    citizen_llm.set_mock_decision(
        {"thought": "one more", "acts": [{"verb": "spawn", "name": "Kin"}]}
    )
    client.post(f"/terrarium/universes/{a['id']}/tick?n=1")

    assert client.get(f"/terrarium/universes/{b['id']}/gates").json()["gates"] == []


async def test_a_gate_read_failure_degrades_to_empty_not_a_broken_page(client, monkeypatch):
    uni = create_universe(client, founders=1)

    def boom(**_kw):
        raise RuntimeError("instinct store is down")

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", boom)

    res = client.get(f"/terrarium/universes/{uni['id']}/gates")
    assert res.status_code == 200
    assert res.json()["gates"] == []


# --- a child is not its parent shifted ------------------------------------
#
# The first version added a flat +0.08 to every trait, which is a translation
# rather than drift: every child strictly exceeded its parent on all five axes,
# lineages never diverged in shape, and six generations saturated everyone at
# 1.0. That would have quietly emptied the evolution claim while looking fine.


def test_each_trait_drifts_on_its_own_not_all_by_the_same_step():
    parent = {"O": 0.5, "C": 0.5, "E": 0.5, "A": 0.5, "N": 0.5}
    child = executor.child_ocean(parent, parent_did="did:soul:vela-aaa", child_name="Nim")

    deltas = [round(child[t] - parent[t], 6) for t in parent]
    assert len(set(deltas)) > 1, f"every trait moved by the same amount: {deltas}"
    assert any(d < 0 for d in deltas), f"drift only ever went up: {deltas}"


def test_drift_stays_inside_the_width_and_the_scale():
    for n in range(60):
        parent = {"O": 0.99, "C": 0.01, "E": 0.5, "A": 0.0, "N": 1.0}
        child = executor.child_ocean(parent, parent_did=f"did:soul:p-{n}", child_name=f"c{n}")
        for t, v in child.items():
            assert 0.0 <= v <= 1.0, (t, v)
            assert abs(v - parent[t]) <= executor.DRIFT_WIDTH + 1e-9, (t, v, parent[t])


def test_the_same_birth_replays_identically():
    """The engine takes no RNG so a Journal replay reproduces the world."""
    a = executor.child_ocean({"O": 0.4, "C": 0.6}, parent_did="did:soul:x", child_name="Kin")
    b = executor.child_ocean({"O": 0.4, "C": 0.6}, parent_did="did:soul:x", child_name="Kin")
    assert a == b


def test_two_children_of_one_parent_differ_from_each_other():
    parent = {"O": 0.5, "C": 0.5, "E": 0.5, "A": 0.5, "N": 0.5}
    first = executor.child_ocean(parent, parent_did="did:soul:vela", child_name="Nim")
    second = executor.child_ocean(parent, parent_did="did:soul:vela", child_name="Kin")
    assert first != second, "siblings would be identical twins forever"
