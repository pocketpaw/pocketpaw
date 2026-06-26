# tests/ee/versions/test_instinct_executor.py
# Created: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — behaviour
# coverage for the artifact-change merge/discard EXECUTOR against real
# ArtifactVersion rows (beanie), with the Instinct store + site deploy mocked.
#
# What this pins (the BP-3 done-when behaviour):
#   * execute_approved_change MERGES: the candidate draft (to_version_id) is
#     promoted to published, marked merged, and — for pocket/site scope — the
#     site deploy is triggered; the Action is marked executed.
#   * execute_approved_change marks the Action FAILED if the deploy raises (the
#     version is still published — published != live).
#   * discard_rejected_change DISCARDS: the candidate is flipped to reverted and
#     the published pointer is left UNTOUCHED (a rejection never moves Live).
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pocketpaw_ee.versions import instinct_executor as executor
from pocketpaw_ee.versions import service as versions

pytestmark = pytest.mark.asyncio

WS = "ws-exec"
POCKET = "pocket-exec"
USER = "user-exec"


class _FakeStore:
    """Captures mark_executed / mark_failed so the test can assert the terminal
    the executor wrote on the Action."""

    def __init__(self) -> None:
        self.executed: dict[str, Any] = {}
        self.failed: dict[str, Any] = {}

    async def mark_executed(self, action_id: str, outcome: Any = None) -> None:
        self.executed[action_id] = outcome

    async def mark_failed(self, action_id: str, error: str) -> None:
        self.failed[action_id] = error


def _action(to_version_id: str) -> SimpleNamespace:
    """A minimal Action stand-in carrying an ``_artifact_change`` blob.

    ``branch`` is "main" to match the real request_publish_pocket flow (it stamps
    branch="main") AND the branch the candidate drafts below are written on
    (write_draft defaults to "main"). The P2a discard_all_drafts is branch-scoped,
    so the blob branch must match where the draft actually lives — exactly as it
    does in production."""
    return SimpleNamespace(
        id="act-1",
        pocket_id=POCKET,
        parameters={
            "_artifact_change": {
                "scope_type": "pocket",
                "scope_id": POCKET,
                "branch": "main",
                "from_version_id": "ver-from",
                "to_version_id": to_version_id,
                "workspace": WS,
                "user_id": USER,
            }
        },
    )


async def test_approve_merges_publishes_and_deploys(
    beanie_test_db, versions_journal, monkeypatch
) -> None:
    """APPROVE = MERGE: the candidate is promoted to published, the site deploy
    fires, and the Action is marked executed."""
    store = _FakeStore()
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    deploys: list[dict] = []

    async def _fake_publish_pocket(**kwargs):
        deploys.append(kwargs)
        return SimpleNamespace(id="site-1")

    monkeypatch.setattr("pocketpaw_ee.sites.service.publish_pocket", _fake_publish_pocket)

    # A candidate draft is the merge target.
    cand = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "candidate"}
    )

    await executor.execute_approved_change(_action(str(cand.id)))

    # The candidate is now the published pointer (the merge = promote). In the
    # single-row model published IS the merge result; we deliberately do NOT also
    # flip it to "merged" (that would erase the pointer).
    refreshed = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert refreshed is not None
    assert str(refreshed.id) == str(cand.id)
    from pocketpaw_ee.versions.models import ArtifactVersion

    row = await ArtifactVersion.get(str(cand.id))
    assert row.status == "published"

    # The site deploy was triggered for the pocket scope.
    assert len(deploys) == 1
    assert deploys[0]["pocket_id"] == POCKET
    assert deploys[0]["workspace_id"] == WS

    # The Action was marked executed (not failed).
    assert "act-1" in store.executed
    assert "act-1" not in store.failed


async def test_approve_merge_marks_failed_when_deploy_raises(
    beanie_test_db, versions_journal, monkeypatch
) -> None:
    """If the deploy raises, the Action is marked FAILED — the version is still
    published (published != live, the BP-2 invariant)."""
    store = _FakeStore()
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    async def _boom_deploy(**kwargs):
        raise RuntimeError("workerd smoke gate failed")

    monkeypatch.setattr("pocketpaw_ee.sites.service.publish_pocket", _boom_deploy)

    cand = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "candidate"}
    )

    await executor.execute_approved_change(_action(str(cand.id)))

    # The version IS published despite the deploy failing.
    published = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert published is not None
    assert str(published.id) == str(cand.id)

    # The Action is marked failed (deploy failed).
    assert "act-1" in store.failed
    assert "deploy failed" in store.failed["act-1"]
    assert "act-1" not in store.executed


async def test_reject_discards_candidate_and_leaves_published(
    beanie_test_db, versions_journal, monkeypatch
) -> None:
    """REJECT = DISCARD: the candidate is reverted, the published pointer is
    untouched."""
    store = _FakeStore()
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    # A live published version, plus a candidate draft.
    live = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "live"}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(live.id)
    )
    cand = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "candidate"}
    )

    await executor.discard_rejected_change(_action(str(cand.id)))

    # The candidate is reverted.
    from pocketpaw_ee.versions.models import ArtifactVersion

    row = await ArtifactVersion.get(str(cand.id))
    assert row.status == "reverted"

    # The published pointer is still the live version — untouched.
    published = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert published is not None
    assert str(published.id) == str(live.id)
    assert published.content == {"v": "live"}


async def test_reject_clears_all_accumulated_drafts(
    beanie_test_db, versions_journal, monkeypatch
) -> None:
    """P2a: ONE reject (discard_rejected_change) must clear ALL drafts above the
    published pointer — not just the candidate the Action names — so
    has_unpublished_changes goes false on the first click instead of after N.

    The action's ``to_version_id`` names the latest draft, but a pocket edited N
    times (back-handling the pre-fix accumulated state) has N draft rows above
    published. After the fix the discard reverts every one in a single pass.

    Reproduce-first: pre-fix discard_rejected_change reverts only to_version_id,
    so the other accumulated drafts remain → get_draft is non-None → fails.
    """
    store = _FakeStore()
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)

    # A live published version.
    live = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "live"}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(live.id)
    )

    # Force three raw draft rows above published (the already-accumulated state a
    # pre-fix pocket is in on disk), independent of the supersede behaviour.
    from pocketpaw_ee.versions.models import ArtifactVersion

    head = await versions._latest_in_branch(
        scope_type="pocket", scope_id=POCKET, branch_name="main"
    )
    next_no = head.version_no + 1
    cands = []
    for j in range(3):
        row = ArtifactVersion(
            scope_type="pocket",
            scope_id=POCKET,
            workspace_id=WS,
            branch="main",
            version_no=next_no + j,
            content={"raw": j},
            status="draft",
        )
        await row.insert()
        cands.append(row)

    # The Action names only the LATEST draft as to_version_id. The publish flow's
    # blob is on the "main" lineage (request_publish_pocket stamps branch="main"),
    # so build a main-branch action here.
    main_action = SimpleNamespace(
        id="act-discard-all",
        pocket_id=POCKET,
        parameters={
            "_artifact_change": {
                "scope_type": "pocket",
                "scope_id": POCKET,
                "branch": "main",
                "from_version_id": str(live.id),
                "to_version_id": str(cands[-1].id),
                "workspace": WS,
                "user_id": USER,
            }
        },
    )
    await executor.discard_rejected_change(main_action)

    # ALL drafts above published are gone → get_draft is None on click 1.
    assert await versions.get_draft(scope_type="pocket", scope_id=POCKET) is None

    # The published pointer is untouched.
    published = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert published is not None
    assert str(published.id) == str(live.id)
