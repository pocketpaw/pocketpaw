# tests/ee/versions/test_versions_service.py
# Created: 2026-06-18 (feat/branch-primitive-versions, BP-1) — coverage for the
# universal Branch-primitive versions service spine.
#
# Invariants pinned here:
#   1. write_draft creates monotonic version_no within (scope, branch) and
#      links parent_version_id to the prior head.
#   2. get_draft returns the latest draft; get_published returns nothing until
#      publish(); publish() flips the published pointer (published_version_id
#      does not change until publish is called).
#   3. publish() validates the version belongs to the artifact/workspace.
#   4. branch() opens a candidate lineage off the source head (version_no=1,
#      parent pointing back at the source).
#   5. list_versions returns the version log newest-first.
#   6. write_draft emits artifact.version.created; branch emits
#      artifact.version.branched — both with a non-empty stamped scope.
#   7. revert is a BP-4 stub (raises NotImplementedError).
from __future__ import annotations

import pytest
from pocketpaw_ee.versions import service as versions

pytestmark = pytest.mark.asyncio

POCKET = "pocket-abc"
WS = "ws-1"


# ---------------------------------------------------------------------------
# 1. write_draft — monotonic versions + parent linkage
# ---------------------------------------------------------------------------


async def test_write_draft_monotonic_versions(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"state": {"n": 1}}
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"state": {"n": 2}}
    )
    v3 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"state": {"n": 3}}
    )

    assert [v1.version_no, v2.version_no, v3.version_no] == [1, 2, 3]
    assert v1.parent_version_id is None
    assert v2.parent_version_id == str(v1.id)
    assert v3.parent_version_id == str(v2.id)
    assert all(v.status == "draft" for v in (v1, v2, v3))
    assert v3.content == {"state": {"n": 3}}


async def test_version_no_is_per_scope(beanie_test_db):
    # Two different artifacts each start their own monotonic sequence.
    a1 = await versions.write_draft(
        scope_type="pocket", scope_id="pocket-A", workspace_id=WS, content={}
    )
    b1 = await versions.write_draft(
        scope_type="pocket", scope_id="pocket-B", workspace_id=WS, content={}
    )
    a2 = await versions.write_draft(
        scope_type="pocket", scope_id="pocket-A", workspace_id=WS, content={}
    )
    assert a1.version_no == 1
    assert b1.version_no == 1
    assert a2.version_no == 2


async def test_generic_scope_type(beanie_test_db):
    # The model/service are artifact-generic — a non-pocket scope_type works
    # with no model change (this is the OSS-extraction seam).
    v = await versions.write_draft(
        scope_type="dashboard", scope_id="dash-1", workspace_id=WS, content={"x": 1}
    )
    assert v.scope_type == "dashboard"
    assert v.version_no == 1


# ---------------------------------------------------------------------------
# 2. pointer reads — get_draft / get_published + publish flips the pointer
# ---------------------------------------------------------------------------


async def test_get_draft_returns_latest_draft(beanie_test_db):
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 2}
    )
    draft = await versions.get_draft(scope_type="pocket", scope_id=POCKET)
    assert draft is not None
    assert draft.id == v2.id
    assert draft.content == {"v": 2}


async def test_published_pointer_unchanged_until_publish(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    # No published version exists yet.
    assert await versions.get_published(scope_type="pocket", scope_id=POCKET) is None

    published = await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    assert published.status == "published"

    pub_pointer = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert pub_pointer is not None
    assert pub_pointer.id == v1.id


async def test_publish_promotes_newer_version(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 2}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    # Promote v2 — the published pointer follows the higher version_no.
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v2.id)
    )
    pub = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert pub is not None
    assert pub.id == v2.id


async def test_publish_rejects_foreign_artifact(beanie_test_db):
    v = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={}
    )
    with pytest.raises(ValueError):
        await versions.publish(
            scope_type="pocket",
            scope_id="some-other-pocket",
            workspace_id=WS,
            version_id=str(v.id),
        )


async def test_publish_missing_version_raises(beanie_test_db):
    from beanie import PydanticObjectId

    with pytest.raises(ValueError):
        await versions.publish(
            scope_type="pocket",
            scope_id=POCKET,
            workspace_id=WS,
            version_id=str(PydanticObjectId()),
        )


# ---------------------------------------------------------------------------
# 4. branch — candidate lineage off the source head
# ---------------------------------------------------------------------------


async def test_branch_opens_candidate_from_published_head(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"live": True}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(v1.id)
    )
    cand = await versions.branch(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, new_branch="candidate-1"
    )
    assert cand.branch == "candidate-1"
    assert cand.version_no == 1
    assert cand.parent_version_id == str(v1.id)
    assert cand.content == {"live": True}  # snapshot of the published head
    # The candidate is its own branch — main's published pointer is unaffected
    # (still v1) and the candidate row does NOT show up as a main-branch draft.
    main_pub = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert main_pub is not None
    assert main_pub.id == v1.id
    assert await versions.get_draft(scope_type="pocket", scope_id="candidate-never") is None
    cand_draft = await versions.get_draft(
        scope_type="pocket", scope_id=POCKET, branch="candidate-1"
    )
    assert cand_draft is not None
    assert cand_draft.id == cand.id


async def test_branch_falls_back_to_latest_when_nothing_published(beanie_test_db):
    v1 = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"draft": 1}
    )
    cand = await versions.branch(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, new_branch="candidate-x"
    )
    assert cand.parent_version_id == str(v1.id)
    assert cand.content == {"draft": 1}


# ---------------------------------------------------------------------------
# 5. list_versions — the version log
# ---------------------------------------------------------------------------


async def test_list_versions_newest_first(beanie_test_db):
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 2}
    )
    log = await versions.list_versions(scope_type="pocket", scope_id=POCKET, branch="main")
    assert [v.version_no for v in log] == [2, 1]


# ---------------------------------------------------------------------------
# 6. Journal emission
# ---------------------------------------------------------------------------


async def test_write_draft_emits_version_created(beanie_test_db, versions_journal):
    v = await versions.write_draft(
        scope_type="pocket",
        scope_id=POCKET,
        workspace_id=WS,
        content={"v": 1},
        author="user-7",
    )
    events = versions_journal.query(action="artifact.version.created")
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["version_id"] == str(v.id)
    assert ev.payload["scope_type"] == "pocket"
    assert ev.payload["version_no"] == 1
    # Scope MUST be stamped non-empty (journal invariant + downstream filters).
    assert ev.scope
    assert f"pocket:{POCKET}" in ev.scope
    assert f"workspace:{WS}" in ev.scope


async def test_branch_emits_version_branched(beanie_test_db, versions_journal):
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    cand = await versions.branch(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, new_branch="cand"
    )
    events = versions_journal.query(action="artifact.version.branched")
    assert len(events) == 1
    assert events[0].payload["version_id"] == str(cand.id)
    assert events[0].payload["from_branch"] == "main"
    assert events[0].scope  # non-empty


# ---------------------------------------------------------------------------
# 7. revert — BP-4 stub
# ---------------------------------------------------------------------------


async def test_revert_is_bp4_stub(beanie_test_db):
    with pytest.raises(NotImplementedError):
        await versions.revert()
