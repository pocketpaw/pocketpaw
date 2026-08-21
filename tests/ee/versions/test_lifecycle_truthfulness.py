# tests/ee/versions/test_lifecycle_truthfulness.py
# Created: 2026-08-21 (feat/version-history-truthful-lifecycle) — the version
# timeline must say what actually happened to each row.
#
# The defect these pin: ``"reverted"`` was written by THREE different
# transitions and by none of the ones a reader would guess. ``revert()`` never
# wrote it at all (revert moves forward — it writes a NEW draft), while the
# overwhelmingly common producer was the supersede inside ``write_draft``, i.e.
# THE OWNER EDITED THEIR SITE AGAIN. The Deploy tab renders the status verbatim
# in a warning tone, so a site edited six times showed five warning rows
# claiming a rollback that never happened.
#
# Invariants pinned here:
#   1. A draft replaced by a later edit reads ``superseded`` — never ``reverted``.
#   2. That supersede emits ``artifact.version.superseded``, so the event
#      history (VersionProjection) has no silent state change.
#   3. A draft the owner throws away reads ``discarded`` — a distinct outcome
#      from being quietly replaced, because one was a decision and one was not.
#   4. ``reverted`` is LEGACY — nothing writes it any more. A revert is a
#      forward step (a new labelled draft), and rewriting an older row to say
#      "reverted" would both break the append-only log and move the published
#      pointer off content that is still live.
#   5. Neither new status is mistaken for a pointer: get_draft / get_published
#      skip superseded and discarded rows.
from __future__ import annotations

import pytest
from pocketpaw_ee.versions import service as versions

pytestmark = pytest.mark.asyncio

POCKET = "pocket-truthful"
WS = "ws-1"


async def _statuses(scope_id: str = POCKET) -> dict[int, str]:
    """version_no → the status ON DISK (not the stale in-memory return value)."""
    rows = await versions.list_versions(scope_type="pocket", scope_id=scope_id)
    return {r.version_no: r.status for r in rows}


# ---------------------------------------------------------------------------
# 1 + 2. An ordinary edit supersedes — it does not revert
# ---------------------------------------------------------------------------


async def test_edit_supersedes_prior_draft_rather_than_reverting_it(beanie_test_db):
    """Three edits in a row leave v1/v2 marked superseded, not reverted.

    This is the whole bug in one assertion: nobody reverted anything here, the
    owner just typed three times.
    """
    for n in (1, 2, 3):
        await versions.write_draft(
            scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": n}
        )

    assert await _statuses() == {1: "superseded", 2: "superseded", 3: "draft"}


async def test_supersede_emits_its_own_event(beanie_test_db, versions_journal):
    """The supersede is a state change, so the event history must record it.

    Without this the projection replays a row that silently stopped being the
    draft, and an audit of "what happened to this artifact" has a hole in it.
    """
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )

    events = versions_journal.query(action="artifact.version.superseded")
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["version_id"] == str(v1.id)
    assert ev.payload["version_no"] == 1
    assert ev.payload["status"] == "superseded"
    # The row that replaced it, so a history reader can follow the chain.
    assert ev.payload["superseded_by_version_no"] == v2.version_no
    assert ev.scope  # tenant/scope stamped, same contract as every other event


