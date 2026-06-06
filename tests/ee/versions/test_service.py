# tests/ee/versions/test_service.py — state-machine tests for the pocket-content
# version-history module (pocketpaw#1345 Phase 1, plan §5/§6). Exercises the
# draft/published version log directly against the shared ``beanie_test_db``
# fixture (mongomock-motor), so no real Mongo is touched.
#
# Behaviours locked here (plan §6 operations):
#   * record_draft writes a NEW draft version, increments version_no, and links
#     the parent chain; the published pointer is untouched.
#   * a pocket with only a draft (never published) is status="draft", not_live.
#   * publish_draft promotes the current draft to published; status flips to
#     "published" and the draft/published pointers converge (draft_no == pub_no).
#   * editing AFTER a publish writes a new draft and leaves the published version
#     content untouched (the core "refine must not clobber live" guarantee).
#   * rollback clones an old version's content into a NEW draft (then publish goes
#     live), leaving history intact.
#   * get_draft_content returns the working version's content (for the preview).
#
# Created 2026-06-06 (feat/1345-draft-published).
# Updated 2026-06-06 (code review TEST-1 + BLOCK-1): added a svelte-track snapshot
# round-trip (record_draft(engine="svelte") → get_draft_content returns the SOURCE
# map, not None — the #1 data-loss risk, previously zero-covered), and a race
# regression test proving the unique index + record_draft retry never produce two
# rows with the same version_no.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.versions import service as versions_service


@pytest.mark.asyncio
async def test_record_draft_creates_first_version(beanie_test_db):
    v = await versions_service.record_draft(
        workspace_id="ws1",
        pocket_id="pk1",
        content={"type": "container", "rev": 1},
        engine="ripple",
        author="u1",
    )
    assert v.version_no == 1
    assert v.status == "draft"
    assert v.parent_version_no is None
    assert v.content == {"type": "container", "rev": 1}
    assert v.pocket_id == "pk1"
    assert v.workspace == "ws1"


@pytest.mark.asyncio
async def test_record_draft_increments_and_links_parent(beanie_test_db):
    v1 = await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    v2 = await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 2}, engine="ripple"
    )
    assert v2.version_no == 2
    assert v2.parent_version_no == v1.version_no


@pytest.mark.asyncio
async def test_fresh_pocket_is_draft_not_live(beanie_test_db):
    """A pocket that has a draft but was NEVER published is Draft, not live. This
    is the core of the 'Live lies' bug at the version layer: creating content
    must not imply published/live."""
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    status = await versions_service.status_for(workspace_id="ws1", pocket_id="pk1")
    assert status.status == "draft"
    assert status.published_version is None
    assert status.draft_version == 1


@pytest.mark.asyncio
async def test_publish_promotes_draft_to_published(beanie_test_db):
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    published = await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")
    assert published.status == "published"
    assert published.version_no == 1

    status = await versions_service.status_for(workspace_id="ws1", pocket_id="pk1")
    # draft == published → the pocket is "published" (no unpublished edits).
    assert status.status == "published"
    assert status.published_version == 1
    assert status.draft_version == 1


@pytest.mark.asyncio
async def test_edit_after_publish_creates_new_draft_published_untouched(beanie_test_db):
    """The headline guarantee: after publishing v1, an edit writes v2 as a draft
    and the published version's CONTENT is untouched until the next publish."""
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")

    # Refine: a new draft on top of the published version.
    v2 = await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 2}, engine="ripple"
    )
    assert v2.version_no == 2
    assert v2.status == "draft"

    status = await versions_service.status_for(workspace_id="ws1", pocket_id="pk1")
    # draft (2) != published (1) → unpublished edits exist → status "draft".
    assert status.status == "draft"
    assert status.draft_version == 2
    assert status.published_version == 1

    # The published version's content is the ORIGINAL, not the new draft.
    pub = await versions_service.get_published(workspace_id="ws1", pocket_id="pk1")
    assert pub is not None
    assert pub.content == {"rev": 1}

    # The draft content (what preview renders) is the NEW content.
    draft_content = await versions_service.get_draft_content(workspace_id="ws1", pocket_id="pk1")
    assert draft_content == {"rev": 2}


