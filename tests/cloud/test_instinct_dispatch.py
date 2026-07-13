# tests/cloud/test_instinct_dispatch.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — pins the
# RFC 03 v2 template-level Instinct gate as wired into the EE
# action_executor.
#
# What this pins:
#   * BLOCK path: a row tripping a block rule → no approval row, no
#     HTTP call, the executor returns the `instinct_blocked` sentinel.
#   * ESCALATE_APPROVAL path: a row tripping an approval rule → one
#     `InstinctApproval` row persisted under the right workspace,
#     the executor returns `instinct_pending` carrying `approval_id`,
#     no HTTP call.
#   * EXECUTE path: auto policy + no rules → gate returns `proceed`;
#     when called without HTTP wiring the executor still walks the
#     security gates and ultimately makes a request — verified by
#     observing a populated `instinct-pending`-FREE result.
#   * NOTIFY_AND_EXECUTE path: notify_only policy → gate returns
#     `proceed` with `notify_rules` populated.
#   * Unknown action surfaces as a `CloudError` (`NotFound`).
#   * Workspace isolation: an approval row in workspace A is invisible
#     to a workspace-B read.
#   * Audit emit: every state-mutating service function emits its event.
#
# The executor's HTTP path is exercised indirectly: a BLOCK / pending
# result must short-circuit BEFORE the HTTP request, so the test does
# not need a fake transport — if the gate ever fell through into the
# HTTP stack on a BLOCK row the test would explode on socket lookup
# (the bogus base_url has no resolver).

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    InstinctApprovalApproved,
    InstinctApprovalCreated,
    InstinctApprovalRejected,
)
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
from pocketpaw_ee.cloud.pockets import action_executor, instinct_dispatch

from pocketpaw.bundled_templates import PocketTemplate

pytestmark = pytest.mark.usefixtures("mongo_db")

FROZEN_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture helpers — minimal valid v2 PocketTemplate dicts. Every test
# builds its own so the rule shape is visible inline. ``shape=data-grid``
# needs at least one column; we declare a `value` column so the row's
# `value` identifier resolves through the default ``TemplateIdentifierResolver``.
# ---------------------------------------------------------------------------


def _template(
    *,
    instinct_policy: str = "auto",
    rules: list[dict] | None = None,
    action_name: str = "do_thing",
) -> PocketTemplate:
    raw: dict = {
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
        },
        "actions": [
            {
                "name": action_name,
                "label": "Do Thing",
                "kind": "single-row",
                "instinct_policy": instinct_policy,
            }
        ],
    }
    if rules is not None:
        raw["instinct_rules"] = {"rules": rules}
    return PocketTemplate.model_validate(raw)


def _raw_action() -> dict:
    """A minimal `ActionBinding`-parseable raw action — used so the
    executor's binding-parse step passes. We never let the executor
    reach an HTTP call in this test file; the BLOCK / pending paths
    short-circuit beforehand."""
    return {"kind": "write_binding", "method": "POST", "path": "/items"}


# ---------------------------------------------------------------------------
# gate_action — direct tests against the wrapper (no executor)
# ---------------------------------------------------------------------------


async def test_gate_block_persists_nothing(recording_bus) -> None:
    template = _template(
        instinct_policy="auto",
        rules=[{"when": "value > 100", "action": "block"}],
    )

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 999},
        now=FROZEN_NOW,
    )

    assert result.next_step == "blocked"
    assert result.approval_id is None
    assert result.decision.verdict == "BLOCK"
    # No InstinctApprovalCreated event for a block.
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert created == []


async def test_gate_escalate_persists_approval_and_emits(recording_bus) -> None:
    template = _template(
        instinct_policy="auto",
        rules=[{"when": "value > 100", "action": "require_approval"}],
    )

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 200},
        row_id="row-42",
        park={"action": "do_thing", "method": "POST", "path": "/items", "params": {"x": 1}},
        now=FROZEN_NOW,
    )

    assert result.next_step == "pending_approval"
    assert result.approval_id is not None
    assert result.decision.verdict == "ESCALATE_APPROVAL"

    # Approval row persisted under workspace=w1.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["id"] == result.approval_id
    assert approvals[0]["workspace_id"] == "w1"
    assert approvals[0]["pocket_id"] == "p1"
    assert approvals[0]["action_name"] == "do_thing"
    assert approvals[0]["row_id"] == "row-42"
    assert approvals[0]["row_data"] == {"value": 200}
    assert approvals[0]["status"] == "pending"

    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1
    assert created[0].data["id"] == result.approval_id
    assert created[0].data["workspace_id"] == "w1"


