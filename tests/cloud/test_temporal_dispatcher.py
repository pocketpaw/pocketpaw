# tests/cloud/test_temporal_dispatcher.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — pins the
# RFC 03 v2 temporal trigger sweep dispatcher.
#
# What this pins:
#   * No temporal triggers on the template → early return, zero state
#     mutation, no completion event emitted.
#   * First sweep with 1 row currently true → 1 rising edge, state
#     persisted as ``True`` (subsequent sweeps will diff against it).
#   * Second sweep, same row still true → 0 edges (continuing-true is
#     idempotent — no re-fire).
#   * Third sweep, row's value flipped to false → 0 edges, state
#     updated to ``False``.
#   * Fourth sweep, row's value back to true → 1 rising edge again.
#   * Edge fires action through the gate: verify ``gate_action`` was
#     called AND ``run_action`` was called.
#   * Block path: a row that trips an Instinct block rule → edge
#     does NOT fire HTTP; state still moves; count under ``blocked``.
#   * Approval path: a row that needs approval → approval row
#     persisted (via the gate wrapper); count under ``escalated``;
#     state still moves.
#   * Multi-tenant: workspace A's state is invisible to workspace B.
#   * Eval failure: a bad CEL doesn't break the sweep — the failure
#     is captured under ``errors`` and the sweep continues for the
#     other rows.
#
# Updated: 2026-06-28 (AW-7 follow-up — security) — adds the TEMPLATE-level
# deny-by-default coverage for the TEMPORAL path. AW-7's
# ``instinct_template_default_deny`` flag was threaded through the router but
# NOT this sweep path, so a scheduled MUTATING action with no matching rule
# fired ungated even with the flag ON. New cases (using the REAL gate, only
# the executor/creds/spec/flag-resolver stubbed) pin: flag ON + mutating + no
# rule ⇒ PARK (escalated, executor never called, approval row persisted); flag
# OFF ⇒ unchanged (fires); a read_only GET ⇒ never gated; and that the flag +
# computed ``is_mutating`` actually REACH both gate points (the direct
# ``gate_action`` and the ``run_action`` re-gate).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from pocketpaw.bundled_templates import PocketTemplate

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Template fixtures — minimal valid v2 PocketTemplate dicts.
# ---------------------------------------------------------------------------


def _template_with_temporal(
    *,
    action_name: str = "alert_overdue",
    when: str = "value > 0",
    instinct_policy: str = "auto",
    rules: list[dict] | None = None,
) -> PocketTemplate:
    raw: dict[str, Any] = {
        "schema_version": "2",
        "name": "temporal-fixture",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "test fixture",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [
                {"field": "id", "widget": "text"},
                {"field": "value", "widget": "number"},
            ],
            "id_field": "id",
        },
        "actions": [
            {
                "name": action_name,
                "label": "Alert",
                "kind": "single-row",
                "instinct_policy": instinct_policy,
                "outcomes_emitted": [],
            }
        ],
        "triggers": [
            {"type": "temporal", "when": when, "action": action_name},
        ],
        "outcomes": [],
    }
    if rules is not None:
        raw["instinct_rules"] = {"rules": rules}
    return PocketTemplate.model_validate(raw)


def _template_no_temporal() -> PocketTemplate:
    raw: dict[str, Any] = {
        "schema_version": "2",
        "name": "non-temporal-fixture",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "no temporal trigger",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [{"field": "id", "widget": "text"}],
            "id_field": "id",
        },
        "actions": [],
        "triggers": [
            {"type": "manual"},
        ],
        "outcomes": [],
    }
    return PocketTemplate.model_validate(raw)


# ---------------------------------------------------------------------------
# Test helpers — capture gate + executor calls without HTTP / Instinct.
# ---------------------------------------------------------------------------


class _RecordingGate:
    """Captures every ``gate_action`` call + returns a configurable verdict."""

    def __init__(self, next_step: str = "proceed", approval_id: str | None = None) -> None:
        self.next_step = next_step
        self.approval_id = approval_id
        self.calls: list[dict] = []

    async def gate_action(self, **kwargs: Any):  # noqa: ANN401
        self.calls.append(kwargs)

        # Minimal stand-in for ``InstinctGateResult`` — only the
        # ``next_step`` / ``approval_id`` / ``decision.reason`` fields
        # the dispatcher reads.
        class _Decision:
            reason = "test-reason"

        class _Gate:
            next_step = self.next_step
            approval_id = self.approval_id
            decision = _Decision()

        return _Gate()


