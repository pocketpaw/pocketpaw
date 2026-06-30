# tests/ee/test_instinct_rule_propose.py — S2-R3 (_instinct_rule gate type).
#
# Created: 2026-06-20 (S2-R3 / feat/szd-slice2-discovery). Covers the propose +
# apply-on-approve executor for the ``_instinct_rule`` Instinct proposal type —
# the gate that stages a discovered governed rule for a human to approve / edit /
# reject before it becomes an active ``InstinctRuleDoc``. Clones the discipline of
# ``test_discovery_propose.py`` (isolated InstinctStore via the lazy
# ``get_instinct_store`` seam + mongomock Beanie via ``beanie_test_db`` + an inert
# recording bus so service ``emit()`` is quiet). Asserts:
#
#   * propose builds the EXACT blob shape (kind / schema=1 / workspace_id /
#     rule_spec editable sub-dict / summary / correlation_id minted /
#     proposed_event_id back-written), with tenancy + owner as SEPARATE top-level
#     fields (NOT inside the editable rule_spec);
#   * approve → executor → EXECUTED, and the rule LANDS (visible via
#     ``rules.service.get_active_rules``);
#   * a schema-version mismatch (blob schema=2) → terminal FAILED, NO rule written;
#   * idempotent re-approve → no double-write (active-rule count stable);
#   * a malformed rule_spec (bad action / invalid CEL) → executor ``_fail`` (not a
#     crash), no rule written;
#   * the executor emits exactly ONE ``decision.completed`` chain-close on approve.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_instinct_rule_propose.py -q

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.instinct_rule_proposals import (  # noqa: E402
    INSTINCT_RULE_KIND,
    INSTINCT_RULE_PARAM_KEY,
    INSTINCT_RULE_SCHEMA,
    execute_approved_instinct_rule,
    propose_instinct_rule,
)

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# A valid editable rule_spec (RuleDraft-shaped). Tenancy lives in scope here, but
# the propose helper carries workspace_id / owner as SEPARATE top-level blob fields.
# ---------------------------------------------------------------------------


def _rule_spec(workspace_id: str = "w1") -> dict[str, Any]:
    return {
        "name": "Require approval on high-value invoices",
        "description": "Flag invoices over 10k for human review.",
        "when": "object.amount > 10000",
        "action": "require_approval",
        "scope": {"workspace_id": workspace_id, "object_type": "Invoice"},
        "confidence": 0.82,
        "provenance": ["audit:row-1", "correction:c-9"],
    }


# ---------------------------------------------------------------------------
# Fixtures — isolated InstinctStore + inert bus, cloned from test_discovery_propose.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "instinct-rule-propose-test-secret")


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so ``rules.service.create_rule``'s ``emit()`` is quiet."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler: Any) -> None:  # noqa: ARG002
            return

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _RecordingBus()  # type: ignore[attr-defined]
    yield bus_mod._bus
    bus_mod._bus = prev  # type: ignore[attr-defined]


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_rule_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


# ---------------------------------------------------------------------------
# propose: EXACT blob shape, chain ids, tenancy/owner separation.
# ---------------------------------------------------------------------------


async def test_propose_builds_exact_blob_shape(store):
    action_id = await propose_instinct_rule(
        workspace_id="w1",
        user_id="u1",
        rule_spec=_rule_spec("w1"),
        summary="Stage the high-value-invoice rule for review.",
    )

    action = await store.get_action(action_id)
    assert action.status == ActionStatus.PENDING

    blob = action.parameters[INSTINCT_RULE_PARAM_KEY]

    # EXACT key set — kind, schema, workspace_id, rule_spec, summary, correlation_id,
    # proposed_event_id (+ no stray keys).
    assert set(blob.keys()) == {
        "kind",
        "schema",
        "workspace_id",
        "user_id",
        "rule_spec",
        "summary",
        "correlation_id",
        "proposed_event_id",
    }
    assert blob["kind"] == INSTINCT_RULE_KIND == "instinct_rule"
    assert blob["schema"] == INSTINCT_RULE_SCHEMA == 1

    # Tenancy + owner ride as SEPARATE top-level fields — NOT inside the editable
    # rule_spec (so a tenant editing the proposal can't move workspace/owner).
    assert blob["workspace_id"] == "w1"
    assert blob["user_id"] == "u1"
    # Tenancy/owner are NOT smuggled into the editable rule_spec.
    assert "workspace_id" not in blob["rule_spec"]
    assert "owner" not in blob["rule_spec"]
    assert "user_id" not in blob["rule_spec"]

    # rule_spec is the editable RuleDraft sub-dict, verbatim.
    assert blob["rule_spec"]["name"] == "Require approval on high-value invoices"
    assert blob["rule_spec"]["action"] == "require_approval"
    assert blob["rule_spec"]["when"] == "object.amount > 10000"
    assert blob["rule_spec"]["scope"]["workspace_id"] == "w1"

    assert blob["summary"] == "Stage the high-value-invoice rule for review."

    # correlation_id minted; proposed_event_id back-written (a real id, not None).
    assert blob["correlation_id"]
    assert blob["proposed_event_id"]

    # pocket_id == workspace_id anchoring (the Action surfaces in per-workspace queries).
    assert str(action.pocket_id) == "w1"


async def test_propose_requires_workspace_and_user(store):
    with pytest.raises(ValueError, match="workspace_id"):
        await propose_instinct_rule(workspace_id="", user_id="u1", rule_spec=_rule_spec())
    with pytest.raises(ValueError, match="user_id"):
        await propose_instinct_rule(workspace_id="w1", user_id="", rule_spec=_rule_spec())


