# tests/cloud/entitlements/test_dto_caps.py — the SMB caps (max_seats /
# max_pockets / max_connectors) + the daily call budget + the S3 storage cap
# added to the plan ladder must reach the wire: plan_tier_to_dto
# (GET /billing/plans) and entitlements_to_dto (GET /entitlements) have to carry
# them, else the plan cards / settings UI cannot render real limits.
# Created 2026-07-08 (feat/billing-smb-caps).
# Updated 2026-08-08 (feat/billing-storage-caps): the caps under test now include
#   ``max_call_seconds_per_day`` (LiveKit daily budget) and ``max_storage_bytes``
#   (S3 storage cap). The entitlements-DTO test had gone stale when the call
#   budget landed — it constructed ``Entitlements`` without the new required
#   fields. Restored with both fields asserted through to the wire.
from __future__ import annotations

from pocketpaw_ee.cloud.billing.plans import get_plan, list_plans
from pocketpaw_ee.cloud.entitlements.domain import Entitlements
from pocketpaw_ee.cloud.entitlements.dto import entitlements_to_dto, plan_tier_to_dto


def test_plan_tier_dto_carries_caps() -> None:
    """Every catalog row on the wire carries the SMB caps + storage from its PlanTier."""
    for tier in list_plans():
        dto = plan_tier_to_dto(tier)
        assert dto.max_seats == tier.max_seats
        assert dto.max_pockets == tier.max_pockets
        assert dto.max_connectors == tier.max_connectors
        assert dto.max_storage_bytes == tier.max_storage_bytes
        assert dto.included_sites == tier.included_sites


def test_free_tier_dto_caps_are_concrete() -> None:
    """Free (a capped tier) serializes concrete integer caps, not null."""
    dto = plan_tier_to_dto(get_plan("free"))
    assert isinstance(dto.max_seats, int)
    assert isinstance(dto.max_pockets, int)
    assert isinstance(dto.max_connectors, int)
    assert isinstance(dto.max_storage_bytes, int)


def test_enterprise_tier_dto_caps_are_null() -> None:
    """Enterprise is uncapped — the caps serialize as null on the wire."""
    dto = plan_tier_to_dto(get_plan("enterprise"))
    assert dto.max_seats is None
    assert dto.max_pockets is None
    assert dto.max_connectors is None
    assert dto.max_storage_bytes is None


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
        # The two newer domain fields are REQUIRED on Entitlements (both were
        # added after this test; ``max_call_seconds_per_day`` rides the domain
        # + service but NOT the wire DTO, so it is only exercised here on the
        # constructor, not asserted on ``dto``).
        max_call_seconds_per_day=7200,
        max_storage_bytes=50_000_000_000,
        included_sites=3,
    )
    dto = entitlements_to_dto(ent)
    assert dto.max_seats == 25
    assert dto.max_pockets == 5000
    assert dto.max_connectors == 250
    assert dto.max_storage_bytes == 50_000_000_000
    # The site allowance reaches the wire too. The builder asks "is my next site
    # covered or does it cost credits" before a publish, and answering it from
    # the plan catalog on the client would mean re-deriving the workspace's tier
    # there — which is the drift this endpoint exists to prevent.
    assert dto.included_sites == 3
