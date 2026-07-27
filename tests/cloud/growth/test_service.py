# tests/cloud/growth/test_service.py — service-level tests for
# ``ee/cloud/growth/service.py``, centred on ``upsert_by_domain`` (the seam
# later ingestion slices call): same domain twice → update-not-duplicate,
# domain normalisation converging on one row, source preserved on update, and
# the tenant boundary (same domain in two workspaces → two independent rows).
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest
from pocketpaw_ee.cloud.models.prospect import Prospect as _ProspectDoc


def _req(**overrides: Any) -> CreateProspectRequest:
    base: dict[str, Any] = {
        "name": "Sam Founder",
        "company": "Acme Dental",
        "domain": "acme-dental.com",
        "source": "manual",
    }
    base.update(overrides)
    return CreateProspectRequest.model_validate(base)


@pytest.mark.asyncio
async def test_upsert_by_domain_creates_then_updates_not_duplicates(mongo_db):
    first = await growth_service.upsert_by_domain("w1", _req())
    assert first.tier == "unqualified"

    second = await growth_service.upsert_by_domain(
        "w1", _req(name="Sam F.", tier="a", emails=["sam@acme-dental.com"])
    )

    # Same row, updated — never a duplicate.
    assert second.id == first.id
    assert second.name == "Sam F."
    assert second.tier == "a"
    assert second.emails == ["sam@acme-dental.com"]
    assert await _ProspectDoc.find({"workspace": "w1"}).count() == 1


@pytest.mark.asyncio
async def test_upsert_by_domain_normalises_before_keying(mongo_db):
    """Scheme / www / case / path variants of one domain converge on one row."""
    first = await growth_service.upsert_by_domain("w1", _req(domain="acme-dental.com"))
    second = await growth_service.upsert_by_domain(
        "w1", _req(domain="https://www.ACME-Dental.com/pricing", name="Updated")
    )
    assert second.id == first.id
    assert second.domain == "acme-dental.com"
    assert await _ProspectDoc.find({"workspace": "w1"}).count() == 1


@pytest.mark.asyncio
async def test_upsert_by_domain_preserves_source_on_update(mongo_db):
    """``source`` records provenance at first capture; a re-import from a
    different source updates the fields but keeps the original source."""
    first = await growth_service.upsert_by_domain("w1", _req(source="manual"))
    second = await growth_service.upsert_by_domain("w1", _req(source="clay", tier="b"))
    assert second.id == first.id
    assert second.source == "manual"
    assert second.tier == "b"


@pytest.mark.asyncio
async def test_upsert_by_domain_is_workspace_scoped(mongo_db):
    """The dedupe key is tenant-local: the same domain in two workspaces is
    two independent rows."""
    w1_row = await growth_service.upsert_by_domain("w1", _req())
    w2_row = await growth_service.upsert_by_domain("w2", _req())
    assert w1_row.id != w2_row.id
    assert w1_row.workspace_id == "w1"
    assert w2_row.workspace_id == "w2"
    assert await _ProspectDoc.find({"domain": "acme-dental.com"}).count() == 2  # global-read: test
