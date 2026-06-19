# tests/cloud/test_instinct_dispatch_lanes.py
# Created: 2026-06-19 (feat/instinct-gate-integration, T6/T11) — integration
# coverage for the layered/learning gate wired LIVE into
# ``instinct_dispatch.gate_action``. The foundation (T1-T5) shipped the pure
# classifier, the trust ledger, the auto-approve writer and the config
# defaults; this file pins the gate ROUTING that calls ``classify_lane`` and
# branches per lane (AUTO → decided row, OPTIMISTIC → optimistic_proceed,
# DRY_RUN → dry_run, ESCALATE → the unchanged human-pending path, BLOCK →
# blocked).
#
# THE SAFETY INVARIANT (T-18 + test_default_ask_is_zero_behavior_change):
# a ``gate_action`` call that passes NO ``approval_level`` arg behaves EXACTLY
# as it did before this PR — every escalate goes to a human-pending row. The
# triager only ever activates when a workspace explicitly opts into a non-ASK
# level. These tests are the proof.
#
# Test plan cases pinned here: T-18, T-20, T-21, T-22, T-23, T-24, T-25, T-26.
# (T-19 dry_run-at-gate routing lives in test_dry_run_lane.py.)

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    InstinctApprovalAutoApproved,
    InstinctApprovalCreated,
)
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
from pocketpaw_ee.cloud.pockets import instinct_dispatch, trust_ledger
from pocketpaw_ee.cloud.pockets.instinct_triage import ApprovalLevel, TriageLane

from pocketpaw.bundled_templates import PocketTemplate

pytestmark = pytest.mark.usefixtures("mongo_db")

FROZEN_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Template fixture — a data-grid template with a single configurable action.
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


def _escalate_template() -> PocketTemplate:
    """A template whose rule escalates the row to human approval."""
    return _template(rules=[{"when": "value > 0", "action": "require_approval"}])


@pytest.fixture(autouse=True)
def _isolated_trust(monkeypatch, tmp_path):
    """Point the trust ledger at a temp dir so the per-test sidecar is clean."""

    def _dir() -> object:
        return tmp_path / "trust"

    monkeypatch.setattr(trust_ledger, "_trust_dir", _dir)


async def _seed_trust(workspace_id: str, pocket_id: str, action: str, *, n_auto: int, n_human: int):
    """Append n_auto auto-approved + n_human human rows so the (pocket, action)
    pair reaches a known score / proposed_count."""
    for _ in range(n_auto):
        await trust_ledger.record_correction(
            workspace_id, pocket_id, action, was_auto_approved=True
        )
    for _ in range(n_human):
        await trust_ledger.record_correction(
            workspace_id, pocket_id, action, was_auto_approved=False
        )


# ===========================================================================
# THE SAFETY INVARIANT — default approval_level=ASK is zero behavior change.
# ===========================================================================


async def test_default_ask_is_zero_behavior_change(recording_bus) -> None:
    """T-18 / safety invariant: a gate_action call with NO approval_level arg
    produces the SAME result as before this PR — a human-pending row, never an
    auto-approval. Even with high trust seeded, the dormant triager escalates.
    """
    # Seed maximum trust for the pair — if the triager were active this would
    # AUTO-approve. Under default ASK it must NOT.
    await _seed_trust("w1", "p1", "do_thing", n_auto=10, n_human=0)

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_escalate_template(),
        action_name="do_thing",
        row_context={"value": 1},
        row_id="r1",
        park={"action": "do_thing", "method": "POST", "path": "/items", "params": {}},
        now=FROZEN_NOW,
        # NO approval_level passed — default ASK.
    )

    assert result.next_step == "pending_approval"
    assert result.lane == TriageLane.ESCALATE
    assert result.approval_id is not None

    # The persisted row is PENDING (human queue), not auto_approved.
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["status"] == "pending"

    # The created event is the human-pending one; NO auto-approved event.
    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    auto = [e for e in recording_bus.events if isinstance(e, InstinctApprovalAutoApproved)]
    assert len(created) == 1
    assert auto == []


