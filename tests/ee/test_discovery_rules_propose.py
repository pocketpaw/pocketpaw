# tests/ee/test_discovery_rules_propose.py — S2-R5: orchestrate the THIRD
# (governed-rule) discovery proposal path alongside the fabric + pocket pair.
#
# Created: 2026-06-20 (S2-R5 / feat/szd-slice2-discovery). Covers the wiring that
# makes ``run_discovery_and_propose`` ALSO file ``_instinct_rule`` proposals — one
# per qualifying ``RuleDraft`` the ``RuleDigester`` (S2-R2) reverse-engineers from
# the workspace's Instinct correction exhaust — and supersede prior open rule
# proposals on a re-run. Clones the run→propose→supersede→approve structure of
# ``test_discovery_propose.py`` (connector read MOCKED; isolated InstinctStore via
# the lazy ``get_instinct_store`` seam; mongomock Beanie via ``beanie_test_db``; an
# inert recording bus so service ``emit()`` is quiet). Asserts:
#
#   * a run WITH qualifying correction exhaust files a PENDING ``_instinct_rule``
#     proposal carrying the shared ``run_id`` + ``role:"instinct_rules"`` — ALONGSIDE
#     the fabric + pocket pair (additive, not instead of);
#   * a SECOND run SUPERSEDES the prior open rule proposal (flips it to REJECTED /
#     superseded, does not stack a second);
#   * a low-confidence / sub-floor rule draft is SKIPPED (no proposal filed) with no
#     crash;
#   * a run with NO correction exhaust files the fabric + pocket pair but ZERO rule
#     proposals (additive, non-breaking);
#   * tenancy: a workspace mismatch raises at entry like the existing path;
#   * (smoke) approving the filed rule proposal via the executor → EXECUTED, and the
#     rule LANDS via ``rules.service.get_active_rules``.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_discovery_rules_propose.py -q

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.discovery import (  # noqa: E402
    DiscoveryRun,
    DiscoveryRunOptions,
    ReadAction,
    run_discovery_and_propose,
)
from pocketpaw_ee.discovery.orchestrate import (  # noqa: E402
    _find_discovery_marker,
)

from pocketpaw.fabric.store import FabricStore  # noqa: E402
from pocketpaw.instinct.correction import Correction, CorrectionPatch  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Mock connector surface (same shape as tests/ee/test_discovery_propose.py).
# ---------------------------------------------------------------------------


@dataclass
class _MockActionResult:
    success: bool
    data: Any = None
    error: str | None = None
    records_affected: int = 0


@dataclass
class _MockActionSchema:
    name: str
    method: str = "GET"
    trust_level: str = "auto"


class _MockAdapter:
    def __init__(self, schemas: list[_MockActionSchema], data_by_action: dict[str, Any]) -> None:
        self._schemas = schemas
        self._data_by_action = data_by_action

    async def actions(self) -> list[_MockActionSchema]:
        return list(self._schemas)

    async def execute(self, action: str, params: dict[str, Any]) -> _MockActionResult:
        if action not in self._data_by_action:
            return _MockActionResult(success=False, error=f"unknown action {action}")
        return _MockActionResult(success=True, data=self._data_by_action[action])


class _MockRegistry:
    def __init__(self, adapters: dict[str, _MockAdapter | None]) -> None:
        self._adapters = adapters

    async def ensure_connected(self, connector_name: str, scope_key: str) -> _MockAdapter | None:
        return self._adapters.get(connector_name)


# Clean, id-keyed records → a HIGH key_confidence type the gate materialises.
def _customers(n: int) -> list[dict[str, Any]]:
    return [{"id": f"cust-{i}", "name": f"Customer {i}", "tier": "gold"} for i in range(n)]


def _mock_run(adapters: dict[str, _MockAdapter | None]) -> DiscoveryRun:
    return DiscoveryRun(registry=_MockRegistry(adapters))


def _opts_for(read_actions: dict[str, list[ReadAction]]) -> DiscoveryRunOptions:
    return DiscoveryRunOptions(read_actions=read_actions)


# ---------------------------------------------------------------------------
# Correction exhaust helpers — corrections anchor on pocket_id == workspace_id
# (the discovery non-pocket convention). ``RuleDigester`` qualifies a path that
# recurs >= RULE_RECUR_THRESHOLD (3) times with a single dominant ``after`` value.
# ---------------------------------------------------------------------------