class _RecordingExecutor:
    """Captures every ``run_action`` call + returns a configurable result."""

    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"ok": True, "action": "alert_overdue", "status": 200}
        self.calls: list[dict] = []

    async def run_action(self, **kwargs: Any):  # noqa: ANN401
        self.calls.append(kwargs)
        return self.result


def _patch_dispatch(monkeypatch, gate: _RecordingGate, executor: _RecordingExecutor) -> None:
    """Patch the dispatcher's lazy imports so the test never reaches the
    real gate or the real action executor."""
    from pocketpaw_ee.cloud.pockets import action_executor as real_executor
    from pocketpaw_ee.cloud.pockets import instinct_dispatch as real_gate
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    monkeypatch.setattr(real_gate, "gate_action", gate.gate_action)
    monkeypatch.setattr(real_executor, "run_action", executor.run_action)

    async def _creds(_ws: str, _pid: str):
        # ``(base_url, auth_type, auth_header, token, allowed_writes, approval_route)``
        return (
            "https://api.example.test",
            "none",
            None,
            "",
            [{"method": "POST", "path_pattern": "*"}],
            None,
        )

    async def _spec(_ws: str, _pid: str):
        return {
            "actions": {
                "alert_overdue": {
                    "kind": "write_binding",
                    "method": "POST",
                    "path": "/alerts",
                    "params": {},
                }
            }
        }

    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _creds)
    monkeypatch.setattr(pockets_service, "get_pocket_ripple_spec", _spec)


# ---------------------------------------------------------------------------
# Cases — rising-edge mechanics, gate branching, multi-tenancy, errors.
# ---------------------------------------------------------------------------


async def test_no_temporal_triggers_is_a_no_op(monkeypatch) -> None:
    """A template with no ``type: temporal`` triggers → no state writes,
    no completion event, zero tally."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher
    from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

    gate = _RecordingGate()
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_no_temporal()
    result = await temporal_dispatcher.sweep_pocket(
        "w1",
        "p1",
        template=template,
        rows=[{"id": "r1", "value": 100}],
    )

    assert result.edges_fired == 0
    assert result.blocked == 0
    assert result.escalated == 0
    assert result.errors == 0
    assert gate.calls == []
    assert executor.calls == []

    # No state persisted.
    persisted = await sweeps_service.load_last_seen("w1", "p1")
    assert persisted == {}


async def test_first_sweep_currently_true_fires_rising_edge(monkeypatch, recording_bus) -> None:
    """A row currently true → 1 rising edge, state persisted as True,
    gate + executor called once."""
    from pocketpaw_ee.cloud._core.realtime.events import TemporalSweepCompleted
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher
    from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()
    result = await temporal_dispatcher.sweep_pocket(
        "w1",
        "p1",
        template=template,
        rows=[{"id": "r1", "value": 42}],
    )

    assert result.edges_fired == 1
    assert result.blocked == 0
    assert result.escalated == 0
    assert result.errors == 0
    assert len(gate.calls) == 1
    assert len(executor.calls) == 1
    assert gate.calls[0]["action_name"] == "alert_overdue"
    assert gate.calls[0]["row_id"] == "r1"
    assert executor.calls[0]["action"] == "alert_overdue"
    # Synthetic actor for the sweeper — not a real user.
    assert gate.calls[0]["user_id"] == "system:temporal-sweeper"

    # State persisted.
    persisted = await sweeps_service.load_last_seen("w1", "p1")
    assert persisted == {("alert_overdue", "r1"): True}

    # Completion event emitted with the tally.
    completion = [e for e in recording_bus.events if isinstance(e, TemporalSweepCompleted)]
    assert len(completion) == 1
    assert completion[0].data["pocket_id"] == "p1"
    assert completion[0].data["workspace_id"] == "w1"
    assert completion[0].data["edges_fired"] == 1


async def test_second_sweep_same_true_does_not_re_fire(monkeypatch) -> None:
    """Continuing-true is idempotent — second sweep produces 0 edges."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()

    # First sweep — fires.
    await temporal_dispatcher.sweep_pocket(
        "w1",
        "p1",
        template=template,
        rows=[{"id": "r1", "value": 42}],
    )
    assert len(executor.calls) == 1

    # Second sweep, same row still true — must NOT re-fire.
    result = await temporal_dispatcher.sweep_pocket(
        "w1",
        "p1",
        template=template,
        rows=[{"id": "r1", "value": 42}],
    )
    assert result.edges_fired == 0
    assert len(executor.calls) == 1  # unchanged
    assert len(gate.calls) == 1  # gate never re-evaluated for a non-edge