@pytest.mark.asyncio
async def test_publish_second_draft_advances_published_pointer(beanie_test_db):
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 2}, engine="ripple"
    )
    pub2 = await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")
    assert pub2.version_no == 2

    status = await versions_service.status_for(workspace_id="ws1", pocket_id="pk1")
    assert status.status == "published"
    assert status.published_version == 2
    assert status.draft_version == 2
    pub = await versions_service.get_published(workspace_id="ws1", pocket_id="pk1")
    assert pub.content == {"rev": 2}


@pytest.mark.asyncio
async def test_rollback_clones_old_version_into_new_draft(beanie_test_db):
    """Rollback makes an old version the new draft (a fresh version with the old
    content), leaving the history intact. Publishing it then goes live."""
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 2}, engine="ripple"
    )
    await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")

    # Roll back to v1's content.
    rolled = await versions_service.rollback(workspace_id="ws1", pocket_id="pk1", target_version=1)
    assert rolled.version_no == 3  # a NEW version, not a mutation of v1
    assert rolled.status == "draft"
    assert rolled.content == {"rev": 1}

    status = await versions_service.status_for(workspace_id="ws1", pocket_id="pk1")
    # The rolled-back draft is unpublished until an explicit publish.
    assert status.status == "draft"
    assert status.draft_version == 3
    assert status.published_version == 2


@pytest.mark.asyncio
async def test_get_draft_content_returns_latest_working_content(beanie_test_db):
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 1}, engine="ripple"
    )
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", content={"rev": 2}, engine="ripple"
    )
    content = await versions_service.get_draft_content(workspace_id="ws1", pocket_id="pk1")
    assert content == {"rev": 2}


@pytest.mark.asyncio
async def test_status_for_unknown_pocket_is_none(beanie_test_db):
    """A pocket with no versions has no draft/published — status_for reports a
    'none' status so callers can treat it as 'not versioned yet'."""
    status = await versions_service.status_for(workspace_id="ws1", pocket_id="nope")
    assert status.status == "none"
    assert status.draft_version is None
    assert status.published_version is None


@pytest.mark.asyncio
async def test_versions_are_workspace_scoped(beanie_test_db):
    """Two workspaces with the same pocket_id never see each other's versions
    (tenant isolation)."""
    await versions_service.record_draft(
        workspace_id="ws_a", pocket_id="pk1", content={"who": "a"}, engine="ripple"
    )
    await versions_service.record_draft(
        workspace_id="ws_b", pocket_id="pk1", content={"who": "b"}, engine="ripple"
    )
    a = await versions_service.get_draft_content(workspace_id="ws_a", pocket_id="pk1")
    b = await versions_service.get_draft_content(workspace_id="ws_b", pocket_id="pk1")
    assert a == {"who": "a"}
    assert b == {"who": "b"}
    # Each workspace's first version is version_no 1 (counter is per (ws, pocket)).
    status_a = await versions_service.status_for(workspace_id="ws_a", pocket_id="pk1")
    assert status_a.draft_version == 1


# ---------------------------------------------------------------------------
# TEST-1: svelte-track snapshot round-trip. The #1 data-loss risk — every other
# test runs engine="ripple", so the svelte branch of get_draft_content (return
# ``source``, not ``content``) had zero coverage. If the engine branch were
# flipped, these fail.
# ---------------------------------------------------------------------------

_SVELTE_SOURCE = {
    "src/routes/+page.svelte": "<h1>hi</h1>",
    "src/routes/+layout.svelte": "<slot />",
}


@pytest.mark.asyncio
async def test_record_draft_svelte_source_round_trips(beanie_test_db):
    """A svelte version stores its content in ``source`` (content=None).
    get_draft_content must return that source map — NOT None, NOT the empty
    ``content``. Flipping the engine branch (returning ``content``) makes this
    return None and the test fails."""
    v = await versions_service.record_draft(
        workspace_id="ws1",
        pocket_id="pk1",
        engine="svelte",
        source=_SVELTE_SOURCE,
        content=None,
    )
    assert v.engine == "svelte"
    assert v.source == _SVELTE_SOURCE
    assert v.content is None

    draft_content = await versions_service.get_draft_content(workspace_id="ws1", pocket_id="pk1")
    assert draft_content == _SVELTE_SOURCE  # the source map, not None


