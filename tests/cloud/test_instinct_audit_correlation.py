# tests/cloud/test_instinct_audit_correlation.py
# Created: 2026-08-06 (T-4, coupling-gap wave) — the EE half of the audit-ledger
# correlation id. The OSS store half (population, NULL handling, hash-material
# freeze, pre-T-4 migration) lives in tests/instinct/test_audit_correlation.py.
#
# What this pins, end to end through a REAL gated proposer:
#   1. a gated external action's audit rows carry the same correlation_id the
#      Action row carries, so "approved by whom" joins to "why" without parsing
#      the untyped ``parameters`` blob;
#   2. the cloud instinct router's audit views expose it additively — both the
#      list (``GET /instinct/audit``) and the single-entry fetch the Why? drawer
#      uses (``GET /instinct/audit/{id}``) — with no router change, because both
#      serialize the OSS AuditEntry model;
#   3. the tamper-evident chain still verifies over those rows
#      (``GET /instinct/audit/verify``), which is the whole point of keeping the
#      column out of the hash material.
#
# ``pocketpaw_ee`` is import-skipped on an OSS-only install. The Instinct store
# is patched to a tmp file; nothing touches a real connector or journal.

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.external_actions import propose as ea_propose  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
USER = "u1"


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_audit_chain.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: st)
    return st


class _FakeUser:
    def __init__(self, user_id: str = USER, workspace_id: str = WS) -> None:
        self.id = user_id
        self.active_workspace = workspace_id

        class _M:
            def __init__(self, ws):
                self.workspace = ws
                self.role = "admin"

        self.workspaces = [_M(workspace_id)]


def _make_client(monkeypatch) -> TestClient:
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: _FakeUser()
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app)


async def test_audit_views_expose_the_chain_key(store, monkeypatch):
    """A gated proposal's audit trail joins to its Decision chain on the wire."""
    action_id = await ea_propose.propose_external_action(
        workspace_id=WS,
        connector_name="crm",
        action="approveApplication",
        requested_by=USER,
    )
    stored = await store.get_action(action_id)
    assert stored is not None
    assert stored.correlation_id  # the proposer minted a chain — T-3's column

    approved = await store.approve(action_id, approver=USER)
    assert approved is not None

    client = _make_client(monkeypatch)

    listed = client.get("/instinct/audit", params={"pocket_id": WS})
    assert listed.status_code == 200, listed.text
    rows = listed.json()["entries"]
    assert rows, "expected the propose + approve audit rows"
    for row in rows:
        assert "correlation_id" in row
        assert row["correlation_id"] == stored.correlation_id

    # The single-entry fetch behind the Why? drawer carries it too.
    one = client.get(f"/instinct/audit/{rows[0]['id']}")
    assert one.status_code == 200, one.text
    assert one.json()["entry"]["correlation_id"] == stored.correlation_id

    # And the ledger still proves itself — the column is not chain material.
    verify = client.get("/instinct/audit/verify")
    assert verify.status_code == 200, verify.text
    assert verify.json()["intact"] is True