def _strong_correction(workspace_id: str, idx: int) -> Correction:
    """A correction that consistently raises ``category`` to ``escalated`` — three
    of these on the same path clear the recurrence threshold AND carry a single
    dominant target value, so the RuleDigester emits a high-confidence draft."""
    return Correction(
        action_id=f"act-{idx}",
        pocket_id=workspace_id,
        actor="u1",
        patches=[CorrectionPatch(path="category", before="normal", after="escalated")],
        context_summary=f"raised category #{idx}",
        action_title=f"Ticket #{idx}",
    )


def _weak_correction(workspace_id: str, idx: int) -> Correction:
    """A correction whose ``after`` differs every time — no constant target. Three
    of these clear the recurrence count but produce a presence-only rule scored
    BELOW the rule confidence floor, so the digester drops it (no draft)."""
    return Correction(
        action_id=f"wact-{idx}",
        pocket_id=workspace_id,
        actor="u1",
        patches=[CorrectionPatch(path="description", before="x", after=f"unique-{idx}")],
        context_summary=f"rewrote description #{idx}",
        action_title=f"Ticket #{idx}",
    )


async def _seed_corrections(store: InstinctStore, corrections: list[Correction]) -> None:
    for correction in corrections:
        await store.record_correction(correction)


# ---------------------------------------------------------------------------
# Fixtures — isolated stores wired into the lazy get_*_store seams (cloned from
# test_discovery_propose.py).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "discovery-rules-propose-test-secret")


@pytest.fixture(autouse=True)
def recording_bus():
    """Inert recording EventBus so service ``emit()`` calls don't raise."""
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
    st = InstinctStore(tmp_path / "instinct_discovery_rules_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture
def fabric(tmp_path: Path, monkeypatch) -> FabricStore:
    fs = FabricStore(tmp_path / "fabric_discovery_rules_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_fabric_store", lambda *a, **k: fs)
    return fs


def _rule_proposals(actions: list[Any]) -> list[Any]:
    """Pending actions carrying an ``_instinct_rule`` blob (the rule proposals)."""
    from pocketpaw_ee.cloud.instinct_rule_proposals import INSTINCT_RULE_PARAM_KEY

    return [a for a in actions if INSTINCT_RULE_PARAM_KEY in (a.parameters or {})]


# ---------------------------------------------------------------------------
# A run WITH qualifying correction exhaust files a PENDING _instinct_rule
# proposal — alongside the fabric + pocket pair, sharing the run_id.
# ---------------------------------------------------------------------------


async def test_run_with_corrections_files_tagged_rule_proposal(store, fabric):
    # Seed the qualifying correction exhaust (3x same path, same target value).
    await _seed_corrections(store, [_strong_correction("w1", i) for i in range(3)])

    adapters = {"crm": _MockAdapter(schemas=[], data_by_action={"list_customers": _customers(3)})}
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # The fabric + pocket pair is filed EXACTLY as before (additive).
    assert result.fabric_objects_action_id is not None
    assert result.pocket_action_id is not None

    # At least one rule proposal was filed, carrying the SHARED run_id + role.
    assert result.instinct_action_ids, "expected a governed-rule proposal to be filed"
    rule_action_id = result.instinct_action_ids[0]
    rule_action = await store.get_action(rule_action_id)
    assert rule_action.status == ActionStatus.PENDING

    marker = _find_discovery_marker(rule_action.parameters)
    assert marker is not None
    assert marker["run_id"] == result.run_id
    assert marker["role"] == "instinct_rules"
    assert marker["workspace_id"] == "w1"

    # The fabric marker shares the same run_id (one run → three roles).
    fabric_marker = _find_discovery_marker(
        (await store.get_action(result.fabric_objects_action_id)).parameters
    )
    assert fabric_marker["run_id"] == result.run_id

    # The staged rule_spec was reverse-engineered from the corrected ``category`` path.
    from pocketpaw_ee.cloud.instinct_rule_proposals import INSTINCT_RULE_PARAM_KEY

    blob = rule_action.parameters[INSTINCT_RULE_PARAM_KEY]
    assert blob["workspace_id"] == "w1"
    assert blob["user_id"] == "u1"
    assert blob["rule_spec"]["scope"]["workspace_id"] == "w1"
    assert "category" in blob["rule_spec"]["when"]


# ---------------------------------------------------------------------------
# A SECOND run supersedes the prior open rule proposal (no duplicate stack).
# ---------------------------------------------------------------------------


async def test_second_run_supersedes_prior_open_rule_proposal(store, fabric):
    await _seed_corrections(store, [_strong_correction("w1", i) for i in range(3)])

    adapters = {"crm": _MockAdapter(schemas=[], data_by_action={"list_customers": _customers(2)})}
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    first = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )
    assert first.instinct_action_ids, "first run should file a rule proposal"
    first_rule_id = first.instinct_action_ids[0]
    assert (await store.get_action(first_rule_id)).status == ActionStatus.PENDING

    second = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # The first run's rule proposal was superseded (REJECTED), not left stacked.
    assert first_rule_id in second.superseded_action_ids
    first_rule = await store.get_action(first_rule_id)
    assert first_rule.status == ActionStatus.REJECTED
    assert "superseded" in (first_rule.rejected_reason or "").lower()

    # The SECOND run's rule proposal is the only open rule proposal now.
    pending = await store.list_actions(
        pocket_id="w1", status=ActionStatus.PENDING, workspace_id="w1"
    )
    pending_rule_ids = {a.id for a in _rule_proposals(pending)}
    assert pending_rule_ids == set(second.instinct_action_ids)


