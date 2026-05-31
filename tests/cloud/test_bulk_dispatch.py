# tests/cloud/test_bulk_dispatch.py
# Created: 2026-05-28 (feat/wave-3b-action-pipeline) — pins the
# Wave 3b bulk action dispatch pipeline. This is the EE-side library
# wrapper around the OSS ``plan_bulk_execution`` planner.
#
# What this pins (RFC 03 v2 §"Bulk action execution model"):
#   * ONE approval blesses the whole batch — N approval-needing rows
#     yield exactly ONE InstinctApproval row carrying every row_id.
#   * Mixed verdict bucketing: block rows go to ``blocked``, execute
#     rows go to ``executions``, approval rows consolidate into a
#     single ``batch_approval_id``.
#   * All-block / all-execute / all-approval / empty paths.
#   * Tenant isolation: workspace A cannot dispatch a workspace B
#     pocket's bulk action.
#   * Non-bulk action raises ``ValidationError``.
#   * Emit-on-write: ``BulkActionDispatched`` event fires exactly
#     once per dispatch_bulk_action call.
#
# Out of scope for Wave 3b (and therefore NOT pinned here):
#   * The approver re-entry that re-runs each row with
#     from_instinct=True after the batch approval lands. Wave 3c.
#   * Outcome event emission per executed row. Wave 3c.
#   * UI for triggering bulk runs.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    BulkActionDispatched,
    InstinctApprovalCreated,
)
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.models.pocket_backend import (
    AllowedWrite as _AllowedWriteDoc,
)
from pocketpaw_ee.cloud.models.pocket_backend import (
    PocketBackendCredential as _BackendCredentialDoc,
)
from pocketpaw_ee.cloud.pockets import bulk_dispatch
from pocketpaw_ee.cloud.pockets import service as pockets_service

from pocketpaw.bundled_templates import PocketTemplate

pytestmark = pytest.mark.usefixtures("mongo_db")

FROZEN_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture helpers — minimal valid v2 PocketTemplate with a bulk action,
# plus a real Beanie Pocket + PocketBackendCredential so the service
# wrapper's pocket fetch + creds lookup find them.
# ---------------------------------------------------------------------------


def _template(
    *,
    instinct_policy: str = "auto",
    rules: list[dict] | None = None,
    action_name: str = "mark_done",
    kind: str = "bulk",
) -> PocketTemplate:
    raw: dict[str, Any] = {
        "schema_version": "2",
        "name": "test-template",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "test fixture",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [{"field": "value", "widget": "number"}],
            "id_field": "id",
        },
        "actions": [
            {
                "name": action_name,
                "label": "Do Thing",
                "kind": kind,
                "instinct_policy": instinct_policy,
            }
        ],
    }
    if rules is not None:
        raw["instinct_rules"] = {"rules": rules}
    return PocketTemplate.model_validate(raw)


def _ripple_spec_with_action(action_name: str = "mark_done") -> dict[str, Any]:
    """RippleSpec with a single ``write_binding`` action keyed by name."""
    return {
        "actions": {
            action_name: {
                "kind": "write_binding",
                "method": "POST",
                "path": "/items",
            }
        }
    }


async def _make_pocket(
    *,
    workspace: str = "w1",
    owner: str = "u1",
    action_name: str = "mark_done",
    visibility: str = "workspace",
) -> str:
    """Insert a real Pocket Beanie doc and return its id."""
    doc = _PocketDoc(
        workspace=workspace,
        name="test pocket",
        owner=owner,
        rippleSpec=_ripple_spec_with_action(action_name),
        visibility=visibility,
        widgets=[],
    )
    await doc.insert()
    return str(doc.id)


async def _make_backend(
    *,
    pocket_id: str,
    workspace_id: str = "w1",
) -> None:
    """Insert a PocketBackendCredential with allowlisted POST /items*."""
    await _BackendCredentialDoc(
        pocket_id=pocket_id,
        workspace_id=workspace_id,
        base_url="https://example.test",
        auth_type="none",
        auth_header=None,
        encrypted_token=None,
        nonce=None,
        salt=None,
        allowed_writes=[
            _AllowedWriteDoc.model_validate({"method": "POST", "path_pattern": "/items*"})
        ],
    ).insert()


