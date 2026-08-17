# tests/cloud/growth/test_upsert_lifecycle.py — what a re-import may and may
# not change.
# Created 2026-08-17 (fix/growth-review-findings, second review pass).
#
# These exist because the first attempt at "a re-import must never resurrect"
# shipped with NO test and was wrong in two ways a review caught:
#
#   * ``whatsapp_number`` was still overwritten unconditionally while
#     ``opted_in`` had become sticky-True, so a sheet carrying a new number and
#     no opt-in column INHERITED the previous person's consent — and the
#     WhatsApp dispatch guard reads exactly that flag. The blanket overwrite it
#     replaced did NOT have that hole, because it reset ``opted_in`` on the
#     same import. A fix must not be more dangerous than the bug it fixes.
#   * the status guard was ``if body.status != "new"``, which protected only a
#     sheet with NO status column — and an operator's working sheet is exactly
#     the one carrying a stale ``in_sequence``.
#
# Both are lifecycle, and lifecycle is not import data.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest

WS = "ws-upsert-lifecycle"


def _req(**over: object) -> CreateProspectRequest:
    base: dict[str, object] = {
        "name": "Priya",
        "company": "Northwind",
        "domain": "northwind.example",
        "source": "manual",
    }
    base.update(over)
    return CreateProspectRequest.model_validate(base)


class TestConsentNeverTransfers:
    """Consent is given by a PERSON on a NUMBER, never by a domain."""

    @pytest.mark.asyncio
    async def test_a_new_number_drops_the_old_consent(self, mongo_db) -> None:
        """Mutation: "consent survives a number change on re-import".

        The founder consented on their mobile. A later sheet points the row at
        an SDR's number with no opt-in column. If the flag survived, a
        business-initiated template would reach someone who never agreed — and
        the Tray card shows channel and copy, never opt-in provenance, so no
        human review catches it.
        """
        created = await growth_service.upsert_by_domain(
            WS, _req(whatsapp_number="+911111111111", opted_in=True)
        )
        assert created.opted_in is True

        moved = await growth_service.upsert_by_domain(WS, _req(whatsapp_number="+919999999999"))
        assert moved.id == created.id
        assert moved.whatsapp_number == "+919999999999"
        assert moved.opted_in is False, "consent must not follow the row to a new number"

    @pytest.mark.asyncio
    async def test_the_same_number_keeps_its_consent(self, mongo_db) -> None:
        """An enrichment pass repeating the number changes nothing about it."""
        await growth_service.upsert_by_domain(
            WS, _req(domain="same.example", whatsapp_number="+911111111111", opted_in=True)
        )
        again = await growth_service.upsert_by_domain(
            WS, _req(domain="same.example", whatsapp_number="+911111111111", company="Renamed")
        )
        assert again.opted_in is True
        assert again.company == "Renamed"

    @pytest.mark.asyncio
    async def test_an_omitted_number_clears_neither_it_nor_the_consent(self, mongo_db) -> None:
        """An email-only enrichment sheet must not blank the WhatsApp channel —
        which would read as a data-entry error rather than an import side
        effect."""
        await growth_service.upsert_by_domain(
            WS, _req(domain="keep.example", whatsapp_number="+911111111111", opted_in=True)
        )
        enriched = await growth_service.upsert_by_domain(
            WS, _req(domain="keep.example", emails=["a@keep.example"])
        )
        assert enriched.whatsapp_number == "+911111111111"
        assert enriched.opted_in is True


class TestAReImportNeverResurrects:
    """Mutation: "a re-import can write status again (resurrects the dead)"."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale_status", ["new", "in_sequence", "qualified", "drafted"])
    async def test_no_status_a_sheet_can_carry_revives_the_dead(
        self, mongo_db, stale_status: str
    ) -> None:
        """``in_sequence`` is the case that matters: it is what a live campaign
        sheet actually contains, and the first fix let it straight through."""
        domain = f"dead-{stale_status}.example"
        created = await growth_service.upsert_by_domain(WS, _req(domain=domain))
        await growth_service.mark_prospect_dead(WS, created.id)

        again = await growth_service.upsert_by_domain(WS, _req(domain=domain, status=stale_status))
        assert again.status == "dead", (
            "a re-import must not lift a prospect out of a terminal state"
        )

    @pytest.mark.asyncio
    async def test_descriptive_fields_still_update(self, mongo_db) -> None:
        """The point is to freeze LIFECYCLE, not to freeze the row — an import
        that corrects a company name must still land."""
        created = await growth_service.upsert_by_domain(WS, _req(domain="desc.example"))
        again = await growth_service.upsert_by_domain(
            WS,
            _req(domain="desc.example", company="Northwind Dental Ltd", tier="a"),
        )
        assert again.id == created.id
        assert again.company == "Northwind Dental Ltd"
        assert again.tier == "a"