async def test_default_ask_high_trust_delete_still_pending(recording_bus) -> None:
    """Belt-and-braces: even a DELETE with high trust under default ASK stays
    pending. ASK short-circuits before any blast-radius reasoning."""
    await _seed_trust("w1", "p1", "do_thing", n_auto=10, n_human=0)

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_escalate_template(),
        action_name="do_thing",
        row_context={"value": 1},
        park={"action": "do_thing", "method": "DELETE", "path": "/items/1", "params": {}},
        now=FROZEN_NOW,
    )
    assert result.next_step == "pending_approval"
    assert result.lane == TriageLane.ESCALATE


# ===========================================================================
# T-18 — ESCALATE verdict + ASK level → unchanged pending path.
# ===========================================================================


async def test_escalate_ask_level_is_pending(recording_bus) -> None:
    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_escalate_template(),
        action_name="do_thing",
        row_context={"value": 1},
        approval_level=ApprovalLevel.ASK,
        park={"action": "do_thing", "method": "POST", "path": "/items", "params": {}},
        now=FROZEN_NOW,
    )
    assert result.next_step == "pending_approval"
    assert result.lane == TriageLane.ESCALATE
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals[0]["status"] == "pending"


# ===========================================================================
# T-20 — AUTO lane writes a decided row, no pending.
# ===========================================================================


async def test_triage_auto_lane_writes_decided_row(recording_bus) -> None:
    """ESCALATE verdict + TRIAGE + trust≥threshold + proposed_count≥1 + POST +
    CompensateSpec → AUTO. A decided (auto_approved) row, no pending row, the
    auto-approved event fires."""
    await _seed_trust("w1", "p1", "do_thing", n_auto=10, n_human=0)  # score 1.0, count 10

    park = {
        "action": "do_thing",
        "method": "POST",
        "path": "/items",
        "params": {"x": 1},
        "compensate": {"method": "DELETE", "path": "/items/1"},
    }
    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_escalate_template(),
        action_name="do_thing",
        row_context={"value": 1},
        row_id="r1",
        approval_level=ApprovalLevel.TRIAGE,
        park=park,
        now=FROZEN_NOW,
    )

    assert result.lane == TriageLane.AUTO
    assert result.next_step == "auto_approved"
    assert result.approval_id is not None

    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["status"] == "auto_approved"
    assert approvals[0]["decided_by"] == "system:triager"

    # No pending rows exist.
    pending = await approvals_service.list_approvals("w1", "u1", {"status": "pending"})
    assert pending == []

    auto = [e for e in recording_bus.events if isinstance(e, InstinctApprovalAutoApproved)]
    assert len(auto) == 1
    assert auto[0].data["lane"] == "AUTO"
    assert auto[0].data["actor"] == "system:triager"


# ===========================================================================
# T-21 / T-22 — EXECUTE proceeds, BLOCK blocks (even at TRUSTED).
# ===========================================================================


async def test_execute_verdict_proceeds() -> None:
    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_template(instinct_policy="auto"),
        action_name="do_thing",
        row_context={"value": 1},
        approval_level=ApprovalLevel.TRIAGE,
        now=FROZEN_NOW,
    )
    assert result.next_step == "proceed"
    assert result.decision.verdict == "EXECUTE"


async def test_block_verdict_blocks_even_at_trusted() -> None:
    """T-22: a BLOCK rule blocks regardless of approval_level — the safety
    hard gate bypasses triage entirely."""
    await _seed_trust("w1", "p1", "do_thing", n_auto=10, n_human=0)
    template = _template(rules=[{"when": "value > 100", "action": "block"}])

    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 999},
        approval_level=ApprovalLevel.TRUSTED,
        park={"action": "do_thing", "method": "POST", "path": "/items"},
        now=FROZEN_NOW,
    )
    assert result.next_step == "blocked"
    assert result.lane == TriageLane.ESCALATE  # default-safe lane on a block
    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert approvals == []