async def test_gate_execute_proceeds() -> None:
    template = _template(instinct_policy="auto")

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
    )

    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"
    assert result.notify_rules == []


async def test_gate_notify_only_proceeds_with_notify_rules() -> None:
    template = _template(
        instinct_policy="notify_only",
        rules=[{"when": "value > 100", "action": "notify"}],
    )

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 999},
        now=FROZEN_NOW,
    )

    assert result.next_step == "proceed"
    assert result.decision.verdict == "NOTIFY_AND_EXECUTE"
    assert len(result.notify_rules) == 1
    assert result.notify_rules[0].when == "value > 100"


async def test_gate_unknown_action_raises_not_found() -> None:
    template = _template(action_name="known_action")

    from pocketpaw_ee.cloud._core.errors import NotFound

    with pytest.raises(NotFound) as excinfo:
        await instinct_dispatch.gate_action(
            workspace_id="w1",
            user_id="u1",
            pocket_id="p1",
            template=template,
            action_name="unknown_action",
            row_context={"value": 1},
            now=FROZEN_NOW,
        )
    assert "unknown_action" in str(excinfo.value)


async def test_gate_workspace_isolation_on_list() -> None:
    template = _template(
        rules=[{"when": "value > 0", "action": "require_approval"}],
    )

    # Persist in workspace A
    await instinct_dispatch.gate_action(
        workspace_id="wA",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
    )

    # Workspace B sees no rows
    in_b = await approvals_service.list_approvals("wB", "u_other", {})
    assert in_b == []
    in_a = await approvals_service.list_approvals("wA", "u1", {})
    assert len(in_a) == 1


# ---------------------------------------------------------------------------
# action_executor wiring — gate_action is called BEFORE the HTTP path
# ---------------------------------------------------------------------------


async def test_executor_blocked_short_circuits_before_http() -> None:
    template = _template(
        rules=[{"when": "value > 100", "action": "block"}],
    )

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action=_raw_action(),
        path="/items",
        params={},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 999},
    )

    assert result["ok"] is False
    assert result["code"] == "instinct_blocked"
    assert result["action"] == "do_thing"


async def test_executor_pending_approval_short_circuits_before_http(recording_bus) -> None:
    template = _template(
        rules=[{"when": "value > 100", "action": "require_approval"}],
    )

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 200},
        row_id="r1",
    )

    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    assert result["approval_id"]
    # The park blob carries the resolved write so a future post-approval
    # re-entry can replay it.
    assert result["_park"]["method"] == "POST"
    assert result["_park"]["path"] == "/items"

    # Persisted under w1 with the right shape.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["id"] == result["approval_id"]

    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1


async def test_executor_from_instinct_bypasses_gate(recording_bus) -> None:
    """A post-approval re-entry (`from_instinct=True`) must NOT call
    the gate again — otherwise an approval would create a second
    approval row in a loop. This pins the bypass."""
    template = _template(
        rules=[{"when": "value > 100", "action": "require_approval"}],
    )

    # We expect this to fall through into the HTTP-path gates; the
    # bogus base_url will reject (rate-limit or DNS), but crucially NO
    # `instinct_pending` and NO new approval row.
    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 200},
        from_instinct=True,
    )

    assert result.get("code") != "instinct_pending"
    # No approval persisted.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals == []
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert created == []


async def test_executor_without_template_is_backward_compatible() -> None:
    """When no template is threaded through (every existing caller),
    the gate is skipped. The executor falls into its existing gate
    stack — the bogus base_url here triggers an SSRF/DNS reject."""
    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action=_raw_action(),
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
    )
    assert result.get("code") != "instinct_pending"
    assert result.get("code") != "instinct_blocked"


# ---------------------------------------------------------------------------
# Approval lifecycle — approve / reject emit + state transitions
# ---------------------------------------------------------------------------


async def test_approve_flips_status_and_emits(recording_bus) -> None:
    template = _template(
        rules=[{"when": "value > 0", "action": "require_approval"}],
    )
    gate = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
    )
    recording_bus.events.clear()

    out = await approvals_service.approve("w1", "u_approver", gate.approval_id, {"note": "ok"})
    assert out["status"] == "approved"
    assert out["decided_by"] == "u_approver"
    approved = [e for e in recording_bus.events if isinstance(e, InstinctApprovalApproved)]
    assert len(approved) == 1
    assert approved[0].data["id"] == gate.approval_id