# ---------------------------------------------------------------------------
# A low-confidence / sub-floor rule draft is skipped (no proposal filed), no crash.
# ---------------------------------------------------------------------------


async def test_low_confidence_rule_draft_skipped(store, fabric):
    # Weak corrections recur enough but carry NO constant target → the digester
    # scores them below RULE_CONFIDENCE_FLOOR and drops them (no draft).
    await _seed_corrections(store, [_weak_correction("w1", i) for i in range(3)])

    adapters = {"crm": _MockAdapter(schemas=[], data_by_action={"list_customers": _customers(2)})}
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # Fabric + pocket still filed; ZERO rule proposals (the weak draft was skipped).
    assert result.fabric_objects_action_id is not None
    assert result.pocket_action_id is not None
    assert result.instinct_action_ids == []

    pending = await store.list_actions(
        pocket_id="w1", status=ActionStatus.PENDING, workspace_id="w1"
    )
    assert _rule_proposals(pending) == []


# ---------------------------------------------------------------------------
# A run with NO correction exhaust files the fabric+pocket pair but ZERO rule
# proposals (additive, non-breaking — the existing path is untouched).
# ---------------------------------------------------------------------------


async def test_no_corrections_files_no_rule_proposal(store, fabric):
    adapters = {"crm": _MockAdapter(schemas=[], data_by_action={"list_customers": _customers(2)})}
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )

    # The fabric + pocket pair is filed exactly as before.
    assert result.fabric_objects_action_id is not None
    assert result.pocket_action_id is not None
    # No correction exhaust → no rule proposals.
    assert result.instinct_action_ids == []

    pending = await store.list_actions(
        pocket_id="w1", status=ActionStatus.PENDING, workspace_id="w1"
    )
    assert _rule_proposals(pending) == []


# ---------------------------------------------------------------------------
# Tenancy validation at entry (mismatch raises like the existing path).
# ---------------------------------------------------------------------------


async def test_requires_workspace_and_user(store, fabric):
    with pytest.raises(ValueError, match="workspace_id"):
        await run_discovery_and_propose(
            workspace_id="", user_id="u1", connector_ids=[], discovery_run=_mock_run({})
        )
    with pytest.raises(ValueError, match="user_id"):
        await run_discovery_and_propose(
            workspace_id="w1", user_id="", connector_ids=[], discovery_run=_mock_run({})
        )


# ---------------------------------------------------------------------------
# (Smoke) approving the filed rule proposal → EXECUTED, rule LANDS.
# ---------------------------------------------------------------------------


async def test_approving_rule_proposal_lands_the_rule(store, fabric, beanie_test_db):
    from pocketpaw_ee.cloud.instinct_rule_proposals import execute_approved_instinct_rule
    from pocketpaw_ee.cloud.rules import service as rules_service

    await _seed_corrections(store, [_strong_correction("w1", i) for i in range(3)])

    adapters = {"crm": _MockAdapter(schemas=[], data_by_action={"list_customers": _customers(2)})}
    opts = _opts_for({"crm": [ReadAction(action="list_customers", type_name="Customer")]})

    result = await run_discovery_and_propose(
        workspace_id="w1",
        user_id="u1",
        connector_ids=["crm"],
        opts=opts,
        discovery_run=_mock_run(adapters),
    )
    assert result.instinct_action_ids, "expected a rule proposal to approve"
    rule_action_id = result.instinct_action_ids[0]

    approved = await store.approve(rule_action_id, approver="u1")
    await execute_approved_instinct_rule(approved)

    final = await store.get_action(rule_action_id)
    assert final.status == ActionStatus.EXECUTED, final.error

    # The rule LANDED — visible via the slice-2 read seam.
    active = await rules_service.get_active_rules("w1")
    assert len(active) == 1
    landed = active[0]
    assert landed["workspace_id"] == "w1"
    assert landed["owner_user_id"] == "u1"
    assert landed["scope"]["workspace_id"] == "w1"