# ===========================================================================
# T-23 — OPTIMISTIC lane → optimistic_proceed + auto_approved row.
# ===========================================================================


async def test_triage_optimistic_lane(recording_bus) -> None:
    """A reversible low-blast write with trust below threshold but ≥1 prior
    execution routes OPTIMISTIC → next_step=optimistic_proceed, an
    auto_approved row tagged lane=OPTIMISTIC."""
    # score 0.5 (below 0.9 threshold), proposed_count 2 → OPTIMISTIC (rule 9)
    await _seed_trust("w1", "p1", "do_thing", n_auto=1, n_human=1)

    park = {
        "action": "do_thing",
        "method": "POST",
        "path": "/items",
        "params": {},
        "compensate": {"method": "DELETE", "path": "/items/1"},
    }
    result = await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_escalate_template(),
        action_name="do_thing",
        row_context={"value": 1},
        approval_level=ApprovalLevel.TRIAGE,
        park=park,
        now=FROZEN_NOW,
    )

    assert result.lane == TriageLane.OPTIMISTIC
    assert result.next_step == "optimistic_proceed"

    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1
    assert approvals[0]["status"] == "auto_approved"

    auto = [e for e in recording_bus.events if isinstance(e, InstinctApprovalAutoApproved)]
    assert len(auto) == 1
    assert auto[0].data["lane"] == "OPTIMISTIC"


# ===========================================================================
# T-24 — every TRIAGE classify decision is audited.
# ===========================================================================


async def test_triage_decision_is_audited(monkeypatch) -> None:
    """T-24: gate_action logs the classify_lane decision (lane, trust_score,
    proposed_count, approval_level) to the audit log at every TRIAGE call."""
    captured: list[dict] = []

    from pocketpaw.security import audit as audit_mod

    class _FakeLogger:
        def log(self, event):
            captured.append(event.to_dict() if hasattr(event, "to_dict") else event.__dict__)

    monkeypatch.setattr(audit_mod, "get_audit_logger", lambda: _FakeLogger())
    # gate_action imports get_audit_logger lazily inside its audit helper, so
    # patching the source module is enough.

    await _seed_trust("w1", "p1", "do_thing", n_auto=10, n_human=0)
    await instinct_dispatch.gate_action(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=_escalate_template(),
        action_name="do_thing",
        row_context={"value": 1},
        approval_level=ApprovalLevel.TRIAGE,
        park={
            "action": "do_thing",
            "method": "POST",
            "path": "/items",
            "compensate": {"method": "DELETE", "path": "/items/1"},
        },
        now=FROZEN_NOW,
    )

    # AuditEvent.to_dict nests the custom fields under ``context``.
    def _ctx(e: dict) -> dict:
        return e.get("context") or {}

    triage_events = [e for e in captured if str(_ctx(e).get("category", "")) == "instinct_triage"]
    assert triage_events, f"no instinct_triage audit event found in {captured}"
    blob = _ctx(triage_events[0])
    assert blob.get("lane") == "AUTO"
    assert "trust_score" in blob
    assert "proposed_count" in blob


# ===========================================================================
# T-26 — BATCH-lane dedup (T11). A second escalate for the same
# (pocket, action, row) returns the existing pending row's id.
# ===========================================================================


async def test_batch_dedup_returns_existing_pending(recording_bus) -> None:
    """T-26 / T11: two escalating gate_action calls for the same
    (pocket_id, action_name, row_id) produce ONE pending row — the second
    finds the first and returns its id."""
    template = _escalate_template()
    common = dict(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        row_id="row-dup",
        park={"action": "do_thing", "method": "POST", "path": "/items"},
        now=FROZEN_NOW,
    )

    first = await instinct_dispatch.gate_action(**common)
    second = await instinct_dispatch.gate_action(**common)

    assert first.next_step == "pending_approval"
    assert second.next_step == "pending_approval"
    assert second.approval_id == first.approval_id

    approvals = await approvals_service.list_approvals("w1", "u1", {})
    assert len(approvals) == 1, "dedup must not create a second pending row"

    created = [e for e in recording_bus.events if isinstance(e, InstinctApprovalCreated)]
    assert len(created) == 1