@pytest.fixture
def stub_run_action(monkeypatch):
    """Stub ``action_executor.run_action`` to return a fake ok response.

    The bulk-dispatch unit tests pin orchestration logic, not the
    HTTP path. Wave 3a already pins ``run_action`` end-to-end against
    the gate; this fixture short-circuits the call so the EXECUTE
    branch returns a known value and never touches the network.

    Returns a list ``calls`` the test can introspect for assertions.
    """
    calls: list[dict] = []

    async def _stub(**kwargs: Any) -> dict:
        calls.append(kwargs)
        return {
            "ok": True,
            "action": kwargs["action"],
            "status": 200,
            "response": {"ok": True},
            "on_success": [],
            "on_error": [],
        }

    from pocketpaw_ee.cloud.pockets import action_executor

    monkeypatch.setattr(action_executor, "run_action", _stub)
    return calls


# ---------------------------------------------------------------------------
# dispatch_bulk — direct library calls
# ---------------------------------------------------------------------------


async def test_all_execute_rows_fan_out(stub_run_action) -> None:
    """3 rows, all auto-policy, no rules → 3 executions, no approval, no blocked."""
    pocket_id = await _make_pocket()
    await _make_backend(pocket_id=pocket_id)
    template = _template(instinct_policy="auto")

    rows = [
        {"id": "r1", "value": 1},
        {"id": "r2", "value": 2},
        {"id": "r3", "value": 3},
    ]
    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
        now=FROZEN_NOW,
    )

    assert result.total_rows == 3
    assert len(result.executions) == 3
    assert result.blocked == []
    assert result.batch_approval_id is None
    # All three rows actually fired (stub recorded the calls).
    assert len(stub_run_action) == 3
    # Each execution carries the row_id.
    fired_ids = {e.row_id for e in result.executions}
    assert fired_ids == {"r1", "r2", "r3"}


async def test_mixed_bucketing_block_execute_approval(stub_run_action) -> None:
    """3 rows: 1 blocks, 1 executes, 1 needs approval. ONE batch approval."""
    pocket_id = await _make_pocket()
    await _make_backend(pocket_id=pocket_id)
    template = _template(
        instinct_policy="auto",
        rules=[
            # Block wins. Then approval. Then plain execute.
            {"when": "value > 900", "action": "block"},
            {"when": "value > 100", "action": "require_approval"},
        ],
    )

    rows = [
        {"id": "rb", "value": 999},  # BLOCK
        {"id": "re", "value": 1},  # EXECUTE
        {"id": "ra", "value": 200},  # ESCALATE_APPROVAL
    ]
    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
        now=FROZEN_NOW,
    )

    assert result.total_rows == 3
    assert len(result.executions) == 1
    assert result.executions[0].row_id == "re"
    assert len(result.blocked) == 1
    assert result.blocked[0].row_id == "rb"
    assert result.batch_approval_id is not None
    # ONE approval doc, carrying the one approval row.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["id"] == result.batch_approval_id
    # Only the execute row fired through run_action.
    assert len(stub_run_action) == 1


async def test_all_approval_rows_consolidate_to_one_batch(stub_run_action, recording_bus) -> None:
    """3 rows all needing approval → ONE batch_approval_id with all 3 row_ids."""
    pocket_id = await _make_pocket()
    await _make_backend(pocket_id=pocket_id)
    template = _template(
        instinct_policy="require_approval",  # author floor
    )

    rows = [
        {"id": "r1", "value": 1},
        {"id": "r2", "value": 2},
        {"id": "r3", "value": 3},
    ]
    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
        now=FROZEN_NOW,
    )

    assert result.total_rows == 3
    assert result.executions == []
    assert result.blocked == []
    assert result.batch_approval_id is not None
    # Exactly ONE approval row created for the batch.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["id"] == result.batch_approval_id
    # row_data carries the per-row data for all three rows + batch flag.
    rd = approval["row_data"]
    assert rd.get("batch") is True
    # Every row's data is reachable via its id.
    for row in rows:
        rid = row["id"]
        assert rid in rd, f"missing row {rid} in row_data: {rd!r}"
    # No HTTP fired.
    assert stub_run_action == []
    # Exactly ONE InstinctApprovalCreated event.
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1


