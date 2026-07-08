# tests/cloud/entitlements/test_dto_caps.py — the SMB caps (max_seats /
# max_pockets / max_connectors) added to the plan ladder must reach the wire:
# plan_tier_to_dto (GET /billing/plans) and entitlements_to_dto (GET /entitlements)
# have to carry them, else the plan cards / settings UI cannot render real limits.
# Created 2026-07-08 (feat/billing-smb-caps).
from __future__ import annotations

from pocketpaw_ee.cloud.billing.plans import get_plan, list_plans
from pocketpaw_ee.cloud.entitlements.domain import Entitlements
from pocketpaw_ee.cloud.entitlements.dto import entitlements_to_dto, plan_tier_to_dto


def test_plan_tier_dto_carries_caps() -> None:
    """Every catalog row on the wire carries the three caps from its PlanTier."""
    for tier in list_plans():
        dto = plan_tier_to_dto(tier)
        assert dto.max_seats == tier.max_seats
        assert dto.max_pockets == tier.max_pockets
        assert dto.max_connectors == tier.max_connectors


def test_free_tier_dto_caps_are_concrete() -> None:
    """Free (a capped tier) serializes concrete integer caps, not null."""
    dto = plan_tier_to_dto(get_plan("free"))
    assert isinstance(dto.max_seats, int)
    assert isinstance(dto.max_pockets, int)
    assert isinstance(dto.max_connectors, int)


def test_enterprise_tier_dto_caps_are_null() -> None:
    """Enterprise is uncapped — the caps serialize as null on the wire."""
    dto = plan_tier_to_dto(get_plan("enterprise"))
    assert dto.max_seats is None
    assert dto.max_pockets is None
    assert dto.max_connectors is None


def test_entitlements_dto_carries_caps() -> None:
    """A resolved entitlement carries its caps to the /entitlements response."""
    ent = Entitlements(
        workspace_id="ws-1",
        plan="pro",
        monthly_credit_allotment=1000,
        monthly_ceiling=None,
        features=frozenset(),
        max_seats=25,
        max_pockets=5000,
        max_connectors=250,
    )
    dto = entitlements_to_dto(ent)
    assert dto.max_seats == 25
    assert dto.max_pockets == 5000
    assert dto.max_connectors == 250