async def test_falling_edge_updates_state_to_false(monkeypatch) -> None:
    """True → False on a row updates state but does NOT fire."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher
    from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()

    # First — fires.
    await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )
    # Second — row falls to value=0 (predicate false).
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 0}]
    )
    assert result.edges_fired == 0

    persisted = await sweeps_service.load_last_seen("w1", "p1")
    assert persisted == {("alert_overdue", "r1"): False}


async def test_re_rising_edge_after_falling_fires_again(monkeypatch) -> None:
    """false → true after a falling edge fires a fresh rising edge."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()

    await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )
    # Fall to false.
    await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 0}]
    )
    # Re-rise.
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 99}]
    )
    assert result.edges_fired == 1
    # Two total dispatches across the test.
    assert len(executor.calls) == 2


async def test_blocked_gate_short_circuits_dispatch(monkeypatch) -> None:
    """Gate returns BLOCK → action_executor never called; tally goes to ``blocked``."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="blocked")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )

    assert result.edges_fired == 0
    assert result.blocked == 1
    assert result.escalated == 0
    assert len(gate.calls) == 1
    assert executor.calls == []  # no HTTP call


async def test_escalated_gate_records_count_no_executor(monkeypatch) -> None:
    """Gate returns ESCALATE_APPROVAL → action_executor never called; tally goes
    to ``escalated``."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="pending_approval", approval_id="approval-1")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )

    assert result.edges_fired == 0
    assert result.blocked == 0
    assert result.escalated == 1
    assert len(gate.calls) == 1
    assert executor.calls == []


async def test_multi_tenant_state_is_isolated(monkeypatch) -> None:
    """Workspace A's state is invisible to workspace B — the same pocket
    id in two workspaces sweeps independently."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher
    from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()

    # Workspace A swept first.
    await temporal_dispatcher.sweep_pocket(
        "wA", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )
    # Workspace B sweeps SAME pocket id, SAME row id — must see empty
    # prior state (rising edge fires fresh).
    result = await temporal_dispatcher.sweep_pocket(
        "wB", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )
    assert result.edges_fired == 1

    # Verify isolation: load each workspace's state separately.
    state_a = await sweeps_service.load_last_seen("wA", "p1")
    state_b = await sweeps_service.load_last_seen("wB", "p1")
    assert state_a == {("alert_overdue", "r1"): True}
    assert state_b == {("alert_overdue", "r1"): True}
    # Two separate rows in the matrix — each workspace owns its own.


async def test_bad_cel_isolates_to_error_tally(monkeypatch) -> None:
    """A row whose CEL eval crashes is captured in ``errors`` and does
    NOT crash the sweep — other rows still process."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    # ``when`` references a column the row doesn't carry — CEL eval
    # fails on the bad row, succeeds on the good one.
    template = _template_with_temporal(when="ghost_field > 0")

    result = await temporal_dispatcher.sweep_pocket(
        "w1",
        "p1",
        template=template,
        rows=[{"id": "r1", "value": 1}, {"id": "r2", "value": 2}],
    )

    # Both rows failed (the predicate references a missing field), so
    # ``errors`` is 2 and ``edges_fired`` is 0. The important assertion
    # is the sweep COMPLETED without raising.
    assert result.errors == 2
    assert result.edges_fired == 0


