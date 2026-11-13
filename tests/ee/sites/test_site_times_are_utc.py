# tests/ee/sites/test_site_times_are_utc.py — every timestamp the sites API returns
# carries its zone.
#
# Mongo stores datetimes with no zone, so a value written tz-aware reads back NAIVE.
# ``deployed_at.isoformat()`` on such a value put "2026-10-31T09:00:00" on the wire,
# and a browser parses a zone-less ISO string as LOCAL time: an owner in India saw
# "Last deployed" five and a half hours off. These pin that the site response, the
# by-pocket status, the client invoices, the export record and the typed datetime
# fields on the DTOs all go out as UTC with an explicit offset.
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.sites import dto
from pocketpaw_ee.sites import service as sites_service

NAIVE = datetime(2026, 10, 31, 9, 0, 0)


def _is_utc(value: str | None) -> bool:
    assert value is not None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.utcoffset() == timedelta(0)


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _publish_local(pocket_id: str):
    wire = {"name": "Cafe", "engine": "ripple", "rippleSpec": {"type": "container"}}
    with patch("pocketpaw_ee.cloud.pockets.service.get", new=AsyncMock(return_value=wire)):
        return await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _generator=_FakeGenerator(),
            _bundle_reader=lambda d: b"unused-in-local-mode",
            _local_deploy=lambda sid, pd: f"http://127.0.0.1:9999/{sid}/",
        )


def test_utc_iso_tags_naive_values_as_utc_and_converts_aware_ones():
    assert dto.utc_iso(None) is None
    assert dto.utc_iso(NAIVE) == "2026-10-31T09:00:00+00:00"
    ist = timezone(timedelta(hours=5, minutes=30))
    assert dto.utc_iso(datetime(2026, 10, 31, 14, 30, tzinfo=ist)) == "2026-10-31T09:00:00+00:00"


@pytest.mark.asyncio
async def test_site_response_times_carry_utc_offset(beanie_test_db):
    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_tz", name="Cafe"
    )
    from pocketpaw_ee.cloud.models.site import Site

    doc = await Site.find_one(Site.pocket_id == "pk_tz")
    doc.deployed_at = NAIVE
    doc.createdAt = NAIVE
    doc.renewal_date = NAIVE
    resp = sites_service._to_response(doc)
    assert resp.deployed_at == "2026-10-31T09:00:00+00:00"
    assert resp.created_at == "2026-10-31T09:00:00+00:00"
    assert resp.renewal_date == "2026-10-31T09:00:00+00:00"


@pytest.mark.asyncio
async def test_gallery_card_read_back_from_mongo_is_utc(beanie_test_db, monkeypatch):
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    await _publish_local("pk_tz2")
    cards = await sites_service.list_for_workspace("ws1")
    assert cards and cards[0].deployed_at is not None
    assert _is_utc(cards[0].deployed_at)
    assert _is_utc(cards[0].created_at)


@pytest.mark.asyncio
async def test_pocket_status_deployed_at_is_utc(beanie_test_db, monkeypatch):
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    await _publish_local("pk_tz3")
    status = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_tz3")
    assert _is_utc(status.deployed_at)


def test_client_invoice_issued_at_is_utc():
    inv = SimpleNamespace(
        id="inv1",
        issued_at=NAIVE,
        amount_cents=100,
        currency="INR",
        paid=False,
        note="",
        amount_unit="minor",
    )
    site = SimpleNamespace(
        id="s1", client_name="", client_contact="", client_notes="", client_invoices=[inv]
    )
    out = sites_service._client_response(site)
    assert out.invoices[0].issued_at == "2026-10-31T09:00:00+00:00"


def test_typed_dto_datetimes_serialize_with_utc_offset():
    transfer = dto.SiteTransferResponse(
        site_id="s1", name="Cafe", from_workspace_id="ws1", offered_at=NAIVE
    )
    assert transfer.model_dump(mode="json")["offered_at"] == "2026-10-31T09:00:00+00:00"
    # An aware value keeps its instant, converted to UTC.
    aware = dto.SiteTransferResponse(
        site_id="s1",
        name="Cafe",
        from_workspace_id="ws1",
        offered_at=datetime(2026, 10, 31, 9, 0, tzinfo=UTC),
    )
    assert aware.model_dump(mode="json")["offered_at"] == "2026-10-31T09:00:00+00:00"
