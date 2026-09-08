# tests/ee/sites/test_draft_first_visibility.py — fix/sites-draft-visible: a
# draft-first site pocket (pocketpaw#1744) must LIST in the /sites gallery before it
# is ever published. The bug: draft-first create persisted a site POCKET but no Site
# doc, and the gallery read (``list_for_workspace``) lists Site docs — so a draft
# appeared in neither the All nor the Draft filter until a publish first minted one.
#
# Created: 2026-07-17 (fix/sites-draft-visible).
#
# These pin the fix + its invariants:
#   * ``create_draft_site`` mints ONE Site doc in a NOT-YET-DEPLOYED state (deployed
#     False, url "") that ``list_for_workspace`` returns, so a draft lists as a draft.
#   * THE ONE-DOC INVARIANT (PERF-1/PERF-2): create → publish yields EXACTLY ONE Site
#     doc for the pocket — publish FINDS the draft (same stable ``_id``) and flips it
#     live in place, never a second doc.
#   * IDEMPOTENT: a repeat create is a no-op (one doc), and it never resets an
#     already-published/live doc back to draft.
#   * CAPTURE INVARIANT: publishing over a draft REUSES the draft's ``signed_key`` so
#     the built ``captureSignedKey`` matches the persisted doc (lead capture works on
#     the first publish) — the create-side twin of ``test_republish_reuses_signed_key``.
#   * BILLING-SAFE: a draft carries no subscription and opens no checkout.
#
# Updated: 2026-08-11 (fix/sites-draft-realtime) — added the realtime block at the
# bottom. Listable was only half of visible: the mint emitted NOTHING (an explicit
# ``# no-event`` opt-out) and ``site.published`` was the only site event that
# existed, so a gallery that was already open never learned a draft had appeared.
# The new tests pin the ``site.created`` emit, its idempotence on the wire, its
# silence over an already-live doc, and that a failed push never costs the user the
# site. See the block comment there for the full failure.

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.asyncio


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


async def _publish_local(pocket_id: str, *, name: str, generator=None, deploy=None):
    """Publish a pocket through the LIVE path in LOCAL deploy mode. PAW_CF_DEPLOY_MODE
    is pinned to ``local`` (the workspace .env leaks ``workers`` into the test env,
    which would send publish down the workers.dev branch and hit the network); with
    ``local`` + the injected ``_local_deploy`` seam no build tree / CF is needed."""
    wire = {"name": name, "engine": "ripple", "rippleSpec": {"type": "container"}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        return await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _generator=generator or _FakeGenerator(),
            _bundle_reader=lambda d: b"unused-in-local-mode",
            _local_deploy=deploy or (lambda sid, pd: f"http://127.0.0.1:9999/{sid}/"),
        )


async def test_create_draft_site_lists_as_draft(beanie_test_db):
    """``create_draft_site`` mints ONE Site doc that ``list_for_workspace`` returns as
    a DRAFT — this is the whole bug: before the fix the gallery listed nothing for a
    draft-first pocket, so it showed in neither the All nor the Draft filter."""
    doc = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_draft", name="Minimal Test"
    )
    assert doc.deployed is False
    assert doc.url == ""

    # It lists — one card, and it reads as a draft (deployed False, no live url).
    cards = await sites_service.list_for_workspace("ws1")
    assert [c.pocket_id for c in cards] == ["pk_draft"]
    assert cards[0].deployed is False
    assert cards[0].url == ""
    assert cards[0].name == "Minimal Test"


async def test_create_then_publish_yields_exactly_one_doc(beanie_test_db, monkeypatch):
    """THE ONE-DOC INVARIANT: create → publish leaves EXACTLY ONE Site doc for the
    pocket. Publish FINDS the draft by its stable ``_id`` and flips it live in place
    (``deployed=True`` + a real url), never minting a second doc — the PERF-1/PERF-2
    dedupe guarantee, now spanning create → publish."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    draft = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk1", name="Bright Smile"
    )
    assert draft.deployed is False

    # Exactly one doc after create.
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk1"}).to_list()
    assert len(docs) == 1

    published = await _publish_local("pk1", name="Bright Smile")

    # SAME doc id — publish upserted the draft, did not replace/insert.
    assert str(published.id) == str(draft.id)
    # The draft flipped LIVE in place.
    assert published.deployed is True
    assert published.url, "publish must stamp a live url over the draft"

    # STILL exactly one Site doc for the pocket.
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk1"}).to_list()
    assert len(docs) == 1, f"expected ONE Site doc per pocket, found {len(docs)}"
    assert docs[0].deployed is True

    # And the gallery now lists it as live (deployed True).
    cards = await sites_service.list_for_workspace("ws1")
    assert len(cards) == 1
    assert cards[0].deployed is True


async def test_create_draft_is_idempotent(beanie_test_db):
    """A repeat create is a no-op — one doc, same id, still a draft. (A create tool
    minting the SAME site twice must not make two Site docs.)"""
    first = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_idem", name="Once"
    )
    second = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_idem", name="Twice"
    )

    assert str(first.id) == str(second.id)
    # The second call returned the existing doc untouched (name not overwritten).
    assert second.name == "Once"
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk_idem"}).to_list()
    assert len(docs) == 1


async def test_create_draft_never_clobbers_a_live_doc(beanie_test_db, monkeypatch):
    """``create_draft_site`` on a pocket that is already PUBLISHED/LIVE returns the live
    doc UNCHANGED — it never resets a live site back to a draft (deployed stays True)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_live", name="Live One"
    )
    live = await _publish_local("pk_live", name="Live One")
    assert live.deployed is True

    # A stray re-create must NOT undo the live state.
    again = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_live", name="Live One"
    )
    assert again.deployed is True
    assert again.url == live.url
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk_live"}).to_list()
    assert len(docs) == 1


