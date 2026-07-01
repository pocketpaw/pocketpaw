# tests/ee/versions/test_version_projection.py
# Created: 2026-06-18 (feat/branch-primitive-revert-history, BP-4) — coverage for
# the VersionProjection: the read projection that replays the journal's
# artifact.version.* events into a per-artifact EVENT history timeline.
#
# What this pins:
#   * The projection folds created / branched / merged / discarded / reverted /
#     published events and yields them oldest → newest for one artifact.
#   * It is scope-keyed: two artifacts' histories never bleed into each other.
#   * history(workspace_id=...) is tenant-filtered.
#   * apply() is idempotent (a re-applied entry does not duplicate the timeline).
#   * Non-version events on the journal are dropped.
#
# It drives the REAL versions service (which emits the events) against a tmp
# journal, then rebuilds the projection from that journal — an end-to-end check
# that the emitted event shape and the projection's fold agree.
from __future__ import annotations

import pytest
from pocketpaw_ee.versions import service as versions
from pocketpaw_ee.versions.projection import VersionProjection

pytestmark = pytest.mark.asyncio

WS = "ws-proj"
POCKET = "pocket-proj"


def _rebuild(journal) -> VersionProjection:
    proj = VersionProjection()
    proj.rebuild(journal.replay_from(0))
    return proj


async def test_projection_folds_full_lifecycle_in_order(beanie_test_db, versions_journal):
    """A create → publish → revert sequence lands as an ordered event timeline."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}, author="u-1"
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    new_draft = await versions.revert(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id), author="u-2"
    )

    proj = _rebuild(versions_journal)
    history = proj.history(scope_type="pocket", scope_id=POCKET)

    # Oldest → newest: created, published, reverted.
    assert [e.action for e in history] == ["created", "published", "reverted"]
    # The created event carries v1; the reverted event carries the new draft +
    # the reverted_from pointer.
    created, published, reverted = history
    assert created.version_id == str(v1.id)
    assert created.actor_id == "u-1"
    assert published.version_id == str(v1.id)
    assert reverted.version_id == str(new_draft.id)
    assert reverted.payload["reverted_from"] == str(v1.id)
    assert reverted.workspace_id == WS


async def test_projection_folds_branch_merge_discard(beanie_test_db, versions_journal):
    """branch / merged / discarded events fold into the history with their verbs."""
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    cand = await versions.branch(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, new_branch="cand"
    )
    await versions.mark_merged(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(cand.id)
    )

    other = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 2}
    )
    await versions.discard(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(other.id)
    )

    proj = _rebuild(versions_journal)
    actions = [e.action for e in proj.history(scope_type="pocket", scope_id=POCKET)]
    assert actions == ["created", "branched", "merged", "created", "discarded"]


async def test_projection_is_scope_keyed(beanie_test_db, versions_journal):
    """Two artifacts keep separate histories — no cross-bleed."""
    await versions.write_draft(
        scope_type="pocket", scope_id="pocket-A", workspace_id=WS, content={}
    )
    await versions.write_draft(
        scope_type="pocket", scope_id="pocket-B", workspace_id=WS, content={}
    )
    await versions.write_draft(
        scope_type="pocket", scope_id="pocket-A", workspace_id=WS, content={}
    )

    proj = _rebuild(versions_journal)
    a = proj.history(scope_type="pocket", scope_id="pocket-A")
    b = proj.history(scope_type="pocket", scope_id="pocket-B")
    assert len(a) == 2
    assert len(b) == 1
    assert all(e.scope_id == "pocket-A" for e in a)


async def test_projection_history_is_tenant_filtered(beanie_test_db, versions_journal):
    """history(workspace_id=...) only returns the caller's tenant's events."""
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id="ws-1", content={}
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id="ws-2", content={}
    )

    proj = _rebuild(versions_journal)
    # Unfiltered sees both; tenant-filtered sees only its own.
    assert len(proj.history(scope_type="pocket", scope_id=POCKET)) == 2
    ws1 = proj.history(scope_type="pocket", scope_id=POCKET, workspace_id="ws-1")
    assert len(ws1) == 1
    assert ws1[0].workspace_id == "ws-1"


async def test_projection_apply_is_idempotent(beanie_test_db, versions_journal):
    """Replaying the same journal twice does not duplicate the timeline."""
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )

    proj = VersionProjection()
    proj.rebuild(versions_journal.replay_from(0))
    first = len(proj.history(scope_type="pocket", scope_id=POCKET))
    # Re-apply the same events (incremental from 0, which re-folds) — dedup keeps
    # the count stable.
    for entry in versions_journal.replay_from(0):
        proj.apply(entry)
    second = len(proj.history(scope_type="pocket", scope_id=POCKET))
    assert first == second == 2


async def test_projection_drops_non_version_events(versions_journal):
    """A non-artifact.version.* event on the journal is ignored by the fold."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from soul_protocol.spec.journal import Actor, EventEntry

    versions_journal.append(
        EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=Actor(kind="system", id="x", scope_context=["pocket:p"]),
            action="fabric.object.created",
            scope=["pocket:p", "workspace:ws-1"],
            payload={"object_id": "o1"},
        )
    )
    proj = _rebuild(versions_journal)
    assert proj.history(scope_type="pocket", scope_id="p") == []
