# tests/ee/versions/test_merge_gate_service.py
# Created: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — coverage for
# the merge-gate state transitions on the versions service (mark_merged /
# discard) and the svelte-edit draft-version hook (Part D).
#
# What this pins:
#   * mark_merged flips an accepted candidate to status="merged" and emits
#     artifact.version.merged; it does NOT move the published pointer (publish
#     already did) and is scope-checked.
#   * discard flips a rejected candidate to status="reverted" and leaves the
#     PUBLISHED pointer untouched (a rejection never moves what is live).
#   * Both raise on a scope/workspace mismatch (the defensive service check).
#   * A svelte set_svelte_source_file edit records a draft ArtifactVersion
#     (scope_type="pocket") snapshotting the source map (Part D).
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.versions import service as versions

pytestmark = pytest.mark.asyncio

WS = "ws-merge"
POCKET = "pocket-merge"
USER = "user-merge"


# ---------------------------------------------------------------------------
# mark_merged / discard — the merge-gate state transitions
# ---------------------------------------------------------------------------


async def test_mark_merged_flips_candidate_and_keeps_published(
    beanie_test_db, versions_journal
) -> None:
    """mark_merged sets status='merged' on the candidate. It does not change the
    published pointer (publish moved it first in the real flow)."""
    # A published version already lives on main.
    pub = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": 1}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(pub.id)
    )

    # A candidate on a branch is published (the merge target), then marked merged.
    cand = await versions.branch(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, new_branch="cand"
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(cand.id)
    )
    merged = await versions.mark_merged(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(cand.id)
    )
    assert merged.status == "merged"

    # The merge event landed on the journal.
    events = versions_journal.query(action="artifact.version.merged")
    assert len(events) == 1
    assert events[0].payload["version_id"] == str(cand.id)


async def test_discard_reverts_candidate_and_leaves_published_untouched(
    beanie_test_db, versions_journal
) -> None:
    """discard flips the candidate to reverted; the published pointer on main is
    untouched (rejection must never move what is live)."""
    pub = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "live"}
    )
    await versions.publish(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(pub.id)
    )

    cand = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"v": "candidate"}
    )
    discarded = await versions.discard(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, version_id=str(cand.id)
    )
    assert discarded.status == "reverted"

    # The published pointer is still the original live version — untouched.
    live = await versions.get_published(scope_type="pocket", scope_id=POCKET)
    assert live is not None
    assert str(live.id) == str(pub.id)
    assert live.content == {"v": "live"}

    events = versions_journal.query(action="artifact.version.discarded")
    assert len(events) == 1


async def test_mark_merged_rejects_cross_workspace(beanie_test_db) -> None:
    """A version mutation must not cross to another workspace — the service-level
    defensive scope check raises ValueError."""
    row = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={}
    )
    with pytest.raises(ValueError):
        await versions.mark_merged(
            scope_type="pocket", scope_id=POCKET, workspace_id="ws-OTHER", version_id=str(row.id)
        )


async def test_discard_rejects_cross_workspace(beanie_test_db) -> None:
    row = await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={}
    )
    with pytest.raises(ValueError):
        await versions.discard(
            scope_type="pocket", scope_id=POCKET, workspace_id="ws-OTHER", version_id=str(row.id)
        )


# ---------------------------------------------------------------------------
# Part D — svelte source edit writes a draft version
# ---------------------------------------------------------------------------


class _FakeSvelteDoc:
    """In-memory Pocket doc carrying a svelte source map — only the attributes
    set_svelte_source_file + the BP-3 version hook read/write."""

    def __init__(self, source: dict) -> None:
        self.id = POCKET
        self.workspace = WS
        self.engine = "svelte"
        self.source = source
        self.save_calls = 0

    async def save(self) -> None:
        self.save_calls += 1


def _wire_stubs(monkeypatch: pytest.MonkeyPatch, fake_doc: _FakeSvelteDoc) -> None:
    async def _fake_fetch(pocket_id: str):  # type: ignore[no-untyped-def]
        return fake_doc

    async def _fake_resolved_wire_dict(doc, viewer_user_id):  # type: ignore[no-untyped-def]
        return {"id": doc.id, "source": doc.source}

    async def _fake_event_payload(doc):  # type: ignore[no-untyped-def]
        return {"recipient_ids": [], "pocket": {"id": doc.id}}

    async def _fake_emit(event):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(pockets_service, "_fetch_pocket", _fake_fetch)
    monkeypatch.setattr(pockets_service, "_pocket_to_domain", lambda doc: object())
    monkeypatch.setattr(pockets_service, "_check_domain_edit_access", lambda *a, **k: None)
    monkeypatch.setattr(pockets_service, "_resolved_wire_dict", _fake_resolved_wire_dict)
    monkeypatch.setattr(pockets_service, "_pocket_event_payload", _fake_event_payload)
    monkeypatch.setattr(pockets_service, "emit", _fake_emit)


async def test_svelte_edit_records_draft_version(
    beanie_test_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set_svelte_source_file edit writes a draft ArtifactVersion snapshotting
    the source map (BP-3 Part D — BP-1 only versioned the rippleSpec write)."""
    fake_doc = _FakeSvelteDoc({"src/Hero.svelte": "<h1>old</h1>"})
    _wire_stubs(monkeypatch, fake_doc)

    # No version exists for this pocket yet.
    assert await versions.get_draft(scope_type="pocket", scope_id=POCKET) is None

    wire, previous = await pockets_service.set_svelte_source_file(
        POCKET, USER, component_path="src/Hero.svelte", new_source="<h1>new</h1>"
    )

    # The edit persisted + returned the prior contents.
    assert fake_doc.save_calls == 1
    assert previous == "<h1>old</h1>"
    assert fake_doc.source["src/Hero.svelte"] == "<h1>new</h1>"

    # A draft ArtifactVersion now snapshots the full source map.
    draft = await versions.get_draft(scope_type="pocket", scope_id=POCKET)
    assert draft is not None
    assert draft.scope_type == "pocket"
    assert draft.scope_id == POCKET
    assert draft.workspace_id == WS
    assert draft.author == USER
    assert draft.status == "draft"
    assert draft.content == {"src/Hero.svelte": "<h1>new</h1>"}


async def test_svelte_edit_version_failure_does_not_break_edit(
    beanie_test_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version write is best-effort — if it raises, the edit still
    succeeds (additive history layer, never a gate)."""
    fake_doc = _FakeSvelteDoc({"src/Hero.svelte": "<h1>old</h1>"})
    _wire_stubs(monkeypatch, fake_doc)

    async def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("version store down")

    monkeypatch.setattr(versions, "write_draft", _boom)

    wire, previous = await pockets_service.set_svelte_source_file(
        POCKET, USER, component_path="src/Hero.svelte", new_source="<h1>still works</h1>"
    )
    assert fake_doc.save_calls == 1
    assert fake_doc.source["src/Hero.svelte"] == "<h1>still works</h1>"
