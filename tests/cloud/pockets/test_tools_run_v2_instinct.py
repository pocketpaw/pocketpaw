# tests/cloud/pockets/test_tools_run_v2_instinct.py — feat/invoke-tool-v1 (v2).
# Created: 2026-06-15 — the v2 WRITE-via-Instinct ROUND-TRIP for invoke_tool.
#
# This pins the two halves of the v2 contract end-to-end against a REAL
# InstinctStore (tmp file) — no HTTP router, but the real propose helper and the
# real approve-side executor, so the propose-produced Action is proven
# consumable by the approve path:
#
#   1. PROPOSE (the security rule): invoke a WRITE connector grant through the
#      real `tool_executor.run_tool`. The real `propose_external_action` files a
#      PENDING Instinct Action; `connectors.service.execute` is NEVER called
#      during the invoke (the human gates the write). `run_tool` returns
#      code="instinct_pending" + the real proposed action_id, and the stored
#      Action's `_external_action` blob carries the exact connector/action/params
#      that were clicked.
#
#   2. APPROVE → EXECUTE: approve that SAME Action via the existing
#      `execute_approved_external_action` path (the path the instinct router runs
#      on POST /instinct/actions/{id}/approve). With `connectors.service.execute`
#      spied, assert it IS awaited on approve, with the proposed connector +
#      action + params (re-validated by the executor). The connector write fires
#      ONLY on approve — never during the click.
#
# The store is patched everywhere the gate reads it (the propose helper + the
# executor both lazy-import `pocketpaw.stores.get_instinct_store`), mirroring
# tests/cloud/test_external_action_gate.py. `connectors.service.execute` is the
# single connector chokepoint both the read path and the approve-side write
# bottom out at, so spying it proves "fires on approve, not on click".

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.connectors.domain import ConnectorActionInfo  # noqa: E402
from pocketpaw_ee.cloud.external_actions import executor as ea_executor  # noqa: E402
from pocketpaw_ee.cloud.pockets import tool_executor  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "ws-alpha"
POCKET = "pocket-1"
USER = "user-alice"
TOOL = "connector:github:create_issue"


class _FakeExecuteResponse:
    """Duck-typed ExecuteActionResponse the approve-side executor reads
    (.success / .data / .error)."""

    def __init__(self, *, success: bool, data: Any = None, error: str | None = None):
        self.success = success
        self.data = data
        self.error = error
        self.records_affected = 0
        self.execution_mode = "cloud"


class _SpyConnectorService:
    """Records connector calls + returns a scripted response. Never touches a
    real connector. Patched onto `connectors.service.execute`."""

    def __init__(self, response: _FakeExecuteResponse | None = None):
        self.calls: list[dict[str, Any]] = []
        self._response = response or _FakeExecuteResponse(success=True, data={"number": 42})

    async def execute(self, workspace_id, name, body, *, user_id=None):
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "name": name,
                "action": body.action,
                "params": dict(body.params),
                "scope": body.scope,
                "pocket_id": body.pocket_id,
                "user_id": user_id,
            }
        )
        return self._response


def _write_trust(action: str = "create_issue") -> ConnectorActionInfo:
    return ConnectorActionInfo(
        name=action,
        description="Create an issue",
        trust_level="confirm",
        execution_mode="cloud",
        is_read=False,
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired where the propose helper and
    the executor both read it (`pocketpaw.stores.get_instinct_store`)."""
    st = InstinctStore(tmp_path / "instinct_tools_run_v2.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: st)
    return st


@pytest.mark.asyncio
async def test_invoke_write_proposes_then_approve_fires_execute(store, monkeypatch):
    """THE v2 round-trip: a WRITE invoke proposes (execute NOT called); approving
    the proposed Action fires `connectors.service.execute` with the proposed
    connector/action/params.

    Proves the whole point of v2: the click NEVER fires the write inline — it
    files a pending Action a human must approve; the connector write runs ONLY on
    approve, through the existing external-action executor (re-validated)."""
    spy = _SpyConnectorService(
        response=_FakeExecuteResponse(success=True, data={"number": 42, "title": "Bug"})
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.connectors.service.execute", spy.execute)

    # --- Half 1: PROPOSE -----------------------------------------------------
    # The bind + trust gates resolve to a bound WRITE action; `execute` (the spy)
    # is the chokepoint we assert never fires during the click.
    with (
        patch(
            "pocketpaw_ee.cloud.connectors.service.is_connector_bound_to_pocket",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "pocketpaw_ee.cloud.connectors.service.get_action_trust",
            new=AsyncMock(return_value=_write_trust()),
        ),
    ):
        result = await tool_executor.run_tool(
            workspace_id=WS,
            pocket_id=POCKET,
            user_id=USER,
            tool=TOOL,
            args={"title": "Bug", "body": "It broke"},
            allowed_tools=[TOOL],
        )

    # The click did NOT fire the connector write — it proposed.
    assert spy.calls == [], "the WRITE must NOT fire inline on click — the human gates it"
    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    assert result["status"] == 202
    action_id = result["proposed_action_id"]
    assert action_id, "the pending response must carry the real proposed action_id"

    # A real PENDING Action was filed, carrying the clicked connector/action/params.
    action = await store.get_action(action_id)
    assert action is not None
    assert action.status == ActionStatus.PENDING
    blob = action.parameters["_external_action"]
    assert blob["connector_name"] == "github"
    assert blob["action"] == "create_issue"
    assert blob["params"] == {"title": "Bug", "body": "It broke"}
    assert blob["requested_by"] == USER
    assert blob["scope"] == "pocket"
    assert blob["pocket_id"] == POCKET

    # --- Half 2: APPROVE → EXECUTE ------------------------------------------
    # Approve through the store (status flip) then run the SAME executor the
    # instinct router fires on approve. NOW the connector write runs.
    approved = await store.approve(action_id, approver=USER)
    await ea_executor.execute_approved_external_action(approved)

    # THE assertion: execute fired ONCE on approve, with the proposed call.
    assert len(spy.calls) == 1, "the connector write must fire exactly once on approve"
    call = spy.calls[0]
    assert call["workspace_id"] == WS
    assert call["name"] == "github"
    assert call["action"] == "create_issue"
    assert call["params"] == {"title": "Bug", "body": "It broke"}

    # The Action is now EXECUTED (the write landed).
    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED
