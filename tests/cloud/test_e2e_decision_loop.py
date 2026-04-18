# test_e2e_decision_loop.py — E2E: Fabric objects → Instinct pipeline → Audit export.
# Created: 2026-03-28
# Tests the full decision loop without any HTTP calls:
#   define object type → create objects → detect low stock (threshold check)
#   → propose actions → approve/reject → audit trail → JSON export.

"""E2E test: The full decision loop.
Fabric objects → agent queries → proposes action → approves → audit logged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ee.fabric.models import FabricQuery, PropertyDef
from ee.fabric.store import FabricStore
from ee.instinct.models import ActionTrigger
from ee.instinct.store import InstinctStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_trigger(reason: str = "Low stock detected") -> ActionTrigger:
    return ActionTrigger(type="agent", source="pocketpaw", reason=reason)


async def _evaluate_threshold(
    store: FabricStore, type_name: str, property_name: str, operator: str, threshold: float
) -> list:
    """Evaluate a threshold rule against Fabric objects — inline evaluator.

    Replaces the not-yet-implemented ee/automations evaluator.  Returns a list
    of FabricObject instances that satisfy the rule.
    """
    result = await store.query(FabricQuery(type_name=type_name))
    fired = []
    for obj in result.objects:
        val = obj.properties.get(property_name)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        match = False
        if operator == "lt" and val < threshold:
            match = True
        elif operator == "lte" and val <= threshold:
            match = True
        elif operator == "gt" and val > threshold:
            match = True
        elif operator == "gte" and val >= threshold:
            match = True
        elif operator == "eq" and val == threshold:
            match = True
        if match:
            fired.append(obj)
    return fired


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_decision_loop(tmp_path: Path) -> None:
    """Step-by-step decision loop: Fabric → threshold rule → Instinct → audit export."""
    fabric = FabricStore(tmp_path / "fabric.db")
    instinct = InstinctStore(tmp_path / "instinct.db")

    # --- Step 1: Define object type with properties ---
    inv_type = await fabric.define_type(
        name="Inventory",
        properties=[
            PropertyDef(name="name", type="string", required=True),
            PropertyDef(name="quantity", type="number", required=True),
        ],
        icon="box",
        color="#FF6B35",
    )
    assert inv_type.id.startswith("ot-")
    assert inv_type.name == "Inventory"
    assert len(inv_type.properties) == 2

    # --- Step 2: Create 3 inventory objects ---
    _oat_milk = await fabric.create_object(inv_type.id, {"name": "Oat Milk", "quantity": 4})
    _coffee = await fabric.create_object(inv_type.id, {"name": "Coffee Beans", "quantity": 50})
    _cups = await fabric.create_object(inv_type.id, {"name": "Cups", "quantity": 200})

    all_inv = await fabric.query(FabricQuery(type_name="Inventory"))
    assert all_inv.total == 3

    # --- Step 3: Query for low stock (qty < 10) ---
    low_stock = await _evaluate_threshold(fabric, "Inventory", "quantity", "lt", 10)
    # Only Oat Milk (qty=4) triggers; Coffee (50) and Cups (200) don't.
    assert len(low_stock) == 1
    assert low_stock[0].properties["name"] == "Oat Milk"

    # --- Step 4: Propose an Instinct action for each triggered object ---
    proposed_actions = []
    for obj in low_stock:
        qty = obj.properties["quantity"]
        action = await instinct.propose(
            pocket_id="store-hq",
            title=f"Reorder {obj.properties['name']}",
            description=f"Stock at {qty} units (threshold: 10)",
            recommendation=f"Order 20 units of {obj.properties['name']}",
            trigger=_agent_trigger(f"Quantity {qty} < 10"),
        )
        proposed_actions.append(action)

    # --- Step 5: Verify pending actions exist ---
    pending = await instinct.pending()
    assert len(pending) == len(proposed_actions)
    for action in pending:
        assert action.status.value == "pending"

    # We also need a second action to reject — propose one more
    second_action = await instinct.propose(
        pocket_id="store-hq",
        title="Deep-clean espresso machine",
        description="Scheduled maintenance",
        recommendation="Run descaling cycle",
        trigger=_agent_trigger("Scheduled maintenance"),
    )
    assert second_action.status.value == "pending"

    # --- Step 6: Approve one, reject one ---
    approved = await instinct.approve(proposed_actions[0].id, "user:owner")
    assert approved is not None
    assert approved.status.value == "approved"
    assert approved.approved_by == "user:owner"

    rejected = await instinct.reject(second_action.id, "Not urgent right now")
    assert rejected is not None
    assert rejected.status.value == "rejected"
    assert rejected.rejected_reason == "Not urgent right now"

    # --- Step 7: Verify audit log has all expected events ---
    audit_entries = await instinct.query_audit(pocket_id="store-hq")
    events = [e.event for e in audit_entries]
    assert "action_proposed" in events, "Expected action_proposed in audit"
    assert "action_approved" in events, "Expected action_approved in audit"
    assert "action_rejected" in events, "Expected action_rejected in audit"

    # --- Step 8: Mark approved action as executed ---
    executed = await instinct.mark_executed(approved.id, "Order placed with SupplierCo")
    assert executed is not None
    assert executed.status.value == "executed"
    assert executed.outcome == "Order placed with SupplierCo"

    # --- Step 9: Export audit as JSON and verify completeness ---
    audit_json = await instinct.export_audit("store-hq")
    parsed = json.loads(audit_json)

    exported_events = [e["event"] for e in parsed]
    assert "action_proposed" in exported_events
    assert "action_approved" in exported_events
    assert "action_rejected" in exported_events
    assert "action_executed" in exported_events

    # Confirm structure — every entry has required fields
    for entry in parsed:
        assert "id" in entry
        assert "actor" in entry
        assert "event" in entry
        assert "description" in entry
        assert "timestamp" in entry


@pytest.mark.asyncio
async def test_multiple_low_stock_items_all_get_actions(tmp_path: Path) -> None:
    """When multiple items breach the threshold, all of them get proposed actions."""
    fabric = FabricStore(tmp_path / "fabric.db")
    instinct = InstinctStore(tmp_path / "instinct.db")

    inv_type = await fabric.define_type(
        name="Inventory",
        properties=[
            PropertyDef(name="name", type="string"),
            PropertyDef(name="quantity", type="number"),
        ],
    )

    # Three items below threshold, one above
    items = [
        {"name": "Milk", "quantity": 2},
        {"name": "Sugar", "quantity": 5},
        {"name": "Salt", "quantity": 8},
        {"name": "Coffee", "quantity": 100},
    ]
    for props in items:
        await fabric.create_object(inv_type.id, props)

    low_stock = await _evaluate_threshold(fabric, "Inventory", "quantity", "lt", 10)
    assert len(low_stock) == 3  # Milk, Sugar, Salt

    names = {obj.properties["name"] for obj in low_stock}
    assert names == {"Milk", "Sugar", "Salt"}

    # Propose actions for all
    for obj in low_stock:
        await instinct.propose(
            pocket_id="store-hq",
            title=f"Reorder {obj.properties['name']}",
            description="Low stock",
            recommendation="Order 20 units",
            trigger=_agent_trigger(),
        )

    pending_count = await instinct.pending_count()
    assert pending_count == 3


@pytest.mark.asyncio
async def test_approved_action_audit_contains_approver(tmp_path: Path) -> None:
    """Audit trail captures who approved the action."""
    instinct = InstinctStore(tmp_path / "instinct.db")

    action = await instinct.propose(
        pocket_id="p1",
        title="Test approval tracking",
        description="",
        recommendation="Do something",
        trigger=ActionTrigger(type="agent", source="pocketpaw", reason="test"),
    )

    await instinct.approve(action.id, "user:jane")

    entries = await instinct.query_audit(pocket_id="p1", event="action_approved")
    assert len(entries) == 1
    assert entries[0].actor == "user:jane"