# ---------------------------------------------------------------------------
# approve → executor → EXECUTED, rule LANDS.
# ---------------------------------------------------------------------------


async def test_approve_executes_and_rule_lands(store, beanie_test_db):
    from pocketpaw_ee.cloud.rules import service as rules_service

    action_id = await propose_instinct_rule(
        workspace_id="w1",
        user_id="u1",
        rule_spec=_rule_spec("w1"),
    )

    approved = await store.approve(action_id, approver="u1")
    await execute_approved_instinct_rule(approved)

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED, final.error

    # The rule LANDED — visible via the slice-2 read seam.
    active = await rules_service.get_active_rules("w1")
    assert len(active) == 1
    landed = active[0]
    assert landed["name"] == "Require approval on high-value invoices"
    assert landed["action"] == "require_approval"
    assert landed["workspace_id"] == "w1"
    assert landed["owner_user_id"] == "u1"
    assert landed["scope"]["workspace_id"] == "w1"
    assert landed["scope"]["object_type"] == "Invoice"

    # The structured outcome was back-written onto the blob.
    outcome = final.parameters[INSTINCT_RULE_PARAM_KEY]["outcome"]
    assert outcome["status"] == "executed"
    assert outcome["rule_id"]


# ---------------------------------------------------------------------------
# Schema-version mismatch → terminal FAILED, NO rule written.
# ---------------------------------------------------------------------------


async def test_schema_mismatch_fails_terminal_no_write(store, beanie_test_db):
    from pocketpaw_ee.cloud.rules import service as rules_service

    action_id = await propose_instinct_rule(
        workspace_id="w1",
        user_id="u1",
        rule_spec=_rule_spec("w1"),
    )

    approved = await store.approve(action_id, approver="u1")
    # Corrupt the in-memory blob to an incompatible build schema (the executor
    # reads action.parameters directly — same mutation pattern as the pocket-create
    # gate test).
    approved.parameters[INSTINCT_RULE_PARAM_KEY]["schema"] = 2
    await execute_approved_instinct_rule(approved)

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.FAILED, final.error
    assert "schema mismatch" in (final.error or "").lower()

    # NO rule written.
    assert await rules_service.get_active_rules("w1") == []


# ---------------------------------------------------------------------------
# Idempotent re-approve → no double-write.
# ---------------------------------------------------------------------------


async def test_idempotent_reapprove_no_double_write(store, beanie_test_db):
    from pocketpaw_ee.cloud.rules import service as rules_service

    action_id = await propose_instinct_rule(
        workspace_id="w1",
        user_id="u1",
        rule_spec=_rule_spec("w1"),
    )

    approved = await store.approve(action_id, approver="u1")
    await execute_approved_instinct_rule(approved)
    assert len(await rules_service.get_active_rules("w1")) == 1

    # Re-invoke the executor on the now-terminal Action — must NOT create a 2nd rule.
    again = await store.get_action(action_id)
    await execute_approved_instinct_rule(again)
    assert len(await rules_service.get_active_rules("w1")) == 1


# ---------------------------------------------------------------------------
# Malformed rule_spec → executor _fail (not a crash), no rule written.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_spec",
    [
        # invalid action literal
        {
            "name": "bad",
            "when": "object.amount > 1",
            "action": "nuke",
            "scope": {"workspace_id": "w1"},
        },
        # invalid CEL trigger
        {
            "name": "bad",
            "when": "this is (not valid CEL",
            "action": "notify",
            "scope": {"workspace_id": "w1"},
        },
    ],
)
async def test_malformed_rule_spec_fails_not_crash(store, beanie_test_db, bad_spec):
    from pocketpaw_ee.cloud.rules import service as rules_service

    action_id = await propose_instinct_rule(
        workspace_id="w1",
        user_id="u1",
        rule_spec=_rule_spec("w1"),
    )

    approved = await store.approve(action_id, approver="u1")
    # Swap in a structurally-invalid rule_spec on the in-memory blob.
    approved.parameters[INSTINCT_RULE_PARAM_KEY]["rule_spec"] = bad_spec
    # Must NOT raise — a bad spec is a failed outcome, not a crash.
    await execute_approved_instinct_rule(approved)

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.FAILED, final.error
    assert await rules_service.get_active_rules("w1") == []


# ---------------------------------------------------------------------------
# The executor emits EXACTLY ONE decision.completed chain-close on approve.
# ---------------------------------------------------------------------------


async def test_executor_emits_exactly_one_chain_close_on_approve(
    store, beanie_test_db, monkeypatch
):
    calls: list[dict[str, Any]] = []

    from pocketpaw_ee.cloud.instinct_rule_proposals import executor as rule_executor

    real_close = rule_executor._emit_chain_close

    def _spy(**kwargs: Any) -> None:
        calls.append(kwargs)
        return real_close(**kwargs)

    monkeypatch.setattr(rule_executor, "_emit_chain_close", _spy)

    action_id = await propose_instinct_rule(
        workspace_id="w1",
        user_id="u1",
        rule_spec=_rule_spec("w1"),
    )
    approved = await store.approve(action_id, approver="u1")
    await execute_approved_instinct_rule(approved)

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED, final.error

    # Exactly one terminal close, and it is the success terminal.
    assert len(calls) == 1
    assert calls[0]["passed"] is True
    assert calls[0]["action_outcome"] == "landed"
