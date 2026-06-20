# tests/ee/sites/test_stable_identity.py — PERF-1 regression guard: a Paw Site
# has a STABLE per-pocket identity, so re-publishing the SAME pocket overwrites
# the one site in place (same URL, same single Site doc) instead of minting a
# fresh site_id / folder / URL / doc on every publish.
#
# Created: 2026-06-18 (feat/sites-stable-identity, PERF-1).
#
# The bug PERF-1 fixes: publish() minted ``site_id = str(ObjectId())`` per call,
# so every publish inserted a NEW Site doc at a NEW URL. One pocket accumulated
# 14 Site docs; the gallery showed dupes and ``pocket_status`` did an arbitrary
# ``find_one`` across them, returning a stale doc (often ``url=None``) while the
# freshest build sat at an unreferenced URL ("stale live link"). These tests pin
# the intended behavior: ONE doc + ONE stable URL per (workspace, pocket_id), and
# ``pocket_status`` returns that canonical, non-null url.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.asyncio


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


def _fake_local_deploy(site_id: str, project_dir: str) -> str:
    """Stand-in for local_server.deploy_local — returns the served URL for a
    site_id WITHOUT needing a real built dir on disk. Because the url is derived
    deterministically from site_id, a STABLE site_id ⇒ a STABLE url, exactly as
    the real local_server.local_url_for behaves (it overwrites <home>/<id>/ in
    place)."""
    return f"http://127.0.0.1:9999/{site_id}/"


async def _publish_local(pocket_id: str, *, name: str, deploy):
    """Publish a pocket through the LIVE path in LOCAL deploy mode (no CF client
    injected → the local branch), recording the deploy calls via ``deploy``."""
    from unittest.mock import AsyncMock, patch

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
            # NB: no _cloudflare → the LOCAL deploy branch is taken.
            _bundle_reader=lambda d: b"unused-in-local-mode",
            _local_deploy=deploy,
        )


async def test_two_publishes_same_pocket_same_url_and_one_doc(beanie_test_db, monkeypatch):
    """THE REGRESSION GUARD: two consecutive ``publish_pocket`` calls for the SAME
    pocket must produce the SAME url AND exactly ONE Site doc for that
    (workspace, pocket_id).

    Before PERF-1 this fails: each publish minted a fresh ObjectId → a new folder,
    a new url, and a new inserted Site doc (two docs, two urls)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)

    served_ids: list[str] = []

    def _recording_deploy(site_id: str, project_dir: str) -> str:
        served_ids.append(site_id)
        return _fake_local_deploy(site_id, project_dir)

    first = await _publish_local("pk_stable", name="Bright Smile", deploy=_recording_deploy)
    second = await _publish_local("pk_stable", name="Bright Smile", deploy=_recording_deploy)

    # SAME stable deploy id both times (not a fresh ObjectId per call) ⇒ same folder.
    assert len(served_ids) == 2
    assert served_ids[0] == served_ids[1], (
        "consecutive publishes for one pocket must deploy at the SAME stable id"
    )
    # SAME url both times — the live link does not move on re-publish.
    assert first.url == second.url
    assert first.url, "the published url must be non-empty"

    # Exactly ONE Site doc for (workspace, pocket_id) — re-publish UPSERTED in place
    # instead of inserting a second doc.
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk_stable"}).to_list()
    assert len(docs) == 1, f"expected ONE Site doc per pocket, found {len(docs)}"
    # The upsert reused the SAME doc id (it updated, did not replace with a new _id).
    assert str(first.id) == str(second.id)


async def test_pocket_status_returns_canonical_non_null_url(beanie_test_db, monkeypatch):
    """``pocket_status`` returns the canonical Site doc's NON-NULL url + is_live —
    the stale-live-link bug is gone: after two publishes there is one doc whose url
    is the live, openable address."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)

    await _publish_local("pk_status", name="x", deploy=_fake_local_deploy)
    second = await _publish_local("pk_status", name="x", deploy=_fake_local_deploy)

    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_status")
    assert res.is_live is True
    assert res.status == "published"
    # The canonical url is returned and is the live (non-null) address — the same
    # one the latest publish deployed at.
    assert res.url == second.url
    assert res.url, "pocket_status must return a non-null live url"
    assert res.site_id == str(second.id)


