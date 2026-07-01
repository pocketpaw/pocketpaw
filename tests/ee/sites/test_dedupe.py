# tests/ee/sites/test_dedupe.py — PERF-2 regression + unit guard: a
# non-destructive migration collapses each pocket's duplicate Site docs down to
# ONE active (non-archived) canonical, archiving the rest, and ``listSites``
# (``list_for_workspace``) then returns one card per pocket.
#
# Created: 2026-06-18 (feat/sites-dedupe-migration, PERF-2).
#
# The state PERF-2 fixes: PERF-1 made NEW publishes stable (one upserted Site doc
# per pocket), but EXISTING data still carries the dupes the old per-publish
# ObjectId minting left behind — e.g. the "AKB" pocket had 14 Site docs. The
# gallery (``list_for_workspace``) listed every one, so a single pocket showed up
# as many cards. This file pins PERF-2's contract:
#   * the PURE picker ``pick_canonical`` chooses the right canonical (stable-id >
#     deployed-with-url > newest) and returns the set to archive — no DB needed;
#   * the async ``dedupe_workspace`` archives the non-canonical dupes
#     non-destructively (sets ``archived=True``, never deletes), is IDEMPOTENT
#     (a second run archives nothing new), and a DRY-RUN writes nothing;
#   * ``list_for_workspace`` excludes archived docs ⇒ exactly one doc per pocket.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.sites import dedupe as dedupe_mod
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.service import _live_object_id

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Pure picker — no DB. ``pick_canonical`` takes the Site docs for ONE pocket and
# returns (canonical_doc, [docs_to_archive]). These exercise the rule directly.
# ---------------------------------------------------------------------------


def _doc(
    *,
    workspace: str = "ws1",
    pocket_id: str = "pk",
    oid: ObjectId | None = None,
    deployed: bool = True,
    url: str = "",
    created: datetime | None = None,
) -> _SiteDoc:
    """Build an in-memory Site doc (never inserted) for the pure picker tests."""
    doc = _SiteDoc(
        id=oid or ObjectId(),
        workspace=workspace,
        pocket_id=pocket_id,
        owner="u1",
        name="x",
        script_name="x",
        deployed=deployed,
        url=url,
    )
    if created is not None:
        doc.createdAt = created
    return doc


def test_pick_canonical_prefers_stable_id_doc():
    """Rule 1: the PERF-1 stable-id doc wins outright — it is the live identity
    every post-PERF-1 publish writes to, even if an older dupe is 'newer' by some
    other field."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    stable_oid = _live_object_id("ws1", "pk")
    stable = _doc(oid=stable_oid, deployed=True, url="http://x/stable/", created=base)
    # A newer dupe with a url — but NOT the stable id.
    newer = _doc(deployed=True, url="http://x/newer/", created=base + timedelta(days=5))

    canonical, to_archive = dedupe_mod.pick_canonical([newer, stable])

    assert canonical.id == stable_oid
    assert [d.id for d in to_archive] == [newer.id]


def test_pick_canonical_prefers_deployed_with_url_then_newest():
    """Rule 2/3/4 (no stable-id doc present): among legacy dupes, prefer a
    deployed doc that carries a real url, breaking ties by newest createdAt."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Oldest: deployed but no url (the stale shape the bug left behind).
    stale = _doc(deployed=True, url="", created=base)
    # Middle: deployed WITH a url — the freshest real live build among these two.
    live_old = _doc(deployed=True, url="http://x/old/", created=base + timedelta(days=1))
    # Newest: deployed WITH a url — should win the tie-break.
    live_new = _doc(deployed=True, url="http://x/new/", created=base + timedelta(days=2))

    canonical, to_archive = dedupe_mod.pick_canonical([stale, live_old, live_new])

    assert canonical.id == live_new.id
    assert {d.id for d in to_archive} == {stale.id, live_old.id}


def test_pick_canonical_single_doc_archives_nothing():
    """A pocket with ONE doc has nothing to archive (the idempotent steady state)."""
    only = _doc(deployed=True, url="http://x/only/")
    canonical, to_archive = dedupe_mod.pick_canonical([only])
    assert canonical.id == only.id
    assert to_archive == []


# ---------------------------------------------------------------------------
# Async migration over a real (mongomock) DB.
# ---------------------------------------------------------------------------