async def test_all_blocked_rows(stub_run_action) -> None:
    """3 rows all blocking → 0 executions, 3 blocked, no approval."""
    pocket_id = await _make_pocket()
    await _make_backend(pocket_id=pocket_id)
    template = _template(
        instinct_policy="auto",
        rules=[{"when": "value > 0", "action": "block"}],
    )

    rows = [
        {"id": "r1", "value": 1},
        {"id": "r2", "value": 2},
        {"id": "r3", "value": 3},
    ]
    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=rows,
        now=FROZEN_NOW,
    )

    assert result.total_rows == 3
    assert result.executions == []
    assert len(result.blocked) == 3
    assert result.batch_approval_id is None
    assert stub_run_action == []
    # No approval persisted.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals == []


async def test_empty_rows(stub_run_action) -> None:
    """Empty selected_rows → total_rows=0, all lists empty, no approval."""
    pocket_id = await _make_pocket()
    await _make_backend(pocket_id=pocket_id)
    template = _template(instinct_policy="auto")

    result = await bulk_dispatch.dispatch_bulk(
        workspace_id="w1",
        user_id="u1",
        pocket_id=pocket_id,
        template=template,
        action_name="mark_done",
        selected_rows=[],
        now=FROZEN_NOW,
    )

    assert result.total_rows == 0
    assert result.executions == []
    assert result.blocked == []
    assert result.batch_approval_id is None
    assert stub_run_action == []


async def test_non_bulk_action_raises_validation_error(stub_run_action) -> None:
    """A ``kind: single-row`` action must not route through the bulk path."""
    pocket_id = await _make_pocket(action_name="single_action")
    await _make_backend(pocket_id=pocket_id)
    template = _template(action_name="single_action", kind="single-row")

    with pytest.raises(ValidationError):
        await bulk_dispatch.dispatch_bulk(
            workspace_id="w1",
            user_id="u1",
            pocket_id=pocket_id,
            template=template,
            action_name="single_action",
            selected_rows=[{"id": "r1", "value": 1}],
            now=FROZEN_NOW,
        )


async def test_tenant_isolation_other_workspace_not_found(stub_run_action) -> None:
    """Workspace A cannot dispatch a workspace-B pocket — surfaces as NotFound."""
    pocket_id = await _make_pocket(workspace="wA", owner="uA", visibility="private")
    await _make_backend(pocket_id=pocket_id, workspace_id="wA")
    template = _template(instinct_policy="auto")

    with pytest.raises(NotFound):
        await bulk_dispatch.dispatch_bulk(
            workspace_id="wB",
            user_id="uB",
            pocket_id=pocket_id,
            template=template,
            action_name="mark_done",
            selected_rows=[{"id": "r1", "value": 1}],
            now=FROZEN_NOW,
        )


# ---------------------------------------------------------------------------
# Service wrapper — dispatch_bulk_action validates + emits the event
# ---------------------------------------------------------------------------


async def test_service_emits_bulk_action_dispatched_once(stub_run_action, recording_bus) -> None:
    """The service-level entry point fires exactly one
    BulkActionDispatched event per call, per EE rule 9."""
    pocket_id = await _make_pocket()
    await _make_backend(pocket_id=pocket_id)
    template = _template(instinct_policy="auto")

    body = {
        "pocket_id": pocket_id,
        "action_name": "mark_done",
        "rows": [
            {"id": "r1", "value": 1},
            {"id": "r2", "value": 2},
        ],
    }
    # Service wrapper threads the template — for the test we pass it
    # via the keyword argument the service exposes to internal callers.
    result = await pockets_service.dispatch_bulk_action(
        workspace_id="w1",
        user_id="u1",
        body=body,
        template=template,
        now=FROZEN_NOW,
    )

    assert result["total_rows"] == 2
    dispatched = [e for e in recording_bus.events if isinstance(e, BulkActionDispatched)]
    assert len(dispatched) == 1
    payload = dispatched[0].data
    assert payload["pocket_id"] == pocket_id
    assert payload["action_name"] == "mark_done"
    assert payload["total_rows"] == 2
    assert payload["workspace_id"] == "w1"
