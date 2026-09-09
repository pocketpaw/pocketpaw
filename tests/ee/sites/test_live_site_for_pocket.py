# tests/ee/sites/test_live_site_for_pocket.py — the narrow Site read that the pocket
# delete guard asks (sites lifecycle wave 2).
#
# This is the sibling of ``site_pocket_ids``, and it inherits that read's one
# non-obvious rule: archived rows are dedupe TOMBSTONES, not sites. The 2026-06-18
# dedupe migration left archived duplicates behind (one pocket had 14 Site docs), so
# a lookup that forgets the filter would find a tombstone, call it a live site, and
# make that pocket permanently undeletable with no live site to explain why.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites import service as sites_service


class _Doc:
    def __init__(self, doc_id="s1", name="Bright Smile", script_name="bright-smile"):
        self.id = doc_id
        self.name = name
        self.script_name = script_name


class _FakeSiteDoc:
    """Records the query it was asked and returns a canned document."""

    query: dict | None = None
    result: object | None = None

    @classmethod
    async def find_one(cls, query):
        cls.query = query
        return cls.result


@pytest.fixture
def fake_sites(monkeypatch):
    _FakeSiteDoc.query = None
    _FakeSiteDoc.result = None
    monkeypatch.setattr(sites_service, "_SiteDoc", _FakeSiteDoc)
    return _FakeSiteDoc


@pytest.mark.asyncio
async def test_the_lookup_excludes_archived_dedupe_tombstones(fake_sites) -> None:
    """Without this filter a pocket whose only Site rows are archived duplicates
    becomes permanently undeletable, with no live site anywhere to explain it."""
    fake_sites.result = None

    await sites_service.live_site_for_pocket(workspace_id="w1", pocket_id="pk1")

    assert fake_sites.query == {
        "workspace": "w1",
        "pocket_id": "pk1",
        "archived": {"$ne": True},
    }


@pytest.mark.asyncio
async def test_it_returns_the_site_id_and_a_name_the_refusal_can_show(fake_sites) -> None:
    fake_sites.result = _Doc()
    assert await sites_service.live_site_for_pocket(workspace_id="w1", pocket_id="pk1") == (
        "s1",
        "Bright Smile",
    )


@pytest.mark.asyncio
async def test_an_unnamed_site_falls_back_to_its_script_name(fake_sites) -> None:
    """A site published without a display name still has to be identifiable in the
    refusal — 'the site ""' tells the owner nothing."""
    fake_sites.result = _Doc(name="")
    assert await sites_service.live_site_for_pocket(workspace_id="w1", pocket_id="pk1") == (
        "s1",
        "bright-smile",
    )


@pytest.mark.asyncio
async def test_no_site_reads_as_none_not_as_an_empty_tuple(fake_sites) -> None:
    """The caller branches on ``is not None``; an empty tuple would be falsy in some
    readings and truthy in others."""
    fake_sites.result = None
    assert await sites_service.live_site_for_pocket(workspace_id="w1", pocket_id="pk1") is None
