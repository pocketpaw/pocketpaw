# test_e2e_connector_to_fabric.py
# E2E: Stripe connector -> Fabric objects -> Automation rule -> Instinct.
# Created: 2026-03-28
# Tests the data ingestion chain:
#   parse stripe.yaml → connect adapter (mock HTTP) → execute list_invoices
#   → create Fabric Invoice objects → run threshold automation → propose Instinct action.
# No real HTTP calls — httpx is patched throughout.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.fabric.models import FabricQuery, PropertyDef
from ee.fabric.store import FabricStore
from ee.instinct.models import ActionTrigger
from ee.instinct.store import InstinctStore
from pocketpaw.connectors.yaml_engine import DirectRESTAdapter, parse_connector_yaml

CONNECTORS_DIR = Path(__file__).parent.parent / "connectors"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_httpx_client(json_data: list) -> MagicMock:
    """Return a patched httpx.AsyncClient whose GET returns the given JSON."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


async def _threshold_check(objects, property_name: str, operator: str, threshold: float) -> list:
    """Inline threshold evaluator (automations module is a placeholder)."""
    fired = []
    for obj in objects:
        val = obj.properties.get(property_name)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if operator == "gt" and val > threshold:
            fired.append(obj)
        elif operator == "lt" and val < threshold:
            fired.append(obj)
        elif operator == "gte" and val >= threshold:
            fired.append(obj)
        elif operator == "lte" and val <= threshold:
            fired.append(obj)
        elif operator == "eq" and val == threshold:
            fired.append(obj)
    return fired


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connector_to_fabric_full_chain(tmp_path: Path) -> None:
    """Full chain: stripe connector → fabric objects → threshold rule → instinct action."""

    # --- Step 1: Load stripe.yaml connector definition ---
    defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
    assert defn.name == "stripe"
    assert any(a["name"] == "list_invoices" for a in defn.actions)

    # --- Step 2: Create DirectRESTAdapter, connect with mock key ---
    adapter = DirectRESTAdapter(defn)
    conn_result = await adapter.connect("pocket-1", {"STRIPE_API_KEY": "sk_test_mock_key"})
    assert conn_result.success is True
    assert "stripe_invoices" in conn_result.tables_created

    # --- Step 3: Mock httpx — return 2 fake invoices ---
    fake_invoices = [
        {"id": "inv_1", "amount_due": 5000, "status": "paid", "customer": "cus_abc"},
        {"id": "inv_2", "amount_due": 3000, "status": "open", "customer": "cus_xyz"},
    ]
    mock_client = _make_mock_httpx_client(fake_invoices)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await adapter.execute("list_invoices", {"limit": 10})

    assert result.success is True
    assert result.data == fake_invoices
    assert result.records_affected == 2

    # --- Step 4: Create Fabric Invoice objects from response ---
    fabric = FabricStore(tmp_path / "fabric.db")
    invoice_type = await fabric.define_type(
        name="Invoice",
        properties=[
            PropertyDef(name="amount_due", type="number"),
            PropertyDef(name="status", type="string"),
            PropertyDef(name="customer", type="string"),
        ],
    )

    fabric_invoices = []
    for invoice in result.data:
        obj = await fabric.create_object(
            invoice_type.id,
            {
                "amount_due": invoice["amount_due"],
                "status": invoice["status"],
                "customer": invoice["customer"],
            },
            source_connector="stripe",
            source_id=invoice["id"],
        )
        fabric_invoices.append(obj)

    # --- Step 5: Query Fabric for type "Invoice" — verify 2 objects ---
    query_result = await fabric.query(FabricQuery(type_name="Invoice"))
    assert query_result.total == 2

    # Verify source tracking is preserved
    source_ids = {obj.source_id for obj in query_result.objects}
    assert source_ids == {"inv_1", "inv_2"}
    for obj in query_result.objects:
        assert obj.source_connector == "stripe"

    # --- Step 6: Create threshold rule on amount_due > 4000 ---
    all_invoice_objects = query_result.objects
    fired = await _threshold_check(all_invoice_objects, "amount_due", "gt", 4000)

    # inv_1 has amount_due=5000, so it fires; inv_2 has 3000, does not
    assert len(fired) == 1
    assert fired[0].source_id == "inv_1"
    assert fired[0].properties["amount_due"] == 5000

    # --- Step 7: Propose Instinct action for the large invoice ---
    instinct = InstinctStore(tmp_path / "instinct.db")
    large_inv = fired[0]
    action = await instinct.propose(
        pocket_id="finance-hq",
        title=f"Review large invoice {large_inv.source_id}",
        description=(
            f"Invoice amount ${large_inv.properties['amount_due'] / 100:.2f} exceeds threshold"
        ),
        recommendation="Review and confirm payment status with accounting team",
        trigger=ActionTrigger(
            type="automation",
            source="threshold-rule",
            reason=f"amount_due {large_inv.properties['amount_due']} > 4000",
        ),
    )

    assert action.id.startswith("act-")
    assert action.status.value == "pending"
    assert "inv_1" in action.title

    # --- Step 8: Verify the full chain in audit log ---
    audit = await instinct.query_audit(pocket_id="finance-hq")
    events = [e.event for e in audit]
    assert "action_proposed" in events

    # Confirm the action trigger reflects the automation source
    fetched_action = await instinct.get_action(action.id)
    assert fetched_action is not None
    assert fetched_action.trigger.type == "automation"
    assert fetched_action.trigger.source == "threshold-rule"


@pytest.mark.asyncio
async def test_connector_not_connected_execute_fails(tmp_path: Path) -> None:
    """Executing an action without connecting first returns a clean error."""
    defn = parse_connector_yaml(CONNECTORS_DIR / "stripe.yaml")
    adapter = DirectRESTAdapter(defn)
    result = await adapter.execute("list_invoices", {})
    assert result.success is False
    assert result.error == "Not connected"


@pytest.mark.asyncio
async def test_open_invoices_do_not_trigger_large_threshold(tmp_path: Path) -> None:
    """Only invoices above the threshold trigger the automation rule."""
    fabric = FabricStore(tmp_path / "fabric.db")
    inv_type = await fabric.define_type(
        name="Invoice",
        properties=[PropertyDef(name="amount_due", type="number")],
    )

    # Create 3 invoices, all below 4000
    for amount in [1000, 2000, 3500]:
        await fabric.create_object(inv_type.id, {"amount_due": amount})

    result = await fabric.query(FabricQuery(type_name="Invoice"))
    fired = await _threshold_check(result.objects, "amount_due", "gt", 4000)
    assert len(fired) == 0


@pytest.mark.asyncio
async def test_fabric_source_deduplication(tmp_path: Path) -> None:
    """The same source_id from the same connector is a distinct object each time it is created.

    Fabric does not auto-deduplicate — callers are responsible for upsert logic.
    This test documents current behaviour: two creates = two objects.
    """
    fabric = FabricStore(tmp_path / "fabric.db")
    inv_type = await fabric.define_type("Invoice", properties=[])

    obj_a = await fabric.create_object(
        inv_type.id, {"amount_due": 5000}, source_connector="stripe", source_id="inv_1"
    )
    obj_b = await fabric.create_object(
        inv_type.id, {"amount_due": 5000}, source_connector="stripe", source_id="inv_1"
    )

    assert obj_a.id != obj_b.id  # Two distinct records

    result = await fabric.query(FabricQuery(type_name="Invoice"))
    assert result.total == 2
