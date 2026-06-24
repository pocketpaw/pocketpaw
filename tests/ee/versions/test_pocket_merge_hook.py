# tests/ee/versions/test_pocket_merge_hook.py
# Created: 2026-06-18 (feat/branch-primitive-versions, BP-1) — proves the
# pocket-content write path records a draft ArtifactVersion.
#
# What this pins:
#   * Mutating a pocket's rippleSpec via the service-level ``merge_spec`` path
#     persists a NEW draft ArtifactVersion (scope_type="pocket", scope_id =
#     the pocket id) carrying the merged spec snapshot.
#   * The existing merge still works (ok:true, the spec persists) — the version
#     write is additive, not a replacement.
#   * A second merge produces a second, monotonically-numbered version.
#
# Posture: same collaborator-stubbing as tests/cloud/pockets/test_merge_spec.py
# (mock the DB-bound + catalog + emit collaborators so merge_spec runs without
# the full auth/catalog plumbing), BUT against a real ``beanie_test_db`` so the
# versions.write_draft call inside the hook persists a real ArtifactVersion row
# we can query back. The fake pocket doc supplies the id + workspace the
# version write keys off.
from __future__ import annotations

import copy

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.versions import service as versions
from pocketpaw_ee.versions.models import ArtifactVersion

pytestmark = pytest.mark.asyncio

POCKET_ID = "pocket-bp1"
WS_ID = "ws-bp1"
USER_ID = "user-bp1"


class _FakeDoc:
    """In-memory stand-in for the Pocket doc — only the attributes the
    merge_spec path (and the BP-1 version hook) read/write."""

    def __init__(self, spec: dict) -> None:
        self.rippleSpec = spec
        self.id = POCKET_ID
        self.workspace = WS_ID
        self.save_calls = 0

    async def save(self) -> None:
        self.save_calls += 1


def _base_spec() -> dict:
    return {
        "version": "1.0",
        "state": {"draft": ""},
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column", "gap": 12},
            "children": [
                {
                    "id": "n_input001",
                    "type": "input",
                    "props": {"value": "{{state.draft}}", "label": "Draft"},
                },
            ],
        },
    }


def _wire_stubs(monkeypatch: pytest.MonkeyPatch, fake_doc: _FakeDoc) -> None:
    """Stub the DB-bound + catalog + emit collaborators so merge_spec runs
    in-memory against ``fake_doc`` — leaving ONLY the BP-1 version hook to hit
    the real ``beanie_test_db``."""

    async def _fake_fetch(pocket_id: str):  # type: ignore[no-untyped-def]
        return fake_doc

    async def _fake_resolved_wire_dict(doc, viewer_user_id):  # type: ignore[no-untyped-def]
        return {"id": doc.id, "rippleSpec": doc.rippleSpec}

    async def _fake_event_payload(doc):  # type: ignore[no-untyped-def]
        return {"recipient_ids": [], "pocket": {"id": doc.id}}

    async def _fake_emit(event):  # type: ignore[no-untyped-def]
        return None

    async def _noop_gate_catalog(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(pockets_service, "_fetch_pocket", _fake_fetch)
    monkeypatch.setattr(pockets_service, "_pocket_to_domain", lambda doc: object())
    monkeypatch.setattr(pockets_service, "_check_domain_edit_access", lambda *a, **k: None)
    monkeypatch.setattr(pockets_service, "_gate_catalog", _noop_gate_catalog)
    monkeypatch.setattr(pockets_service, "_resolved_wire_dict", _fake_resolved_wire_dict)
    monkeypatch.setattr(pockets_service, "_pocket_event_payload", _fake_event_payload)
    monkeypatch.setattr(pockets_service, "emit", _fake_emit)


async def test_merge_records_pocket_draft_version(beanie_test_db, monkeypatch: pytest.MonkeyPatch):
    fake_doc = _FakeDoc(copy.deepcopy(_base_spec()))
    _wire_stubs(monkeypatch, fake_doc)

    # No version exists for this pocket yet.
    assert await versions.get_draft(scope_type="pocket", scope_id=POCKET_ID) is None

    body = {"merge": {"state": {"draft": "hello"}}}
    result = await pockets_service.merge_spec(
        workspace_id=WS_ID, user_id=USER_ID, pocket_id=POCKET_ID, body=body
    )

    # The existing merge still works.
    assert result["ok"] is True, result
    assert fake_doc.save_calls == 1
    assert fake_doc.rippleSpec["state"]["draft"] == "hello"

    # A draft ArtifactVersion now exists for the pocket, snapshotting the spec.
    draft = await versions.get_draft(scope_type="pocket", scope_id=POCKET_ID)
    assert draft is not None
    assert draft.scope_type == "pocket"
    assert draft.scope_id == POCKET_ID
    assert draft.workspace_id == WS_ID
    assert draft.author == USER_ID
    assert draft.status == "draft"
    assert draft.version_no == 1
    assert draft.content["state"]["draft"] == "hello"


async def test_second_merge_writes_second_version(beanie_test_db, monkeypatch: pytest.MonkeyPatch):
    fake_doc = _FakeDoc(copy.deepcopy(_base_spec()))
    _wire_stubs(monkeypatch, fake_doc)

    await pockets_service.merge_spec(
        workspace_id=WS_ID,
        user_id=USER_ID,
        pocket_id=POCKET_ID,
        body={"merge": {"state": {"draft": "first"}}},
    )
    await pockets_service.merge_spec(
        workspace_id=WS_ID,
        user_id=USER_ID,
        pocket_id=POCKET_ID,
        body={"merge": {"state": {"draft": "second"}}},
    )

    log = await versions.list_versions(scope_type="pocket", scope_id=POCKET_ID)
    assert [v.version_no for v in log] == [2, 1]
    assert log[0].content["state"]["draft"] == "second"


async def test_version_hook_failure_does_not_break_merge(
    beanie_test_db, monkeypatch: pytest.MonkeyPatch
):
    """The version write is best-effort — if it raises, the merge still
    succeeds (the snapshot is an additive audit layer, never a gate)."""
    fake_doc = _FakeDoc(copy.deepcopy(_base_spec()))
    _wire_stubs(monkeypatch, fake_doc)

    async def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("version store down")

    monkeypatch.setattr(versions, "write_draft", _boom)

    result = await pockets_service.merge_spec(
        workspace_id=WS_ID,
        user_id=USER_ID,
        pocket_id=POCKET_ID,
        body={"merge": {"state": {"draft": "still works"}}},
    )
    assert result["ok"] is True
    assert fake_doc.save_calls == 1
    # No version row was written, but the merge persisted.
    assert await ArtifactVersion.find().count() == 0