@pytest.mark.asyncio
async def test_svelte_version_rolls_back_with_source(beanie_test_db):
    """A svelte rollback clones ``source`` (not ``content``) into the new draft,
    so the svelte track round-trips through publish → refine → rollback."""
    await versions_service.record_draft(
        workspace_id="ws1", pocket_id="pk1", engine="svelte", source=_SVELTE_SOURCE
    )
    await versions_service.publish_draft(workspace_id="ws1", pocket_id="pk1")
    await versions_service.record_draft(
        workspace_id="ws1",
        pocket_id="pk1",
        engine="svelte",
        source={"src/routes/+page.svelte": "<h1>v2</h1>"},
    )
    rolled = await versions_service.rollback(workspace_id="ws1", pocket_id="pk1", target_version=1)
    assert rolled.engine == "svelte"
    assert rolled.source == _SVELTE_SOURCE
    assert rolled.content is None
    content = await versions_service.get_draft_content(workspace_id="ws1", pocket_id="pk1")
    assert content == _SVELTE_SOURCE


# ---------------------------------------------------------------------------
# BLOCK-1: record_draft is race-safe. The unique index on
# (workspace, pocket_id, version_no) turns a lost check-then-act race into a
# DuplicateKeyError; record_draft retries (re-read max → re-increment → insert).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_version_no_is_rejected_by_unique_index(beanie_test_db):
    """The unique index forbids two rows with the same (ws, pocket, version_no):
    a raw second insert at v1 must raise DuplicateKeyError, not silently corrupt
    the log. This is what makes the record_draft retry meaningful."""
    from pocketpaw_ee.cloud.models.pocket_version import PocketVersion
    from pymongo.errors import DuplicateKeyError

    await PocketVersion(workspace="ws1", pocket_id="pk1", version_no=1, content={"rev": 1}).insert()
    with pytest.raises(DuplicateKeyError):
        await PocketVersion(
            workspace="ws1", pocket_id="pk1", version_no=1, content={"rev": 1}
        ).insert()


@pytest.mark.asyncio
async def test_record_draft_retries_when_version_no_is_stolen(beanie_test_db):
    """Simulate the race deterministically: a concurrent writer has already taken
    v2, but our ``latest_version_no`` reads a STALE max (1) on the first attempt,
    so record_draft computes v2 and its insert hits the unique index. record_draft
    must catch the DuplicateKeyError, re-read the now-current max (2), and insert
    v3 — the edit lands as a distinct version instead of surfacing a 500."""
    from pocketpaw_ee.cloud.models.pocket_version import PocketVersion
    from pocketpaw_ee.cloud.versions.service import _BeanieVersionStore

    # v1 already exists (an earlier edit), and a racing writer has claimed v2.
    await versions_service.record_draft(workspace_id="ws1", pocket_id="pk1", content={"rev": 1})
    await PocketVersion(
        workspace="ws1", pocket_id="pk1", version_no=2, content={"rev": "racer"}
    ).insert()

    class _StaleOnceStore(_BeanieVersionStore):
        """Reports a stale max (1) on the FIRST read so the caller computes the
        already-taken v2 and collides; every later read returns the true max."""

        def __init__(self) -> None:
            self._served_stale = False

        async def latest_version_no(self, workspace_id: str, pocket_id: str):
            if not self._served_stale:
                self._served_stale = True
                return 1  # stale: hides the racer's v2 → caller computes v2, collides
            return await super().latest_version_no(workspace_id, pocket_id)

    v = await versions_service.record_draft(
        workspace_id="ws1",
        pocket_id="pk1",
        content={"rev": 2},
        _store=_StaleOnceStore(),
    )
    # Retry recovered: our edit landed AFTER the racer's v2, as v3 — never a
    # duplicate, never a raised error.
    assert v.version_no == 3
    assert v.content == {"rev": 2}

    versions = await versions_service.list_versions(workspace_id="ws1", pocket_id="pk1")
    nums = sorted(x.version_no for x in versions)
    assert nums == [1, 2, 3]  # no duplicates in the log
