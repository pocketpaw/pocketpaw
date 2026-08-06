# tests/cloud/test_instinct_correlation_columns.py
# Created: 2026-08-05 (T-3, coupling-gap wave) — the EE half of the first-class
# Decision-Graph chain-id columns. The OSS store half lives in
# tests/instinct/test_correlation_columns.py.
#
# What this pins — for all THREE gated proposers that hold a chain id:
#   * external_actions.propose_external_action (``_external_action``),
#   * pockets.instinct_bridge.propose_pocket_write (``_pocket_write``),
#   * agent.mcp_servers.belt._propose_change_handler (``_code_change``):
#   1. the ``correlation_id`` COLUMN is written at INSERT and the per-kind blob
#      keeps its own copy (blob-schema compat — no reader breaks);
#   2. the join no longer depends on the post-propose back-write: with the
#      back-write forced to FAIL, the Action is still joinable to its chain via
#      the column (pre-T-3 this left the row permanently unjoinable);
#   3. a parked write with NO chain id stores NULL — nothing is invented;
#   4. the cloud instinct router's action view exposes both ids additively, so
#      the FE can join a Tray row to its Decision chain without parsing the
#      untyped ``parameters`` blob.
#
# ``pocketpaw_ee`` is import-skipped on an OSS-only install. The Instinct store
# is patched to a tmp file; nothing touches a real connector, journal, or repo
# remote.

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.belt as belt  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.cloud.external_actions import propose as ea_propose  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.pockets import instinct_bridge  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
USER = "u1"
PARKED_CORR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore wired everywhere the proposers read it (each
    lazy-imports ``pocketpaw.stores.get_instinct_store``); the router's
    ``_store`` indirection is pointed at it too for the read-path test."""
    st = InstinctStore(tmp_path / "instinct_chain_ids.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    monkeypatch.setattr("pocketpaw_ee.instinct.router._store", lambda *a, **k: st)
    return st


# ---------------------------------------------------------------------------
# external_actions — the kind whose back-write was the original failure mode
# ---------------------------------------------------------------------------


async def test_external_action_propose_writes_column_and_keeps_blob(store, monkeypatch):
    """propose_external_action lands the chain id on the COLUMN at INSERT, and
    the schema-1 blob still carries its own copy.

    The emit is stubbed to return None so the post-propose back-write never
    runs — otherwise this would pass on the OLD behaviour too (the back-write
    would fill the column). Mutation that must break it: drop
    ``correlation_id=corr`` from the ``store.propose`` call.
    """
    monkeypatch.setattr(ea_propose, "_emit_agent_proposed", lambda **kwargs: None)

    action_id = await ea_propose.propose_external_action(
        workspace_id=WS,
        connector_name="crm",
        action="approveApplication",
        params={"application_id": "app-7"},
        requested_by=USER,
    )
    action = await store.get_action(action_id)
    assert action is not None

    blob = action.parameters["_external_action"]
    assert blob["correlation_id"], "the blob copy must survive (schema-1 compat)"
    # The column is the joinable record and agrees with the blob.
    assert action.correlation_id == blob["correlation_id"]


async def test_external_action_join_survives_back_write_failure(store, monkeypatch):
    """THE regression this slice exists for: force the post-propose back-write to
    fail and the Action is STILL joinable to its Decision chain via the column.

    Pre-T-3 the correlation id only reached the row through this best-effort
    write, so a failure left the action permanently unjoinable.
    """
    seen: dict[str, Any] = {}

    async def _boom(**kwargs):
        seen["called"] = True
        raise RuntimeError("simulated back-write failure (locked db)")

    # (``seen`` is asserted after the propose below — it proves the failure was
    # injected at the real back-write seam, not skipped by a code path change.)

    # Fail the back-write at its own seam — everything before it is the real
    # production path.
    monkeypatch.setattr(ea_propose, "_persist_chain_ids", _boom)

    with pytest.raises(RuntimeError):
        # The emit path calls the back-write directly; a raising stub surfaces
        # here, which is stricter than production's swallow — we only need the
        # row to already carry the column by this point.
        await ea_propose.propose_external_action(
            workspace_id=WS,
            connector_name="crm",
            action="approveApplication",
            params={"application_id": "app-9"},
            requested_by=USER,
        )

    # The stub really fired at the back-write seam — the RuntimeError above came
    # from OUR injection, not from an unrelated code path.
    assert seen.get("called") is True

    # The proposal DID land (propose committed before the emit/back-write) and
    # carries its correlation on the column despite the failed back-write.
    rows = await store.list_actions(pocket_id=WS)
    assert len(rows) == 1
    assert rows[0].correlation_id, "the chain join must not depend on the back-write"
    assert rows[0].parameters["_external_action"]["correlation_id"] == rows[0].correlation_id


async def test_external_action_back_write_fills_event_id_column(store, monkeypatch):
    """When the back-write DOES run, it writes the ``agent.proposed`` event id to
    the column (not just the blob) — the id that is genuinely unknowable at
    insert time."""
    fake_event_id = "12345678-1234-5678-1234-567812345678"
    monkeypatch.setattr(
        ea_propose,
        "_emit_agent_proposed",
        lambda **kwargs: __import__("uuid").UUID(fake_event_id),
    )
    action_id = await ea_propose.propose_external_action(
        workspace_id=WS,
        connector_name="crm",
        action="approveApplication",
        requested_by=USER,
    )
    action = await store.get_action(action_id)
    assert action.proposed_event_id == fake_event_id
    # Blob copies stay in sync for schema-1 readers.
    assert action.parameters["_external_action"]["proposed_event_id"] == fake_event_id
    assert action.parameters["_external_action"]["correlation_id"] == action.correlation_id


# ---------------------------------------------------------------------------
# instinct_bridge — the ``_pocket_write`` parked-write kind
# ---------------------------------------------------------------------------


def _pocket() -> dict[str, Any]:
    return {"_id": "p1", "workspace": WS, "name": "Sales", "owner": USER}


async def test_pocket_write_propose_writes_column_from_parked_correlation(store):
    """The executor-minted chain id on the parked write lands on the COLUMN and
    stays on the schema-2 blob."""
    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write={
            "action": "create_lead",
            "method": "POST",
            "path": "/leads",
            "params": {"name": "acme"},
            "correlation_id": PARKED_CORR,
        },
        requested_by=USER,
    )
    action = await store.get_action(action_id)
    assert action is not None
    assert action.correlation_id == PARKED_CORR
    assert action.parameters["_pocket_write"]["correlation_id"] == PARKED_CORR


async def test_pocket_write_without_correlation_stores_null(store):
    """A parked write with no chain id stores NULL — nothing is invented."""
    action_id = await instinct_bridge.propose_pocket_write(
        pocket=_pocket(),
        backend_config=None,
        parked_write={"action": "create_lead", "method": "POST", "path": "/leads"},
        requested_by=USER,
    )
    action = await store.get_action(action_id)
    assert action is not None
    assert action.correlation_id is None
    assert action.proposed_event_id is None


# ---------------------------------------------------------------------------
# belt — the ``_code_change`` kind
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal git repo — propose only validates that ``.git`` exists inside
    the allowlist; nothing is applied or pushed here."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    return work


@pytest.fixture
def allowlist(repo: Path, monkeypatch) -> None:
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(repo.parent)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


async def test_belt_propose_writes_column_and_keeps_blob(store, repo, allowlist, monkeypatch):
    """The belt develop-station propose lands its minted chain id on the COLUMN
    at INSERT; the schema-2 blob keeps its copy.

    The emit is stubbed to return None so the post-propose back-write never
    runs — otherwise this would pass on the OLD behaviour too. Mutation that
    must break it: drop ``correlation_id=str(correlation_id)`` from the
    ``store.propose`` call in ``_propose_change_handler``.
    """
    monkeypatch.setattr(belt, "_emit_agent_proposed", lambda **kwargs: None)

    tokens = attach_agent_identity(workspace_id=WS, user_id=USER, session_mongo_id="sess-1")
    try:
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": (
                    "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
                    " def hello():\n-    return 'hi'\n+    return 'hello'\n"
                ),
                "summary": "Friendlier greeting.",
                "task": "Make the greeting friendlier.",
            }
        )
    finally:
        detach_agent_identity(tokens)

    assert res.get("is_error") is not True, res
    rows = await store.list_actions(pocket_id=WS)
    assert len(rows) == 1
    action = rows[0]
    blob = action.parameters["_code_change"]
    assert blob["correlation_id"], "the blob copy must survive (schema-2 compat)"
    assert action.correlation_id == blob["correlation_id"]


# ---------------------------------------------------------------------------
# read path — the cloud instinct router's action view
# ---------------------------------------------------------------------------


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


async def test_router_action_view_exposes_chain_ids(store, monkeypatch):
    """Both list views expose ``correlation_id`` + ``proposed_event_id`` on the
    wire (additively) so the FE can join a Tray row to its Decision chain."""
    action_id = await ea_propose.propose_external_action(
        workspace_id=WS,
        connector_name="crm",
        action="approveApplication",
        requested_by=USER,
    )
    stored = await store.get_action(action_id)

    client = _make_client(monkeypatch)

    listed = client.get("/instinct/actions", params={"pocket_id": WS})
    assert listed.status_code == 200, listed.text
    row = next(a for a in listed.json()["actions"] if a["id"] == action_id)
    assert "correlation_id" in row
    assert "proposed_event_id" in row
    assert row["correlation_id"] == stored.correlation_id

    pending = client.get("/instinct/actions/pending", params={"pocket_id": WS})
    assert pending.status_code == 200, pending.text
    prow = next(a for a in pending.json() if a["id"] == action_id)
    assert prow["correlation_id"] == stored.correlation_id
