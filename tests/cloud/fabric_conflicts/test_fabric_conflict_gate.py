# tests/cloud/fabric_conflicts/test_fabric_conflict_gate.py
# Created: 2026-07-10 (FST-6 — the _fabric_conflict stewardship gate).
#
# Covers the sweep + propose + apply-on-approve executor for the conflict
# lifecycle: un-rankable Fabric conflicts become Instinct stewardship
# proposals a human arbitrates; approve PINs the chosen statement, reject
# keeps the policy winner. Clones the discipline of
# ``tests/ee/test_instinct_rule_propose.py`` (the precedent module — isolated
# stores via the lazy ``pocketpaw.stores`` seams). Asserts:
#
#   * an un-rankable conflict yields exactly ONE proposal with the EXACT blob
#     shape (tenancy + subject as SEPARATE top-level fields, editable
#     ``resolution`` defaulting to the policy winner, choices with provenance);
#   * re-sweep files NO duplicate while one is open (one open proposal per
#     ``(workspace_id, object_id, property)`` dedupe key);
#   * mode off → the sweep is inert (shadow AND enforce both sweep);
#   * approve (default choice) → the executor PINs the policy winner and the
#     cache reflects it in enforce; the answered conflict stops re-sweeping;
#   * approve with an EDITED choice → the rival gets pinned instead;
#   * reject → NO statement changes (the policy winner stands) and the
#     reject-memory (conflict_signature) stops the same conflict from
#     re-staging until a new rival changes its shape;
#   * the volume guard warns past 5 open stewardship proposals;
#   * tenant scoping: another workspace's sweep never sees the conflict;
#   * schema mismatch / unstaged choice → terminal FAILED, NO pin;
#   * idempotent re-execute → no double run, exactly ONE chain close.
#
# Run with:
#   uv run pytest tests/cloud/fabric_conflicts/ -q

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.fabric_conflicts import (  # noqa: E402
    FABRIC_CONFLICT_KIND,
    FABRIC_CONFLICT_PARAM_KEY,
    FABRIC_CONFLICT_SCHEMA,
    STEWARDSHIP_QUEUE_WARN_THRESHOLD,
    execute_approved_fabric_conflict,
    sweep_conflicts_to_proposals,
)

from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

pytestmark = pytest.mark.asyncio

WS = "w1"


# ---------------------------------------------------------------------------
# Fixtures — isolated Fabric + Instinct stores via the lazy store seams,
# cloned from the instinct-rule precedent tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "fabric-conflict-gate-test-secret")