async def test_publish_over_draft_reuses_signed_key(beanie_test_db, monkeypatch):
    """CAPTURE INVARIANT: publishing over a draft REUSES the draft's ``signed_key`` so
    the built ``captureSignedKey`` matches the persisted doc — otherwise lead capture
    silently breaks on the first publish (the built key would not match the stored one,
    since publish's UPDATE branch preserves the doc's key)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    draft = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_key", name="Keyed"
    )
    assert draft.signed_key, "a draft must seed a non-empty signed_key"

    keys: list[str] = []

    class _RecordingGen:
        async def build(self, **kw):
            from pocketpaw_ee.sites.generator_client import BuildResult

            keys.append(kw.get("capture_signed_key"))
            return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")

    published = await _publish_local("pk_key", name="Keyed", generator=_RecordingGen())

    assert keys[0] == draft.signed_key, "the build must reuse the draft's signed_key"
    assert published.signed_key == draft.signed_key, "the live doc keeps the draft key"


_DYNAMIC_SPEC = {
    "type": "container",
    "objects": [{"name": "signups", "fields": {"email": "text"}}],
    "sources": [{"name": "all", "kind": "data", "object": "signups", "refresh": "pocket_open"}],
}


async def test_dynamic_create_then_publish_yields_one_doc(beanie_test_db, monkeypatch):
    """The one-doc invariant across the DYNAMIC publish branch too: a dynamic publish
    goes through ``_provision_dynamic_site`` (not the inline deploy), which must FIND
    the pre-existing draft doc (same stable ``_id``) and flip it to
    provision_status="provisioning" in place — not insert a second doc."""

    class _RecordingDispatch:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def __call__(
            self, *, workspace_id, pocket_id, action, job_name, params, triggered_by
        ):
            self.calls.append({"pocket_id": pocket_id, "job_name": job_name})
            return {"ok": True, "code": "job_enqueued", "job_id": "job_xyz"}

    dispatch = _RecordingDispatch()
    monkeypatch.setattr("pocketpaw_ee.cloud.jobs.service.dispatch_job", dispatch)

    draft = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_dyn", name="Guestbook"
    )
    assert draft.deployed is False

    wire = {
        "name": "Guestbook",
        "engine": "ripple",
        "rippleSpec": _DYNAMIC_SPEC,
        "pattern": "dynamic",
    }
    with patch("pocketpaw_ee.cloud.pockets.service.get", new=AsyncMock(return_value=wire)):
        published = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk_dyn",
            _generator=_FakeGenerator(),
            _bundle_reader=lambda d: b"export default {}",
        )

    assert str(published.id) == str(draft.id)
    assert published.provision_status == "provisioning"
    assert published.deployed is False
    assert len(dispatch.calls) == 1 and dispatch.calls[0]["job_name"] == "provision_site"

    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk_dyn"}).to_list()
    assert len(docs) == 1, f"expected ONE Site doc per pocket, found {len(docs)}"


async def test_draft_carries_no_billing(beanie_test_db):
    """BILLING-SAFE: a draft carries no subscription and nothing has been bought —
    only publish bills. The minted draft doc has no sub state, no rail and no
    period paid for.

    It used to end on ``cards[0].checkout_url is None``, which stopped meaning
    anything on 2026-09-05: the hosted checkout went with the gateway, so the
    field is gone rather than empty. The assertions below are its replacement and
    are strictly stronger — ``billing_rail`` and ``period_paid_usd`` are what
    every seam now reads to decide whether a site has been paid for, and a draft
    that acquired either would be a site being billed before it was published."""
    doc = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_bill", name="Free Draft"
    )
    assert doc.deployed is False
    assert doc.subscription_status == "none"
    assert doc.subscription_id is None
    assert doc.plan_tier is None
    assert doc.pending_deploy_inputs == {}
    assert doc.billing_rail == "", "a draft rides no rail — nothing is paying for it"
    assert doc.period_paid_usd == 0
    assert doc.renewal_date is None

    cards = await sites_service.list_for_workspace("ws1")
    assert cards[0].subscription_status == "none"
    assert cards[0].period_paid_usd == 0


# ---------------------------------------------------------------------------
# Realtime: a draft must REACH an already-open gallery (fix/sites-draft-realtime)
# ---------------------------------------------------------------------------
# The draft-first fix above made a draft LISTABLE. It did not make it ARRIVE: the
# mint carried a ``# no-event`` opt-out, and ``site.published`` was the only site
# event in the catalog — so a gallery that was already open learned nothing when a
# draft appeared and showed it only after a manual Refresh or an F5. The bounded
# create poll covers the ONE tab that ran the create (it is armed by the per-run
# ``pocket_created`` SSE, so it is per-stream by construction); a draft created from
# the main chat, a second tab, a teammate's session, or an import reached nobody.
#
# These pin the emit itself. The workspace fan-out is pinned next to the publish
# routing in tests/cloud/realtime/test_audience.py.


def _site_created(bus) -> list:
    """The ``site.created`` events the recording bus saw, newest last."""
    return [e for e in bus.events if e.type == "site.created"]


async def test_create_draft_site_emits_site_created(beanie_test_db, _recording_bus_for_sites):
    """THE BUG: minting a draft emitted nothing, so an open gallery never heard about
    it. A fresh mint must emit ``site.created`` carrying the ids the client keys the
    gallery row off (``site_id`` / ``pocket_id``) plus the ``workspace_id`` the
    audience resolver fans out on.

    Mutation that must break this: delete the ``emit(SiteCreated(...))`` call in
    ``create_draft_site`` (i.e. restore the ``# no-event`` opt-out)."""
    doc = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_evt", name="Fresh Draft"
    )

    events = _site_created(_recording_bus_for_sites)
    assert len(events) == 1, "a fresh draft mint must emit exactly one site.created"
    data = events[0].data
    assert data["workspace_id"] == "ws1"
    assert data["site_id"] == str(doc.id)
    assert data["pocket_id"] == "pk_evt"
    assert data["owner"] == "u1"
    # ``deployed`` rides along so a client can tell this apart from a publish without
    # a second read — a draft is never live.
    assert data["deployed"] is False


