# tests/ee/sites/test_created_at_on_the_wire.py — feat/sites-created-at: the /sites
# gallery orders by "most recent", and until now the only timestamp on the wire was
# ``deployed_at``.
#
# Created: 2026-08-21 (feat/sites-created-at).
#
# THE BUG. ``deployed_at`` is None for every DRAFT, and draft-first create
# (pocketpaw#1744) means most of a real workspace is drafts. The gallery's sort
# therefore had NO ordering key for the entire draft population and fell through to
# its alphabetical tiebreak — so a site created a minute ago rendered below one
# created last month, under "About". The data was never missing: ``Site`` extends
# ``TimestampedDocument``, which has stamped ``createdAt`` on insert since day one.
# It simply was not surfaced.
#
# What these pin:
#   * a freshly minted DRAFT carries ``created_at`` on SiteResponse even though it
#     has no ``deployed_at`` — the case the whole change exists for;
#   * it is an ISO-8601 string, not a datetime, matching every other timestamp on
#     these DTOs (the frontend does Date.parse on it);
#   * publishing does not disturb it — a site's creation time is not its deploy
#     time, and the two must be independently readable;
#   * a doc that somehow carries no ``createdAt`` reads None rather than raising,
#     so an old row degrades to name order instead of blanking the gallery.
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.asyncio


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _publish_local(pocket_id: str, *, name: str):
    """Publish through the LIVE path in LOCAL deploy mode. PAW_CF_DEPLOY_MODE is
    pinned by the caller's monkeypatch (the workspace .env leaks ``workers`` into the
    test env, which would send publish down the workers.dev branch and hit the
    network); the injected ``_local_deploy`` seam means no build tree is needed.
    Copied from test_draft_first_visibility.py, which publishes the same way."""
    wire = {"name": name, "engine": "ripple", "rippleSpec": {"type": "container"}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        return await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _generator=_FakeGenerator(),
            _bundle_reader=lambda d: b"unused-in-local-mode",
            _local_deploy=lambda sid, pd: f"http://127.0.0.1:9999/{sid}/",
        )


async def test_a_draft_carries_created_at_with_no_deployed_at(beanie_test_db):
    """THE CASE THIS EXISTS FOR. A draft has never deployed, so ``deployed_at`` is
    None — and before this change that left the gallery with nothing to order it by.
    ``created_at`` is present from the moment the row is minted."""
    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_draft", name="Minimal Test"
    )

    cards = await sites_service.list_for_workspace("ws1")
    assert len(cards) == 1
    card = cards[0]

    assert card.deployed is False
    assert card.deployed_at is None, "a draft has not deployed — this must stay None"
    assert card.created_at is not None, (
        "a draft must carry created_at, or the gallery has no key to order it by"
    )


async def test_created_at_is_an_iso_string_not_a_datetime(beanie_test_db):
    """The wire shape. Every other timestamp on these DTOs is an ISO-8601 string and
    the frontend runs Date.parse over it; a datetime would serialize differently and
    a naive client would read NaN."""
    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_iso", name="Bright Smile"
    )

    card = (await sites_service.list_for_workspace("ws1"))[0]
    assert isinstance(card.created_at, str)
    # Round-trips: the string parses back to the same instant.
    assert isinstance(datetime.fromisoformat(card.created_at), datetime)


async def test_publishing_does_not_move_created_at(beanie_test_db, monkeypatch):
    """A site's creation time is not its deploy time. Publish upserts the SAME doc
    (the one-doc invariant), so ``created_at`` must survive it unchanged while
    ``deployed_at`` appears alongside — the gallery reads deploy first and falls back
    to creation, and it can only do that if both are readable."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_pub", name="Bright Smile"
    )
    before = (await sites_service.list_for_workspace("ws1"))[0].created_at
    assert before is not None

    await _publish_local("pk_pub", name="Bright Smile")

    card = (await sites_service.list_for_workspace("ws1"))[0]
    assert card.deployed is True
    assert card.created_at == before, "publish must not restamp the creation time"
    assert card.deployed_at is not None, "and it must stamp the deploy time"


async def test_a_doc_without_created_at_reads_none_rather_than_raising(beanie_test_db):
    """Degrade, do not crash. ``_to_response`` reads the field via getattr for the
    same reason ``deployed_at`` does: a row written before the field existed must
    render as "no date" — which the gallery answers with name order — rather than
    taking the whole list down with an AttributeError."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    doc = _SiteDoc(
        workspace="ws1",
        pocket_id="pk_old",
        owner="u1",
        name="Legacy",
        script_name="legacy",
        deployed=False,
        signed_key="k",
    )
    # Stand in for a row that predates TimestampedDocument's field.
    object.__setattr__(doc, "createdAt", None)

    response = sites_service._to_response(doc)
    assert response.created_at is None