@pytest.fixture
def fabric(tmp_path: Path, monkeypatch) -> FabricStore:
    st = FabricStore(tmp_path / "fabric_conflict_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda *a, **k: st)
    return st


@pytest.fixture
def instinct(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_conflict_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """The suite-wide FST mode seam — the gate reads it through the OSS
    chokepoint (late-bound), so this patch governs sweep + verbs alike."""
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


async def _conflicted_object(
    fabric: FabricStore,
    *,
    workspace_id: str = WS,
    properties: tuple[str, ...] = ("arr",),
) -> tuple[str, dict[str, tuple[str, str]]]:
    """One Customer whose ``properties`` each carry an un-rankable pair:
    two connector-tier, normal-rank, open, materially different statements
    observed within the epsilon. Returns (object_id, {property:
    (winner_stmt_id, rival_stmt_id)}) — the newer observation (value 120)
    provisionally wins over the rival (value 200)."""
    obj_type = await fabric.define_type(name="Customer", properties=[])
    obj = await fabric.create_object(
        obj_type.id, {p: 0 for p in properties}, source_connector="crm", source_id="c-1"
    )
    now = datetime.now()
    src_a = await fabric.upsert_source(
        "connector_run", connector="crm", run_id="r1", workspace_id=workspace_id
    )
    src_b = await fabric.upsert_source(
        "connector_run", connector="billing", run_id="r9", workspace_id=workspace_id
    )
    pairs: dict[str, tuple[str, str]] = {}
    for prop in properties:
        rival = await fabric.append_statement(
            obj.id,
            prop,
            200,
            src_b.id,
            "connector",
            observed_at=now - timedelta(hours=1),
            workspace_id=workspace_id,
        )
        winner = await fabric.append_statement(
            obj.id, prop, 120, src_a.id, "connector", observed_at=now, workspace_id=workspace_id
        )
        pairs[prop] = (winner.id, rival.id)
    return obj.id, pairs


async def _pending_conflict_actions(instinct: InstinctStore, workspace_id: str = WS) -> list[Any]:
    actions = await instinct.list_actions(
        pocket_id=workspace_id,
        status=ActionStatus.PENDING,
        workspace_id=workspace_id,
        limit=500,
    )
    return [a for a in actions if FABRIC_CONFLICT_PARAM_KEY in (a.parameters or {})]


# ---------------------------------------------------------------------------
# Sweep: exactly ONE proposal, exact blob shape.
# ---------------------------------------------------------------------------


async def test_sweep_files_exactly_one_proposal_with_exact_blob_shape(
    fabric, instinct, monkeypatch
):
    obj_id, pairs = await _conflicted_object(fabric)
    winner_id, rival_id = pairs["arr"]
    _set_mode(monkeypatch, "shadow")

    filed = await sweep_conflicts_to_proposals(WS)

    assert len(filed) == 1
    action = await instinct.get_action(filed[0])
    assert action.status == ActionStatus.PENDING
    assert str(action.pocket_id) == WS  # workspace anchoring

    blob = action.parameters[FABRIC_CONFLICT_PARAM_KEY]

    # EXACT key set — no stray keys.
    assert set(blob.keys()) == {
        "kind",
        "schema",
        "workspace_id",
        "object_id",
        "property",
        "object_type",
        "choices",
        "policy_winner_statement_id",
        "conflict_signature",
        "resolution",
        "summary",
        "correlation_id",
        "proposed_event_id",
    }
    assert blob["kind"] == FABRIC_CONFLICT_KIND == "fabric_conflict"
    assert blob["schema"] == FABRIC_CONFLICT_SCHEMA == 1

    # Tenancy + subject ride as SEPARATE top-level fields — NOT inside the
    # editable ``resolution`` (so an edit can't re-scope or re-target the pin).
    assert blob["workspace_id"] == WS
    assert blob["object_id"] == obj_id
    assert blob["property"] == "arr"
    assert blob["object_type"] == "Customer"
    assert set(blob["resolution"].keys()) == {"chosen_statement_id"}

    # The editable choice defaults to the policy's provisional winner.
    assert blob["resolution"]["chosen_statement_id"] == winner_id
    assert blob["policy_winner_statement_id"] == winner_id
    assert blob["conflict_signature"] == sorted([winner_id, rival_id])

    # Choices: the winner first, then the rival — each with value +
    # provenance (writer_class, observed_at, and the SourceRef identity).
    assert [c["statement_id"] for c in blob["choices"]] == [winner_id, rival_id]
    by_id = {c["statement_id"]: c for c in blob["choices"]}
    assert by_id[winner_id]["value"] == 120
    assert by_id[rival_id]["value"] == 200
    assert by_id[winner_id]["writer_class"] == "connector"
    assert by_id[winner_id]["source"]["connector"] == "crm"
    assert by_id[rival_id]["source"]["connector"] == "billing"
    assert by_id[winner_id]["observed_at"]

    # Chain fields: correlation minted at propose; proposed_event_id is the
    # best-effort back-write (present either way).
    assert blob["correlation_id"]
    assert "proposed_event_id" in blob


async def test_resweep_does_not_duplicate_while_open(fabric, instinct, monkeypatch):
    await _conflicted_object(fabric)
    _set_mode(monkeypatch, "shadow")

    first = await sweep_conflicts_to_proposals(WS)
    second = await sweep_conflicts_to_proposals(WS)

    assert len(first) == 1
    assert second == []  # one open proposal per dedupe key
    assert len(await _pending_conflict_actions(instinct)) == 1


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
async def test_mode_gate(fabric, instinct, monkeypatch, mode):
    """Off → inert (zero proposals). Shadow AND enforce both sweep — a
    queued conflict in shadow is observation with a human answer."""
    await _conflicted_object(fabric)
    _set_mode(monkeypatch, mode)

    filed = await sweep_conflicts_to_proposals(WS)

    if mode == "off":
        assert filed == []
        assert await _pending_conflict_actions(instinct) == []
    else:
        assert len(filed) == 1


# ---------------------------------------------------------------------------
# Approve — the choice → PIN mapping.
# ---------------------------------------------------------------------------


async def test_approve_default_choice_pins_policy_winner_and_enforce_cache(
    fabric, instinct, monkeypatch
):
    obj_id, pairs = await _conflicted_object(fabric)
    winner_id, _ = pairs["arr"]
    _set_mode(monkeypatch, "enforce")

    filed = await sweep_conflicts_to_proposals(WS)
    approved = await instinct.approve(filed[0], approver="steward-1")
    await execute_approved_fabric_conflict(approved)

    final = await instinct.get_action(filed[0])
    assert final.status == ActionStatus.EXECUTED, final.error

    # The policy winner is now PINNED — the durable "this one wins".
    stmts = await fabric.get_statements(obj_id, "arr", workspace_id=WS)
    pinned = [s for s in stmts if s.pinned]
    assert [s.id for s in pinned] == [winner_id]

    # Enforce: the cache reflects the pinned winner.
    obj = await fabric.get_object(obj_id, workspace_id=WS)
    assert obj is not None and obj.properties["arr"] == 120

    # The structured outcome was back-written onto the blob.
    outcome = final.parameters[FABRIC_CONFLICT_PARAM_KEY]["outcome"]
    assert outcome["status"] == "executed"
    assert outcome["pinned_statement_id"] == winner_id
    assert outcome["value"] == 120

    # The answered conflict is CLOSED — a re-sweep stages nothing (the
    # pinned path is never un-rankable).
    assert await sweep_conflicts_to_proposals(WS) == []


async def test_approve_with_edited_choice_pins_the_rival(fabric, instinct, monkeypatch):
    obj_id, pairs = await _conflicted_object(fabric)
    _, rival_id = pairs["arr"]
    _set_mode(monkeypatch, "enforce")

    filed = await sweep_conflicts_to_proposals(WS)
    approved = await instinct.approve(filed[0], approver="steward-1")
    # Simulate the approve-with-edits path (ApproveRequest.parameters): the
    # human re-points the editable choice at the staged rival. Same in-memory
    # mutation pattern the instinct-rule precedent tests use.
    approved.parameters[FABRIC_CONFLICT_PARAM_KEY]["resolution"] = {"chosen_statement_id": rival_id}
    await execute_approved_fabric_conflict(approved)

    final = await instinct.get_action(filed[0])
    assert final.status == ActionStatus.EXECUTED, final.error

    stmts = await fabric.get_statements(obj_id, "arr", workspace_id=WS)
    pinned = [s for s in stmts if s.pinned]
    assert [s.id for s in pinned] == [rival_id]

    # Enforce: the cache flips to the human's chosen value.
    obj = await fabric.get_object(obj_id, workspace_id=WS)
    assert obj is not None and obj.properties["arr"] == 200


# ---------------------------------------------------------------------------
# Reject — the policy winner stands; reject-memory stops the nag.
# ---------------------------------------------------------------------------


async def test_reject_keeps_policy_winner_and_resweep_respects_reject_memory(
    fabric, instinct, monkeypatch
):
    obj_id, pairs = await _conflicted_object(fabric)
    _set_mode(monkeypatch, "shadow")

    filed = await sweep_conflicts_to_proposals(WS)
    before = await fabric.get_statements(obj_id, "arr", workspace_id=WS)

    await instinct.reject(filed[0], reason="policy pick is fine", rejector="steward-1")
    # The executor NEVER runs on reject (router owns the close): no pins, no
    # deprecations, no validity changes — the policy winner simply stands.
    after = await fabric.get_statements(obj_id, "arr", workspace_id=WS)
    assert [(s.id, s.pinned, s.rank, s.valid_to) for s in after] == [
        (s.id, s.pinned, s.rank, s.valid_to) for s in before
    ]

    # Reject-memory: the SAME conflict (same signature) is not re-staged —
    # the human already answered "keep the policy winner".
    assert await sweep_conflicts_to_proposals(WS) == []

    # A NEW rival changes the conflict's signature → it re-stages.
    src = await fabric.upsert_source(
        "connector_run", connector="erp", run_id="r22", workspace_id=WS
    )
    await fabric.append_statement(
        obj_id, "arr", 300, src.id, "connector", observed_at=datetime.now(), workspace_id=WS
    )
    refiled = await sweep_conflicts_to_proposals(WS)
    assert len(refiled) == 1
    blob = (await instinct.get_action(refiled[0])).parameters[FABRIC_CONFLICT_PARAM_KEY]
    assert len(blob["choices"]) == 3  # the reshaped conflict carries all three


# ---------------------------------------------------------------------------
# Volume guard — the PRD queue-volume metric.
# ---------------------------------------------------------------------------


async def test_volume_guard_warns_past_threshold(fabric, instinct, monkeypatch, caplog):
    properties = tuple(f"kpi_{i}" for i in range(STEWARDSHIP_QUEUE_WARN_THRESHOLD + 1))
    await _conflicted_object(fabric, properties=properties)
    _set_mode(monkeypatch, "shadow")

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.fabric_conflicts.propose"):
        filed = await sweep_conflicts_to_proposals(WS)

    assert len(filed) == STEWARDSHIP_QUEUE_WARN_THRESHOLD + 1
    warnings = [r for r in caplog.records if "stewardship queue" in r.getMessage()]
    assert len(warnings) == 1
    assert f"has {STEWARDSHIP_QUEUE_WARN_THRESHOLD + 1} open" in warnings[0].getMessage()


async def test_volume_guard_quiet_at_threshold(fabric, instinct, monkeypatch, caplog):
    properties = tuple(f"kpi_{i}" for i in range(STEWARDSHIP_QUEUE_WARN_THRESHOLD))
    await _conflicted_object(fabric, properties=properties)
    _set_mode(monkeypatch, "shadow")

    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.fabric_conflicts.propose"):
        filed = await sweep_conflicts_to_proposals(WS)

    assert len(filed) == STEWARDSHIP_QUEUE_WARN_THRESHOLD
    assert [r for r in caplog.records if "stewardship queue" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# Tenancy — another workspace never sees the conflict.
# ---------------------------------------------------------------------------


async def test_tenant_scoping_respected(fabric, instinct, monkeypatch):
    await _conflicted_object(fabric, workspace_id="w1")
    _set_mode(monkeypatch, "shadow")

    # w2's sweep: the W4a scope hides w1's statements — nothing to stage.
    assert await sweep_conflicts_to_proposals("w2") == []
    assert await _pending_conflict_actions(instinct, "w2") == []

    # w1's own sweep stages it, stamped with w1's tenancy.
    filed = await sweep_conflicts_to_proposals("w1")
    assert len(filed) == 1
    blob = (await instinct.get_action(filed[0])).parameters[FABRIC_CONFLICT_PARAM_KEY]
    assert blob["workspace_id"] == "w1"


# ---------------------------------------------------------------------------
# Executor guards — fail clean, never pin on a bad blob.
# ---------------------------------------------------------------------------


async def test_schema_mismatch_fails_terminal_no_pin(fabric, instinct, monkeypatch):
    obj_id, _ = await _conflicted_object(fabric)
    _set_mode(monkeypatch, "shadow")
    filed = await sweep_conflicts_to_proposals(WS)

    approved = await instinct.approve(filed[0], approver="steward-1")
    approved.parameters[FABRIC_CONFLICT_PARAM_KEY]["schema"] = 2
    await execute_approved_fabric_conflict(approved)

    final = await instinct.get_action(filed[0])
    assert final.status == ActionStatus.FAILED, final.error
    assert "schema mismatch" in (final.error or "").lower()
    stmts = await fabric.get_statements(obj_id, "arr", workspace_id=WS)
    assert not any(s.pinned for s in stmts)


async def test_unstaged_choice_fails_terminal_no_pin(fabric, instinct, monkeypatch):
    """An edit cannot smuggle in an arbitrary statement id — the executor
    validates the chosen id against the STAGED choices."""
    obj_id, _ = await _conflicted_object(fabric)
    _set_mode(monkeypatch, "shadow")
    filed = await sweep_conflicts_to_proposals(WS)

    approved = await instinct.approve(filed[0], approver="steward-1")
    approved.parameters[FABRIC_CONFLICT_PARAM_KEY]["resolution"] = {
        "chosen_statement_id": "stm-evil"
    }
    await execute_approved_fabric_conflict(approved)

    final = await instinct.get_action(filed[0])
    assert final.status == ActionStatus.FAILED, final.error
    assert "staged choices" in (final.error or "")
    stmts = await fabric.get_statements(obj_id, "arr", workspace_id=WS)
    assert not any(s.pinned for s in stmts)


async def test_idempotent_reexecute_single_chain_close(fabric, instinct, monkeypatch):
    calls: list[dict[str, Any]] = []

    from pocketpaw_ee.cloud.fabric_conflicts import executor as conflict_executor

    real_close = conflict_executor._emit_chain_close

    def _spy(**kwargs: Any) -> None:
        calls.append(kwargs)
        return real_close(**kwargs)

    monkeypatch.setattr(conflict_executor, "_emit_chain_close", _spy)

    await _conflicted_object(fabric)
    _set_mode(monkeypatch, "shadow")
    filed = await sweep_conflicts_to_proposals(WS)

    approved = await instinct.approve(filed[0], approver="steward-1")
    await execute_approved_fabric_conflict(approved)
    # Re-invoke on the now-terminal Action — must NOT re-pin or re-close.
    again = await instinct.get_action(filed[0])
    await execute_approved_fabric_conflict(again)

    final = await instinct.get_action(filed[0])
    assert final.status == ActionStatus.EXECUTED, final.error
    assert len(calls) == 1
    assert calls[0]["passed"] is True
    assert calls[0]["action_outcome"] == "landed"