async def _seed_dupes(workspace: str, pocket_id: str, n: int) -> list[_SiteDoc]:
    """Insert ``n`` Site docs for one pocket with ascending createdAt and real
    urls — the AKB-style dupe pile the old per-publish minting produced."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    docs: list[_SiteDoc] = []
    for i in range(n):
        d = _SiteDoc(
            workspace=workspace,
            pocket_id=pocket_id,
            owner="u1",
            name=f"AKB {i}",
            script_name=f"s{i}",
            deployed=True,
            url=f"http://127.0.0.1:9999/legacy-{i}/",
        )
        d.createdAt = base + timedelta(minutes=i)
        await d.insert()
        docs.append(d)
    return docs


async def test_dedupe_apply_archives_all_but_one(beanie_test_db):
    """The core migration: a pocket with 14 dupes ends with exactly ONE active
    (non-archived) doc; the other 13 are archived (NOT deleted)."""
    await _seed_dupes("ws1", "akb", 14)

    report = await dedupe_mod.dedupe_workspace("ws1", apply=True)

    # Report accounting: one group, 13 archived, 1 kept active.
    assert report.pockets_scanned == 1
    assert report.docs_archived == 13
    assert report.applied is True

    all_docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "akb"}).to_list()
    assert len(all_docs) == 14, "non-destructive: every doc still exists"
    active = [d for d in all_docs if not getattr(d, "archived", False)]
    archived = [d for d in all_docs if getattr(d, "archived", False)]
    assert len(active) == 1, "exactly one active doc per pocket after dedupe"
    assert len(archived) == 13


async def test_dedupe_dry_run_writes_nothing(beanie_test_db):
    """DRY-RUN (the default): report what WOULD be archived, but write nothing."""
    await _seed_dupes("ws1", "akb", 5)

    report = await dedupe_mod.dedupe_workspace("ws1", apply=False)

    assert report.applied is False
    assert report.docs_archived == 4  # what it WOULD archive

    all_docs = await _SiteDoc.find({"workspace": "ws1", "pocket_id": "akb"}).to_list()
    assert all(not getattr(d, "archived", False) for d in all_docs), (
        "dry-run must not flip archived on any doc"
    )


async def test_dedupe_is_idempotent(beanie_test_db):
    """Running the migration twice is safe: the second run finds one active doc
    already and archives nothing new."""
    await _seed_dupes("ws1", "akb", 6)

    first = await dedupe_mod.dedupe_workspace("ws1", apply=True)
    assert first.docs_archived == 5

    second = await dedupe_mod.dedupe_workspace("ws1", apply=True)
    assert second.docs_archived == 0, "second run must archive nothing new"

    active = [
        d
        for d in await _SiteDoc.find({"workspace": "ws1", "pocket_id": "akb"}).to_list()
        if not getattr(d, "archived", False)
    ]
    assert len(active) == 1


async def test_list_for_workspace_excludes_archived(beanie_test_db):
    """``list_for_workspace`` (the gallery / listSites read) returns ONE card per
    pocket after the dedupe — archived dupes are filtered out."""
    await _seed_dupes("ws1", "akb", 8)
    # A second pocket with its own single doc — both pockets should show once.
    other = _SiteDoc(
        workspace="ws1",
        pocket_id="other",
        owner="u1",
        name="Other",
        script_name="o",
        deployed=True,
        url="http://x/other/",
    )
    await other.insert()

    # Before dedupe: the gallery shows all 8 AKB dupes + 1 other = 9 cards.
    before = await sites_service.list_for_workspace("ws1")
    assert len(before) == 9

    await dedupe_mod.dedupe_workspace("ws1", apply=True)

    after = await sites_service.list_for_workspace("ws1")
    pocket_ids = sorted(r.pocket_id for r in after)
    assert pocket_ids == ["akb", "other"], "one card per pocket after dedupe"


async def test_dedupe_multiple_pockets_independent(beanie_test_db):
    """Each (workspace, pocket_id) group is deduped independently — pockets don't
    bleed into each other's canonical pick."""
    await _seed_dupes("ws1", "akb", 4)
    await _seed_dupes("ws1", "second", 3)

    report = await dedupe_mod.dedupe_workspace("ws1", apply=True)

    assert report.pockets_scanned == 2
    assert report.docs_archived == 3 + 2  # (4-1) + (3-1)
    for pid in ("akb", "second"):
        active = [
            d
            for d in await _SiteDoc.find({"workspace": "ws1", "pocket_id": pid}).to_list()
            if not getattr(d, "archived", False)
        ]
        assert len(active) == 1


async def test_dedupe_all_workspaces(beanie_test_db):
    """``dedupe_all`` (the boot/CLI unscoped path) reconciles every workspace's
    pockets in one sweep."""
    await _seed_dupes("wsA", "p1", 3)
    await _seed_dupes("wsB", "p2", 4)

    report = await dedupe_mod.dedupe_all(apply=True)

    assert report.docs_archived == 2 + 3
    for ws, pid in (("wsA", "p1"), ("wsB", "p2")):
        active = [
            d
            for d in await _SiteDoc.find({"workspace": ws, "pocket_id": pid}).to_list()
            if not getattr(d, "archived", False)
        ]
        assert len(active) == 1