async def test_published_head_is_never_superseded(beanie_test_db):
    """A new draft on top of a published version is a reviewable change — the
    published row keeps its status so the live pointer survives."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )

    assert await _statuses() == {1: "published", 2: "draft"}


# ---------------------------------------------------------------------------
# 3. Throwing a draft away is a decision, and reads as one
# ---------------------------------------------------------------------------


async def test_discard_reads_as_discarded(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.discard(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )

    assert await _statuses() == {1: "discarded"}


async def test_discard_all_drafts_reads_as_discarded(beanie_test_db):
    """The owner clicking "discard unpublished changes" clears the live draft.

    Rows already superseded by a later edit are not live drafts, so they keep
    saying superseded — the history distinguishes "replaced while you worked"
    from "you threw this away".
    """
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )
    cleared = await versions.discard_all_drafts(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS
    )

    assert cleared == 1
    assert await _statuses() == {1: "superseded", 2: "discarded"}


# ---------------------------------------------------------------------------
# 4. A revert is a forward step — it never rewrites the rows behind it
# ---------------------------------------------------------------------------


async def test_revert_never_rewrites_history_or_moves_the_live_pointer(beanie_test_db):
    """Reverting to v1 leaves v1 and v2 exactly as they were.

    Two reasons this matters, and the second is a live-site bug waiting to
    happen. The log is append-only, so a revert belongs on it as a new entry
    rather than as an edit to an old one. And the revert has not taken effect
    yet — it produced a DRAFT that still has to be published — so demoting v2
    here would make "online now" name v1 while v2's content is what visitors
    are actually being served.
    """
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v2.id)
    )

    new_draft = await versions.revert(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )

    assert new_draft.version_no == 3
    assert new_draft.status == "draft"
    assert new_draft.content == {"n": 1}
    # The revert reads as its own entry, not as damage to the two behind it.
    assert new_draft.label == "Revert to v1"
    assert await _statuses() == {1: "published", 2: "published", 3: "draft"}

    # v2 is still what the public sees until the revert draft is published.
    live = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert live is not None and live.id == v2.id


async def test_nothing_writes_the_legacy_reverted_status(beanie_test_db):
    """``reverted`` survives in the model only so rows written before this
    change still validate. Every transition that used to produce it now says
    what it actually was."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.write_draft(  # supersedes v1
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 3}
    )
    await versions.discard_all_drafts(scope_type="pocket", scope_id=POCKET, workspace_id=WS)
    await versions.revert(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )

    assert "reverted" not in (await _statuses()).values()


# ---------------------------------------------------------------------------
# 5. The derived pointers ignore both new statuses
# ---------------------------------------------------------------------------


async def test_pointers_skip_superseded_and_discarded(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    await versions.write_draft(  # superseded by the next one
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )
    v3 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 3}
    )

    draft = await versions.get_draft(scope_type="pocket", scope_id=POCKET)
    published = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert draft is not None and draft.id == v3.id
    assert published is not None and published.id == v1.id

    await versions.discard_all_drafts(scope_type="pocket", scope_id=POCKET, workspace_id=WS)
    assert await versions.get_draft(scope_type="pocket", scope_id=POCKET) is None
    still_live = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert still_live is not None and still_live.id == v1.id


# ---------------------------------------------------------------------------
# 6. Legacy rows are split on the read path, never rewritten
# ---------------------------------------------------------------------------


async def test_legacy_reverted_rows_split_by_lineage(beanie_test_db):
    """Rows already carrying the old word are classified from their lineage.

    v1 was superseded (v2 descends from it) and v3 was discarded (nothing
    descends from it), but both were written as "reverted" and are now
    indistinguishable on disk. The parent chain still separates them.
    """
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )
    v3 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 3}
    )
    # Put the collection back into the pre-2026-08-21 shape.
    for row in (v1, v2):
        row.status = "reverted"
        await row.save()

    rows = await versions.list_versions(scope_type="pocket", scope_id=POCKET)
    shown = versions.resolve_legacy_statuses(rows)

    assert shown[str(v1.id)] == "superseded"  # v2 descends from it
    assert shown[str(v2.id)] == "superseded"  # v3 descends from it
    assert shown[str(v3.id)] == "draft"  # untouched, still the live draft


async def test_legacy_reverted_leaf_reads_as_discarded(beanie_test_db):
    """A legacy row nothing descends from is where the lineage stopped."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    v1.status = "reverted"
    await v1.save()

    rows = await versions.list_versions(scope_type="pocket", scope_id=POCKET)
    assert versions.resolve_legacy_statuses(rows)[str(v1.id)] == "discarded"


async def test_resolver_leaves_current_statuses_alone(beanie_test_db):
    """It only ever touches the legacy value — a guess must not overwrite a fact."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 2}
    )
    v3 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 3}
    )

    rows = await versions.list_versions(scope_type="pocket", scope_id=POCKET)
    shown = versions.resolve_legacy_statuses(rows)
    # v1 is published and v2 superseded — v2 has a descendant, so a resolver that
    # ignored the status field would still call it superseded; v1 proves the
    # point, since lineage alone would have said the same of it.
    assert shown[str(v1.id)] == "published"
    assert shown[str(v2.id)] == "superseded"
    assert shown[str(v3.id)] == "draft"


async def test_resolver_does_not_write(beanie_test_db):
    """Resolution is display-only: the rows on disk keep saying what they said."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"n": 1}
    )
    v1.status = "reverted"
    await v1.save()

    rows = await versions.list_versions(scope_type="pocket", scope_id=POCKET)
    versions.resolve_legacy_statuses(rows)

    assert (await _statuses()) == {1: "reverted"}