async def test_repeat_create_draft_does_not_re_emit(beanie_test_db, _recording_bus_for_sites):
    """IDEMPOTENT ON THE WIRE, not just in Mongo. The mint already returns the existing
    doc untouched on a repeat create (``test_create_draft_is_idempotent``); the event
    must follow it. Emitting per CALL rather than per INSERT would push a duplicate
    "new site" at every open gallery each time a create tool retried.

    Mutation that must break this: move the emit ABOVE the idempotent early return."""
    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_evt_once", name="Once"
    )
    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_evt_once", name="Twice"
    )

    assert len(_site_created(_recording_bus_for_sites)) == 1


async def test_create_draft_over_live_doc_does_not_emit(
    beanie_test_db, monkeypatch, _recording_bus_for_sites
):
    """A stray re-create against an ALREADY-LIVE site must stay silent. The doc is
    returned unchanged (``test_create_draft_never_clobbers_a_live_doc``), so an event
    here would tell every open gallery a live site had just been created — the same
    early return covers both, and this pins that it does."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")

    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_evt_live", name="Live One"
    )
    await _publish_local("pk_evt_live", name="Live One")
    before = len(_site_created(_recording_bus_for_sites))

    await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_evt_live", name="Live One"
    )

    assert len(_site_created(_recording_bus_for_sites)) == before


async def test_emit_failure_does_not_fail_the_create(
    beanie_test_db, monkeypatch, _recording_bus_for_sites
):
    """The draft doc is the primary contract; the push is a courtesy. A realtime
    failure must not lose the site the user just asked for — the same discipline the
    thumbnail capture and the ``pocket_created`` push already follow.

    Mutation that must break this: drop the try/except around the emit."""

    async def _boom(_event):
        raise RuntimeError("bus is down")

    # raising=True (the default) on purpose: if the helper is renamed away, this test
    # must fail loudly rather than pass against an attribute monkeypatch invented.
    monkeypatch.setattr(sites_service, "_emit_site_created", _boom)

    doc = await sites_service.create_draft_site(
        workspace_id="ws1", user_id="u1", pocket_id="pk_evt_boom", name="Still Mine"
    )

    assert doc.deployed is False
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk_evt_boom"}).to_list()
    assert len(docs) == 1