async def test_no_action_trigger_counts_as_fired_no_executor(monkeypatch) -> None:
    """A temporal trigger without an ``action`` is a pure fact-trigger —
    the state still moves but no executor call happens. Counts under
    ``fired`` so the rising edge is observable."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    raw = {
        "schema_version": "2",
        "name": "fact-trigger",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "no action",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [
                {"field": "id", "widget": "text"},
                {"field": "value", "widget": "number"},
            ],
            "id_field": "id",
        },
        "actions": [],
        "triggers": [{"type": "temporal", "when": "value > 0"}],
        "outcomes": [],
    }
    template = PocketTemplate.model_validate(raw)

    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 1}]
    )
    assert result.edges_fired == 1
    assert gate.calls == []  # no gate eval — no action declared
    assert executor.calls == []


async def test_sweep_duration_ms_is_recorded(monkeypatch) -> None:
    """The ``sweep_duration_ms`` field is non-negative and rides on the
    completion event."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    template = _template_with_temporal()
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 1}]
    )
    assert result.sweep_duration_ms >= 0


async def test_explicit_now_is_honored(monkeypatch) -> None:
    """Passing a fixed ``now`` keeps the OSS sweeper deterministic — the
    test doesn't need to be tied to wall-clock."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    fixed = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)
    template = _template_with_temporal()
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 1}], now=fixed
    )
    assert result.edges_fired == 1


async def test_template_none_is_no_op(monkeypatch) -> None:
    """Scheduler path: when no template resolves, the dispatcher does
    nothing — no state writes, no event, no gate / executor calls."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher
    from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

    gate = _RecordingGate()
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    result = await temporal_dispatcher.sweep_pocket(
        "w1",
        "p1",
        template=None,
        rows=[{"id": "r1", "value": 1}],
    )

    assert result.edges_fired == 0
    assert gate.calls == []
    assert executor.calls == []
    persisted = await sweeps_service.load_last_seen("w1", "p1")
    assert persisted == {}


# ---------------------------------------------------------------------------
# AW-7 follow-up (security) — TEMPLATE-level deny-by-default on the TEMPORAL
# sweep path. AW-7 added the per-workspace ``instinct_template_default_deny``
# flag: ON ⇒ a template-bound pocket with NO matching rule for a MUTATING
# action PARKS it instead of firing. The router threaded it; the temporal
# sweep path did NOT, so a scheduled mutating action with no matching rule
# fired ungated even with the flag ON — a hole in the exact feature meant to
# close it. These tests pin that the temporal path now mirrors the router:
# flag ON + mutating + no rule ⇒ PARK; flag OFF ⇒ unchanged; a READ is never
# gated. They use the REAL ``gate_action`` (only the executor + creds + spec +
# the flag resolver are stubbed) so the actual escalation logic fires
# end-to-end — a stubbed gate would not prove the hole is closed.
# ---------------------------------------------------------------------------


def _patch_real_gate(
    monkeypatch,
    executor: _RecordingExecutor,
    *,
    default_deny: bool,
    method: str = "POST",
    read_only: bool = False,
) -> None:
    """Wire the dispatcher to the REAL gate but stub everything around it.

    The executor is recorded (so we can assert it is/ isn't called), the creds
    + spec are faked (no HTTP, no real pocket), and
    ``resolve_workspace_template_default_deny`` returns ``default_deny`` so the
    test drives the flag without touching a Workspace document. ``method`` /
    ``read_only`` shape the single ``alert_overdue`` binding so the dispatcher's
    ``is_mutating`` computation has real input.
    """
    from pocketpaw_ee.cloud.pockets import action_executor as real_executor
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    monkeypatch.setattr(real_executor, "run_action", executor.run_action)

    async def _creds(_ws: str, _pid: str):
        return (
            "https://api.example.test",
            "none",
            None,
            "",
            [{"method": method, "path_pattern": "*"}],
            None,
        )

    binding: dict[str, Any] = {
        "kind": "write_binding",
        "method": method,
        "path": "/alerts",
        "params": {},
    }
    if read_only:
        binding["read_only"] = True

    async def _spec(_ws: str, _pid: str):
        return {"actions": {"alert_overdue": binding}}

    async def _deny(_ws: str) -> bool:
        return default_deny

    monkeypatch.setattr(pockets_service, "get_pocket_backend_for_executor", _creds)
    monkeypatch.setattr(pockets_service, "get_pocket_ripple_spec", _spec)
    monkeypatch.setattr(pockets_service, "resolve_workspace_template_default_deny", _deny)


