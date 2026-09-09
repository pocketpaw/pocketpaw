# tests/ee/sites/test_pocket_delete_orphan.py — deleting a pocket must not strand
# the site published from it (sites lifecycle wave 2).
#
# The failure this prevents is not a broken page. architecture-sites Invariant 1 says
# a site worker never depends on the box, so deleting the pocket leaves the Worker,
# its D1, the custom hostname, the R2 assets and the live signed key all working —
# an unreachable site still serving, still accepting lead ingest against a valid key,
# and still billing. Nothing in the product can reach it again.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ConflictError


class _FakePocket:
    def __init__(self) -> None:
        self.id = "pk1"
        self.owner = "u1"
        self.workspace = "w1"
        self.visibility = "private"
        self.shared_with: list[str] = []
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


@pytest.fixture
def wired(monkeypatch):
    """Drive pockets_service.delete with the Site lookup and emit stubbed out."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.sites import service as sites_service

    pocket = _FakePocket()

    async def _fetch(_pocket_id):
        return pocket

    emitted: list = []

    async def _emit(evt):
        emitted.append(evt)

    monkeypatch.setattr(pockets_service, "_fetch_pocket", _fetch)
    monkeypatch.setattr(pockets_service, "emit", _emit)
    return pockets_service, sites_service, pocket, emitted


@pytest.mark.asyncio
async def test_deleting_a_pocket_that_has_a_site_is_refused(wired, monkeypatch) -> None:
    """The whole point. The pocket must survive so the site stays reachable."""
    pockets_service, sites_service, pocket, emitted = wired

    async def _live(*, workspace_id, pocket_id):
        return ("site-123", "Bright Smile")

    monkeypatch.setattr(sites_service, "live_site_for_pocket", _live)

    with pytest.raises(ConflictError) as err:
        await pockets_service.delete("pk1", "u1")

    assert err.value.code == "pocket.has_site"
    # The message has to be actionable: it names the site and the way out.
    assert "Bright Smile" in err.value.message
    assert "Delete the site first" in err.value.message
    # And nothing may have happened.
    assert pocket.deleted is False
    assert emitted == []


@pytest.mark.asyncio
async def test_a_pocket_with_no_site_still_deletes(wired, monkeypatch) -> None:
    """The guard must not become a wall. A pocket that was never published, or whose
    site has already been deleted, deletes exactly as before."""
    pockets_service, sites_service, pocket, emitted = wired

    async def _live(*, workspace_id, pocket_id):
        return None

    monkeypatch.setattr(sites_service, "live_site_for_pocket", _live)

    await pockets_service.delete("pk1", "u1")

    assert pocket.deleted is True
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_the_site_lookup_is_scoped_to_the_pockets_own_workspace(
    wired, monkeypatch
) -> None:
    """A cross-tenant lookup would let one workspace's site block another's delete —
    and, worse, leak that the site exists."""
    pockets_service, sites_service, pocket, _ = wired
    seen: dict = {}

    async def _live(*, workspace_id, pocket_id):
        seen["workspace_id"] = workspace_id
        seen["pocket_id"] = pocket_id
        return None

    monkeypatch.setattr(sites_service, "live_site_for_pocket", _live)

    await pockets_service.delete("pk1", "u1")

    assert seen == {"workspace_id": "w1", "pocket_id": "pk1"}


@pytest.mark.asyncio
async def test_a_non_owner_is_still_refused_before_the_site_is_ever_looked_up(
    wired, monkeypatch
) -> None:
    """Ownership is checked first. Otherwise the refusal message would tell a
    non-owner the name of a site in a workspace they cannot see."""
    pockets_service, sites_service, _pocket, _ = wired
    called = False

    async def _live(*, workspace_id, pocket_id):
        nonlocal called
        called = True
        return ("site-123", "Bright Smile")

    monkeypatch.setattr(sites_service, "live_site_for_pocket", _live)

    from pocketpaw_ee.cloud.shared.errors import Forbidden

    with pytest.raises(Forbidden):
        await pockets_service.delete("pk1", "someone-else")

    assert called is False