# ===========================================================================
# T-26b / FIX 3 — BATCH dedup must survive a pocket with MANY pending rows.
# `_find_existing_pending_id` previously listed pending rows (default limit
# 50) and matched in Python; a pocket with >50 pending rows could push the
# target row past the limit, so the second escalate for the SAME row would
# miss it and stack a duplicate. The fix pushes action_name + row_id into the
# query filter so the DB finds the row regardless of how many others exist.
# ===========================================================================


async def test_batch_dedup_survives_many_pending_rows(recording_bus) -> None:
    """FIX 3: with 60 OTHER pending rows already on the pocket (more than the
    list default limit of 50), a re-escalate of the same (pocket, action, row)
    still finds the existing pending row and returns its id — no duplicate."""
    template = _escalate_template()

    # First: the row we care about — creates ONE pending row.
    target = dict(
        workspace_id="w1",
        user_id="u1",
        pocket_id="p1",
        template=template,
        action_name="do_thing",
        row_context={"value": 1},
        row_id="row-target",
        park={"action": "do_thing", "method": "POST", "path": "/items"},
        now=FROZEN_NOW,
    )
    first = await instinct_dispatch.gate_action(**target)
    assert first.next_step == "pending_approval"

    # Now flood the SAME pocket with 60 other distinct pending rows so the
    # target row is no longer in the first page of a limit-50 list query.
    for i in range(60):
        await instinct_dispatch.gate_action(
            workspace_id="w1",
            user_id="u1",
            pocket_id="p1",
            template=template,
            action_name="do_thing",
            row_context={"value": 1},
            row_id=f"row-noise-{i}",
            park={"action": "do_thing", "method": "POST", "path": "/items"},
            now=FROZEN_NOW,
        )

    # Re-escalate the ORIGINAL row. It must dedup to the first row's id even
    # though 60 newer pending rows would otherwise bury it past the limit.
    second = await instinct_dispatch.gate_action(**target)
    assert second.next_step == "pending_approval"
    assert second.approval_id == first.approval_id, (
        "dedup missed the target row because it fell past the list limit"
    )

    # Exactly 61 distinct pending rows total — the target deduped, not stacked.
    all_pending = await approvals_service.list_approvals(
        "w1", "u1", {"status": "pending", "limit": 200}
    )
    target_rows = [r for r in all_pending if r.get("row_id") == "row-target"]
    assert len(target_rows) == 1, "the target row was duplicated, not deduped"


# ===========================================================================
# T-35 / T9 — outcomes/service must NOT call trust_ledger (correctness guard).
# The trust loop is written at EXACTLY ONE site (instinct_bridge, T8); a
# second write from the outcomes path would self-poison the score by
# double-counting one executed write (design MF-3).
# ===========================================================================


def test_outcomes_service_does_not_import_trust_ledger() -> None:
    """T-35: the outcomes service neither imports nor references trust_ledger.

    A source-level assertion (not a behavioral mock) — the invariant is
    'this module is structurally incapable of moving the trust score', so we
    assert the absence of the symbol entirely, the way the design specifies.
    """
    import inspect

    from pocketpaw_ee.cloud.outcomes import service as outcomes_service

    src = inspect.getsource(outcomes_service)
    assert "trust_ledger" not in src, (
        "outcomes/service.py must not reference trust_ledger — trust feedback "
        "is written at exactly one site (instinct_bridge.execute_approved_write)"
    )
    assert "record_correction" not in src
    # And the module object carries no such attribute (catches a re-export).
    assert not hasattr(outcomes_service, "trust_ledger")