async def test_pocket_status_picks_live_doc_over_stale_dupe(beanie_test_db, monkeypatch):
    """Robustness against pre-existing dupes (PERF-1 does NOT migrate them — that's
    PERF-2): if an OLD stale doc (url="" / deployed) already exists for a pocket,
    ``pocket_status`` must return the canonical doc with a real url, not the stale
    one an arbitrary find_one might surface."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)

    # An old, stale dupe with NO url (the shape the bug left behind).
    stale = _SiteDoc(
        workspace="ws1",
        pocket_id="pk_dupe",
        owner="u1",
        name="Stale",
        script_name="stale",
        deployed=True,
        url="",
    )
    await stale.insert()

    # A real publish lands the canonical doc with a live url.
    live = await _publish_local("pk_dupe", name="Live", deploy=_fake_local_deploy)

    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id="pk_dupe")
    assert res.is_live is True
    assert res.url == live.url
    assert res.url, "pocket_status must surface the doc that has a real url, not the stale one"


class _RecordingCF:
    """A Cloudflare client that records the script_name each put_worker targets, so
    a test can prove a re-publish OVERWRITES the SAME worker (stable script_name per
    pocket) instead of orphaning a new one."""

    def __init__(self):
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


async def test_cf_republish_uses_stable_script_name_and_one_doc(beanie_test_db):
    """PROD consistency: on the real Cloudflare path, two publishes of the same
    pocket target the SAME worker script_name (overwrite in place, no orphan) and
    leave exactly ONE Site doc."""
    from unittest.mock import AsyncMock, patch

    cf = _RecordingCF()
    wire = {"name": "x", "engine": "ripple", "rippleSpec": {"type": "container"}}

    async def _publish_cf():
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=wire),
        ):
            return await sites_service.publish_pocket(
                workspace_id="ws1",
                user_id="u1",
                pocket_id="pk_cf",
                _generator=_FakeGenerator(),
                _cloudflare=cf,  # injected CF → the REAL deploy branch
                _bundle_reader=lambda d: b"export default {}",
            )

    first = await _publish_cf()
    second = await _publish_cf()

    # Same worker script_name both times — the worker is overwritten, not orphaned.
    assert cf.put_calls == [first.script_name, first.script_name]
    assert first.script_name == second.script_name
    # script_name is still the str form of the (now stable) doc id.
    assert first.script_name == str(first.id)
    # Exactly ONE Site doc — the second publish upserted the first.
    docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "pk_cf"}).to_list()
    assert len(docs) == 1
    assert str(first.id) == str(second.id)


async def test_republish_reuses_signed_key(beanie_test_db, monkeypatch):
    """PERF-1 review fix: a live RE-publish must REUSE the stored ``signed_key`` so
    the built HTML's ``captureSignedKey`` matches the persisted ``doc.signed_key``
    the capture endpoint verifies against. Before the fix each publish minted a
    fresh key while the upsert preserved the old one → silent lead-capture breakage
    on every re-publish."""
    from unittest.mock import AsyncMock, patch

    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)

    keys: list[str] = []

    class _RecordingGen:
        async def build(self, **kw):
            from pocketpaw_ee.sites.generator_client import BuildResult

            keys.append(kw.get("capture_signed_key"))
            return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")

    wire = {"name": "AKB", "engine": "ripple", "rippleSpec": {"type": "container"}}

    async def _pub():
        with patch(
            "pocketpaw_ee.cloud.pockets.service.get",
            new=AsyncMock(return_value=wire),
        ):
            return await sites_service.publish_pocket(
                workspace_id="ws1",
                user_id="u1",
                pocket_id="pk-signedkey",
                _generator=_RecordingGen(),
                _bundle_reader=lambda d: b"x",
                _local_deploy=lambda sid, pd: f"http://127.0.0.1:9999/{sid}/",
            )

    await _pub()
    await _pub()

    assert keys[0] is not None
    assert keys[0] == keys[1], "re-publish must reuse the same signed_key, not mint a new one"
    doc = await _SiteDoc.find_one(
        {"_id": sites_service._live_object_id("ws1", "pk-signedkey"), "workspace": "ws1"}
    )
    assert doc is not None and doc.signed_key == keys[1], "built key must match the stored doc"