async def test_temporal_default_deny_parks_uncovered_mutating_action(
    monkeypatch, recording_bus
) -> None:
    """THE HOLE, CLOSED. Flag ON + a temporal-triggered MUTATING action +
    a bound template with NO matching rule ⇒ the action PARKS (escalated /
    pending approval), it is NOT executed. Mirrors the router-path test
    ``test_template_default_deny_parks_uncovered_mutating_action``."""
    from pocketpaw_ee.cloud._core.realtime.events import InstinctApprovalCreated
    from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    executor = _RecordingExecutor()
    # POST (mutating), flag ON. `auto` policy + no rules ⇒ resolve_instinct
    # returns its default EXECUTE/reason="auto" — the no-rule-match case.
    _patch_real_gate(monkeypatch, executor, default_deny=True, method="POST")

    template = _template_with_temporal(instinct_policy="auto")  # no instinct_rules
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )

    # The mutating action PARKED — counted under escalated, NOT fired, and the
    # executor was NEVER called (no ungated write).
    assert result.escalated == 1
    assert result.edges_fired == 0
    assert result.blocked == 0
    assert executor.calls == []

    # A real human-pending approval row was persisted by the gate.
    approvals = await approvals_service.list_approvals("w1", "system:temporal-sweeper", {})
    assert len(approvals) == 1
    assert approvals[0]["action_name"] == "alert_overdue"
    assert approvals[0]["status"] == "pending"
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1


async def test_temporal_default_deny_off_proceeds_unchanged(monkeypatch) -> None:
    """Flag OFF (the default) ⇒ the temporal mutating action fires exactly as
    before — the executor runs, the edge counts under ``fired``. Byte-for-byte
    unchanged behavior on the dormant path."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    executor = _RecordingExecutor()
    _patch_real_gate(monkeypatch, executor, default_deny=False, method="POST")

    template = _template_with_temporal(instinct_policy="auto")
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )

    assert result.edges_fired == 1
    assert result.escalated == 0
    assert len(executor.calls) == 1
    # The executor's re-gate also saw the flag OFF, so it did not park either.
    assert executor.calls[0]["template_default_deny"] is False


async def test_temporal_default_deny_does_not_gate_read_only(monkeypatch) -> None:
    """Flag ON but the binding is a READ-ONLY GET ⇒ ``is_mutating`` is False, so
    the deny-by-default flip never parks it — a read has nothing to govern. The
    edge proceeds to the executor (counted under ``fired``)."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    executor = _RecordingExecutor()
    _patch_real_gate(monkeypatch, executor, default_deny=True, method="GET", read_only=True)

    template = _template_with_temporal(instinct_policy="auto")
    result = await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )

    # A read is never gated by the template default-deny flip.
    assert result.edges_fired == 1
    assert result.escalated == 0
    assert len(executor.calls) == 1


async def test_temporal_threads_flag_and_is_mutating_into_gate(monkeypatch) -> None:
    """Defense against the security review's 'double-gate' note: the flag AND
    the computed ``is_mutating`` must actually REACH the direct ``gate_action``
    call, and the flag must reach the ``run_action`` re-gate. A recording gate
    captures the kwargs so we prove the values arrive (not just that the call
    happens)."""
    from pocketpaw_ee.cloud.pockets import temporal_dispatcher

    gate = _RecordingGate(next_step="proceed")
    executor = _RecordingExecutor()
    _patch_dispatch(monkeypatch, gate, executor)

    # Drive the flag ON via the resolver; the recorded gate proceeds regardless.
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _deny(_ws: str) -> bool:
        return True

    monkeypatch.setattr(pockets_service, "resolve_workspace_template_default_deny", _deny)

    template = _template_with_temporal(instinct_policy="auto")
    await temporal_dispatcher.sweep_pocket(
        "w1", "p1", template=template, rows=[{"id": "r1", "value": 42}]
    )

    # FIRST gate point — the direct gate_action call got BOTH values.
    assert len(gate.calls) == 1
    assert gate.calls[0]["template_default_deny"] is True
    # POST binding from `_patch_dispatch`'s spec ⇒ mutating.
    assert gate.calls[0]["is_mutating"] is True

    # SECOND gate point — run_action got the flag so its re-gate honors it too.
    assert len(executor.calls) == 1
    assert executor.calls[0]["template_default_deny"] is True