async def test_reject_flips_status_and_emits(recording_bus) -> None:
    template = _template(
        rules=[{"when": "value > 0", "action": "require_approval"}],
    )
    gate = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
    )
    recording_bus.events.clear()

    out = await approvals_service.reject("w1", "u_approver", gate.approval_id, {"note": "nope"})
    assert out["status"] == "rejected"
    rejected = [e for e in recording_bus.events if isinstance(e, InstinctApprovalRejected)]
    assert len(rejected) == 1


async def test_double_decide_conflicts() -> None:
    from pocketpaw_ee.cloud._core.errors import ConflictError

    template = _template(
        rules=[{"when": "value > 0", "action": "require_approval"}],
    )
    gate = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
    )
    await approvals_service.approve("w1", "u_approver", gate.approval_id, None)

    with pytest.raises(ConflictError):
        await approvals_service.reject("w1", "u_approver", gate.approval_id, None)


async def test_decide_cross_workspace_not_found() -> None:
    from pocketpaw_ee.cloud._core.errors import NotFound

    template = _template(
        rules=[{"when": "value > 0", "action": "require_approval"}],
    )
    gate = await instinct_dispatch.gate_action(
        workspace_id="wA",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
    )

    with pytest.raises(NotFound):
        await approvals_service.approve("wB", "u_approver", gate.approval_id, None)


# ---------------------------------------------------------------------------
# AW-7 — TEMPLATE-level deny-on-no-match. A bound template that declares no
# rule matching a MUTATING action parks the write for a human when the
# per-workspace / global flag is ON. Reads stay ungated; flag OFF is unchanged;
# a per-workspace override beats the global default.
# ---------------------------------------------------------------------------


async def test_template_default_deny_parks_uncovered_mutating_action(recording_bus) -> None:
    """Flag ON + a bound template with NO rule for a MUTATING action ⇒ park
    (PENDING_APPROVAL), not proceed. This is the deny-by-default flip."""
    # `auto` policy + no rules → resolve_instinct returns its default EXECUTE
    # (reason="auto") — the no-rule-match case AW-7 must park.
    template = _template(instinct_policy="auto")

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        row_id="row-1",
        park={"action": "do_thing", "method": "POST", "path": "/items", "params": {}},
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=True,
    )

    assert result.next_step == "pending_approval"
    assert result.approval_id is not None
    # The verdict is still EXECUTE/auto — we did not invent a rule, we parked a
    # write the template did not govern.
    assert result.decision.verdict == "EXECUTE"
    assert result.decision.reason == "auto"

    # One human-pending approval row was persisted.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["id"] == result.approval_id
    assert approvals[0]["status"] == "pending"
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1


async def test_template_default_deny_does_not_park_read(recording_bus) -> None:
    """Flag ON but a READ (is_mutating=False) ⇒ proceed ungated. A read has
    nothing to govern, so the deny-on-no-match flip must never gate it."""
    template = _template(instinct_policy="auto")

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=False,  # read_only / GET / HEAD
    )

    assert result.next_step == "proceed"
    assert result.approval_id is None
    # No approval row persisted for a read.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals == []
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert created == []


async def test_template_default_deny_off_is_unchanged() -> None:
    """Flag OFF (the default) ⇒ the no-rule-match EXECUTE still proceeds —
    zero day-one behavior change."""
    template = _template(instinct_policy="auto")

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        now=FROZEN_NOW,
        template_default_deny=False,
        is_mutating=True,
    )

    assert result.next_step == "proceed"
    assert result.approval_id is None


async def test_template_default_deny_does_not_override_matched_execute_rule(recording_bus) -> None:
    """A rule that MATCHED and returned EXECUTE is an explicit policy decision —
    deny-on-no-match must not override it. Only the no-rule-match `auto`
    default parks. A notify rule that matches gives reason != "auto", so the
    write still proceeds even with the flag ON."""
    template = _template(
        instinct_policy="notify_only",
        rules=[{"when": "value > 0", "action": "notify"}],
    )

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 5},
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=True,
    )

    # notify_only author floor → NOTIFY_AND_EXECUTE (reason "notify_only"), NOT
    # the no-rule-match "auto" default — so it proceeds, unparked.
    assert result.next_step == "proceed"
    assert result.decision.verdict == "NOTIFY_AND_EXECUTE"
    assert result.approval_id is None
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert created == []


async def test_template_default_deny_block_still_blocks() -> None:
    """A BLOCK rule short-circuits before the deny-on-no-match flip — a blocked
    write is still blocked, never silently turned into a park."""
    template = _template(
        instinct_policy="auto",
        rules=[{"when": "value > 100", "action": "block"}],
    )

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 999},
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=True,
    )

    assert result.next_step == "blocked"
    assert result.approval_id is None


# --- executor-level integration (the gate is wired through run_action) ------


async def test_executor_template_default_deny_parks_mutating_write(recording_bus) -> None:
    """Flag ON threaded through `run_action` + a bound template with no rule
    for a MUTATING (POST) action ⇒ `instinct_pending`, no HTTP call. The
    executor computes is_mutating=True from the parsed binding."""
    template = _template(instinct_policy="auto")

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action={"kind": "write_binding", "method": "POST", "path": "/items"},
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 1},
        row_id="r1",
        template_default_deny=True,
    )

    assert result["ok"] is True
    assert result["code"] == "instinct_pending"
    assert result["approval_id"]
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1


async def test_executor_template_default_deny_proceeds_for_read_only(recording_bus) -> None:
    """Flag ON + a read_only GET binding ⇒ the executor computes
    is_mutating=False, so the gate does NOT park; the write falls through to
    the HTTP stack (the bogus base_url rejects later). No approval row, no
    `instinct_pending`."""
    template = _template(instinct_policy="auto")

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action={
            "kind": "write_binding",
            "method": "GET",
            "path": "/items",
            "read_only": True,
        },
        path="/items",
        params={},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "GET", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 1},
        row_id="r1",
        template_default_deny=True,
    )

    # NOT parked — a read is never gated by the template default-deny flip.
    assert result.get("code") != "instinct_pending"
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals == []
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert created == []


async def test_executor_template_default_deny_off_proceeds(recording_bus) -> None:
    """Flag OFF (default) threaded through `run_action` ⇒ the no-rule-match
    mutating write is NOT parked — it falls into the existing HTTP gate stack
    (bogus base_url rejects). Zero behavior change."""
    template = _template(instinct_policy="auto")

    result = await action_executor.run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u1",
        action="do_thing",
        raw_action={"kind": "write_binding", "method": "POST", "path": "/items"},
        path="/items",
        params={"x": 1},
        base_url="https://example.test",
        auth_type="bearer",
        auth_header=None,
        token="t",
        allowed_writes=[{"method": "POST", "path_pattern": "/items*"}],
        template=template,
        row_context={"value": 1},
        row_id="r1",
        # template_default_deny defaults False
    )

    assert result.get("code") != "instinct_pending"
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals == []


# --- per-workspace override beats the global default ------------------------


async def test_workspace_override_beats_global_template_default_deny(monkeypatch) -> None:
    """The per-workspace `instinct_template_default_deny` field overrides the
    global config default (resolve order mirrors approval_level)."""
    from pocketpaw_ee.cloud.models.workspace import Workspace
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    # Global default OFF, workspace field ON → resolver returns True.
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: type("S", (), {"instinct_template_default_deny": False})(),
    )
    ws = Workspace(name="ws-on", slug="ws-on", owner="u1", instinct_template_default_deny=True)
    await ws.insert()
    resolved = await pockets_service.resolve_workspace_template_default_deny(str(ws.id))
    assert resolved is True

    # Global default ON, workspace field OFF → workspace wins (returns False).
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: type("S", (), {"instinct_template_default_deny": True})(),
    )
    ws_off = Workspace(
        name="ws-off", slug="ws-off", owner="u1", instinct_template_default_deny=False
    )
    await ws_off.insert()
    resolved_off = await pockets_service.resolve_workspace_template_default_deny(str(ws_off.id))
    assert resolved_off is False

    # Workspace field unset (None) → falls back to the global default (True).
    ws_none = Workspace(name="ws-none", slug="ws-none", owner="u1")
    await ws_none.insert()
    resolved_none = await pockets_service.resolve_workspace_template_default_deny(str(ws_none.id))
    assert resolved_none is True
