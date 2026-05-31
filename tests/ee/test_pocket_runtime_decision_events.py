# tests/ee/test_pocket_runtime_decision_events.py
# Created: 2026-05-25 (RFC 09 Slice 2 — feat/rfc-09-slice-2-pocket-runtime-emits)
#
# Pins the chain-forming event emits the pocket runtime adds in
# Slice 2: ``agent.proposed`` from ``run_action``'s entry-after-allowlist
# point, ``policy.evaluated(passed=True)`` + ``decision.completed(landed)``
# on the direct-path success branch, ``decision.completed(failed)`` on
# each of the four gate-8 except branches, plus the parked-blob schema-2
# round-trip and the Instinct re-entry guard that prevents a double
# ``agent.proposed`` emit. End-to-end happy path: the chain folds into a
# real Decision row queryable via DecisionGraph.find.
#
# What's NOT covered here (lives in Slice 3 / Slice 4):
#   * ``policy.evaluated(passed=False)`` from Instinct park
#     (instinct_bridge propose path — Slice 3).
#   * ``human.corrected`` from approve/reject (Slice 3).
#   * decision.completed on reject_action (Slice 3).
#   * Abandon-path sweeper (Slice 4).
#
# Fixture strategy: build a small async helper that wires the journal +
# DecisionGraph + httpx mock + DNS stub, mirrors the
# `test_pocket_action_executor.py` pattern. We do NOT exercise the full
# Beanie / Instinct stores — instinct_bridge.execute_approved_write is
# unit-tested here via direct ``run_action(..., from_instinct=True,
# correlation_id=<parked>)`` calls, which is the same code path the
# bridge takes.

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pocketpaw_ee.cloud.decisions.service import (
    DecisionGraph,
    get_decision_graph,
    reset_projection_for_tests,
)
from pocketpaw_ee.cloud.decisions.store import set_db_path
from pocketpaw_ee.cloud.pockets import action_executor
from pocketpaw_ee.cloud.pockets import instinct_bridge as instinct_bridge_module
from pocketpaw_ee.cloud.pockets.action_executor import run_action
from soul_protocol.engine.journal import open_journal

import pocketpaw.journal_dep as journal_dep

BASE = "https://api.example.com"


# ---------------------------------------------------------------------------
# Fixtures — wire journal + projection + httpx + DNS
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Same housekeeping as `test_pocket_action_executor.py`."""
    action_executor._action_log.clear()
    yield
    action_executor._action_log.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Public IP for every hostname so the SSRF DNS guard passes."""

    def _fake_getaddrinfo(host, *_args, **_kwargs):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)


@pytest.fixture
def journal(tmp_path: Path):
    """Fresh on-disk journal per test, wired into the lazy
    ``pocketpaw.journal_dep.get_journal`` lookup the helper performs."""
    j = open_journal(tmp_path / "journal.db")
    journal_dep.reset_journal_cache()
    original = journal_dep._cached_journal

    def _stub() -> object:
        return j

    journal_dep._cached_journal = _stub  # type: ignore[assignment]
    yield j
    journal_dep._cached_journal = original  # type: ignore[assignment]
    journal_dep.reset_journal_cache()
    j.close()


@pytest.fixture
def graph(tmp_path: Path) -> DecisionGraph:
    """Fresh DecisionGraph + decisions.db per test, plumbed in as the
    process-global singleton via reset_projection_for_tests."""
    set_db_path(tmp_path / "decisions.db")
    reset_projection_for_tests()
    g = get_decision_graph()
    yield g
    reset_projection_for_tests()


def _mock_client_patch(monkeypatch, handler):
    """Inject an httpx MockTransport that handles outbound requests."""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_executor.httpx, "AsyncClient", _factory)


def _write_action(method: str = "POST", path: str = "/leases/42/renew") -> dict:
    return {"kind": "write_binding", "method": method, "path": path, "params": {}}


def _allow(method: str = "POST", pattern: str = "/leases/*/renew") -> list[dict]:
    return [{"method": method, "path_pattern": pattern}]


def _journal_actions(journal) -> list[str]:
    """All actions in append order — useful for assertions on which
    chain events fired in what order."""
    return [e.action for e in journal.replay_from(0)]


def _events_by_correlation(journal, correlation_id: UUID) -> list:
    """All events that share the given correlation_id, in append order."""
    return [e for e in journal.replay_from(0) if e.correlation_id == correlation_id]


# ---------------------------------------------------------------------------
# 1. agent.proposed — happy path
# ---------------------------------------------------------------------------


async def test_agent_proposed_emitted_on_direct_path(monkeypatch, journal, graph: DecisionGraph):
    """A direct (non-Instinct) run_action call lands `agent.proposed` in
    the journal with the minted correlation_id and the projection sees
    the chain start."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={"rent": 2000},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True

    # Exactly one agent.proposed event landed.
    proposed = [e for e in journal.replay_from(0) if e.action == "agent.proposed"]
    assert len(proposed) == 1, _journal_actions(journal)
    entry = proposed[0]
    assert entry.actor.kind == "agent"
    assert entry.actor.id == "user:u_alice"
    assert "workspace:w1" in entry.scope
    assert "pocket:p1" in entry.scope
    payload = entry.payload or {}
    assert payload["action"] == "mark_renewed"
    assert payload["pocket_id"] == "p1"
    assert payload["proposal_kind"] == "pocket_write"
    # Correlation_id was minted (a fresh UUID); journal entries carry it.
    assert entry.correlation_id is not None


# ---------------------------------------------------------------------------
# 2. agent.proposed re-entry guard — the subtle-bug catch
# ---------------------------------------------------------------------------


async def test_from_instinct_reentry_does_not_re_emit_agent_proposed(
    monkeypatch, journal, graph: DecisionGraph
):
    """RFC 09 audit Surprise 3 — the Instinct re-entry path (where
    `execute_approved_write` calls `run_action(..., from_instinct=True)`
    with the original correlation_id) MUST NOT emit a second
    `agent.proposed`. Otherwise `_fold_proposed` overwrites the chain's
    `proposed_at` / `intent` / `action` and the Decision record loses
    its original provenance."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    corr = uuid4()

    # First entry — agent.proposed fires because from_instinct=False.
    await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action={**_write_action(), "requires_instinct": True},
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        correlation_id=corr,
    )
    proposed_first = [
        e for e in _events_by_correlation(journal, corr) if e.action == "agent.proposed"
    ]
    assert len(proposed_first) == 1

    # Second entry — the Instinct re-entry. Same correlation_id.
    # from_instinct=True MUST suppress the proposed emit.
    await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action={**_write_action(), "requires_instinct": True},
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        from_instinct=True,
        correlation_id=corr,
    )

    proposed_total = [
        e for e in _events_by_correlation(journal, corr) if e.action == "agent.proposed"
    ]
    assert len(proposed_total) == 1, (
        "Instinct re-entry must NOT re-emit agent.proposed; got "
        f"{[e.action for e in _events_by_correlation(journal, corr)]}"
    )


# ---------------------------------------------------------------------------
# 3. correlation_id minted INSIDE run_action (not module-level)
# ---------------------------------------------------------------------------


async def test_each_call_mints_a_fresh_correlation_id(monkeypatch, journal, graph: DecisionGraph):
    """Two back-to-back run_action calls (no caller-supplied correlation
    id) must get distinct correlation_ids — a module-level cache would
    fold both writes into one chain incorrectly."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))

    for _ in range(2):
        await run_action(
            workspace_id="w1",
            pocket_id="p1",
            user_id="u_alice",
            action="mark_renewed",
            raw_action=_write_action(),
            path="/leases/42/renew",
            params={},
            base_url=BASE,
            auth_type="none",
            auth_header=None,
            token="",
            allowed_writes=_allow(),
        )

    proposed = [e for e in journal.replay_from(0) if e.action == "agent.proposed"]
    assert len(proposed) == 2
    assert proposed[0].correlation_id != proposed[1].correlation_id, (
        "fresh run_action calls must get distinct correlation_ids; got "
        f"{proposed[0].correlation_id} twice"
    )


async def test_caller_supplied_correlation_id_overrides_mint(
    monkeypatch, journal, graph: DecisionGraph
):
    """A caller (e.g. the Instinct re-entry path) can pass its own
    correlation_id — the emit uses that, not a freshly-minted one."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    corr = uuid4()

    await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        correlation_id=corr,
    )

    proposed = [e for e in journal.replay_from(0) if e.action == "agent.proposed"]
    assert len(proposed) == 1
    assert proposed[0].correlation_id == corr


# ---------------------------------------------------------------------------
# 4. decision.completed — direct success path (landed)
# ---------------------------------------------------------------------------


async def test_direct_success_emits_policy_and_decision_completed(
    monkeypatch, journal, graph: DecisionGraph
):
    """A direct (non-Instinct) success closes the chain with two events
    in sequence: `policy.evaluated(passed=True, policy='auto')` for
    chain symmetry, then `decision.completed(landed)` for the close."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"renewed": True}))

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True

    actions = _journal_actions(journal)
    assert actions == ["agent.proposed", "policy.evaluated", "decision.completed"]
    decided = [e for e in journal.replay_from(0) if e.action == "decision.completed"][0]
    assert (decided.payload or {}).get("passed") is True
    assert (decided.payload or {}).get("action_outcome") == "landed"


# ---------------------------------------------------------------------------
# 5. decision.completed — direct failure branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,handler,expected_code,expected_error_class",
    [
        (
            "http_error",
            lambda r: httpx.Response(500, json={"error": "boom"}),
            "http_error",
            "BackendHTTPError",
        ),
        (
            "redirect_is_guard_error",
            lambda r: httpx.Response(302, headers={"location": "/elsewhere"}),
            "redirect",
            "GuardError",
        ),
    ],
)
async def test_direct_failure_branches_emit_decision_completed_failed(
    monkeypatch,
    journal,
    graph: DecisionGraph,
    scenario: str,
    handler,
    expected_code: str,
    expected_error_class: str,
):
    """Each gate-8 except branch in `run_action` (HTTP error, guard
    error, ...) emits `decision.completed(passed=False, action_outcome=
    "failed", error_class=<type>)`. Parameterised so a future regression
    on any single branch surfaces individually."""
    _mock_client_patch(monkeypatch, handler)

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is False
    assert result["code"] == expected_code

    # The chain should end with decision.completed(failed) — NO policy.
    # evaluated for the failure paths (the auto-approve emit only fires
    # on the success branch).
    actions = _journal_actions(journal)
    assert "decision.completed" in actions, actions
    decided = [e for e in journal.replay_from(0) if e.action == "decision.completed"][0]
    payload = decided.payload or {}
    assert payload["passed"] is False
    assert payload["action_outcome"] == "failed"
    assert payload["error_class"] == expected_error_class


async def test_timeout_branch_emits_decision_completed_failed(
    monkeypatch, journal, graph: DecisionGraph
):
    """The TimeoutError branch emits decision.completed(failed,
    error_class='TimeoutError'). Triggered by patching asyncio.wait_for
    to raise — the underlying httpx call timing out reliably is fragile
    in tests."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))

    async def _raise_timeout(*_args, **_kwargs):
        raise TimeoutError("simulated")

    monkeypatch.setattr(action_executor.asyncio, "wait_for", _raise_timeout)

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is False
    assert result["code"] == "timeout"
    decided = [e for e in journal.replay_from(0) if e.action == "decision.completed"][0]
    assert (decided.payload or {})["error_class"] == "TimeoutError"


async def test_unexpected_exception_branch_emits_decision_completed_failed(
    monkeypatch, journal, graph: DecisionGraph
):
    """The bare `except Exception` branch catches anything else and
    emits decision.completed(failed, error_class=<type>)."""

    def _raise_runtime(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    # Force the asyncio.wait_for path to raise a generic Exception
    # subclass that's neither a TimeoutError, _BackendHTTPError, nor
    # _GuardError so the bare except branch fires.
    async def _raise_runtime_async(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(action_executor.asyncio, "wait_for", _raise_runtime_async)

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action=_write_action(),
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is False
    # Code is "error" for the generic-exception branch.
    assert result["code"] == "error"
    decided = [e for e in journal.replay_from(0) if e.action == "decision.completed"][0]
    payload = decided.payload or {}
    assert payload["error_class"] == "RuntimeError"
    assert payload["action_outcome"] == "failed"


# ---------------------------------------------------------------------------
# 6. Instinct re-entry path — bridge owns the chain close
# ---------------------------------------------------------------------------


async def test_instinct_reentry_success_does_not_double_emit_completed(
    monkeypatch, journal, graph: DecisionGraph
):
    """When `from_instinct=True` (the bridge re-entered run_action with
    `binding.requires_instinct=True`), the action_executor's gate-8
    success emit MUST NOT fire — the bridge will emit decision.completed
    at site (b). Otherwise the chain ends with TWO terminal events."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    corr = uuid4()

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action={**_write_action(), "requires_instinct": True},
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        from_instinct=True,
        correlation_id=corr,
    )
    assert result["ok"] is True

    # The action_executor should NOT have emitted policy.evaluated or
    # decision.completed on the re-entry path — only the bridge does.
    actions = _journal_actions(journal)
    assert "decision.completed" not in actions, actions
    assert "policy.evaluated" not in actions, actions


async def test_instinct_reentry_failure_does_not_emit_completed(
    monkeypatch, journal, graph: DecisionGraph
):
    """The HTTP-error branch on the re-entry path must NOT emit
    decision.completed — the bridge handles failure close at site (d)."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(500))
    corr = uuid4()

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action={**_write_action(), "requires_instinct": True},
        path="/leases/42/renew",
        params={},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        from_instinct=True,
        correlation_id=corr,
    )
    assert result["ok"] is False
    actions = _journal_actions(journal)
    assert "decision.completed" not in actions, actions


# ---------------------------------------------------------------------------
# 7. schema-2 parked blob carries correlation_id (+ parked_policy_event_id)
# ---------------------------------------------------------------------------


async def test_parked_blob_carries_correlation_id_and_event_id_placeholder(
    monkeypatch, journal, graph: DecisionGraph
):
    """A `requires_instinct` first-entry returns the `_park` dict with
    correlation_id on it (action_executor side). When the bridge
    `propose_pocket_write` persists the blob into the Instinct Action's
    parameters, the schema-2 fields land verbatim — correlation_id
    populated, parked_policy_event_id starts as None (Slice 3 fills it)."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    corr = uuid4()

    park_result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action={**_write_action(), "requires_instinct": True},
        path="/leases/42/renew",
        params={"rent": 2000},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
        correlation_id=corr,
    )
    assert park_result["code"] == "instinct_pending"
    park = park_result["_park"]
    assert park["correlation_id"] == str(corr)

    # Drive the bridge directly: `propose_pocket_write` builds the blob,
    # but we don't need the Instinct store actually backing it — we
    # build the blob shape it would persist by replicating
    # propose_pocket_write's blob-construction logic via a stub on
    # `get_instinct_store`.
    captured_blob: dict = {}

    class _StubStore:
        async def propose(self, **kwargs):
            captured_blob.update(kwargs.get("parameters", {}).get("_pocket_write", {}))

            class _A:
                id = "stub_id"

            return _A()

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: _StubStore())

    pocket = {"_id": "p1", "workspace": "w1", "owner": "u_alice", "name": "Lease 42"}
    await instinct_bridge_module.propose_pocket_write(
        pocket=pocket,
        backend_config=None,
        parked_write=park,
        requested_by="u_alice",
    )

    # Schema-2 invariants on the persisted blob.
    assert captured_blob["schema"] == instinct_bridge_module._POCKET_WRITE_SCHEMA
    assert captured_blob["schema"] == 2
    assert captured_blob["correlation_id"] == str(corr)
    # Slice 3 will populate this; Slice 2 leaves it as None so the
    # round-trip contract is stable.
    assert captured_blob["parked_policy_event_id"] is None
    # The non-chain fields still ride along (RFC 05 M2b.1 contract).
    assert captured_blob["workspace_id"] == "w1"
    assert captured_blob["requested_by"] == "u_alice"
    assert captured_blob["method"] == "POST"


# ---------------------------------------------------------------------------
# 8. End-to-end happy path — direct success folds into a Decision row
# ---------------------------------------------------------------------------


async def test_direct_success_folds_into_decision_via_graph_find(
    monkeypatch, journal, graph: DecisionGraph
):
    """RFC 07 promised the projection emits a Decision row when the
    chain closes. Slice 2 is the producer half — exercise the full
    happy path and assert a Decision shows up via `DecisionGraph.find`."""
    _mock_client_patch(monkeypatch, lambda r: httpx.Response(200, json={"renewed": True}))

    result = await run_action(
        workspace_id="w1",
        pocket_id="p1",
        user_id="u_alice",
        action="mark_renewed",
        raw_action={**_write_action(), "outcome": "renewal_completed"},
        path="/leases/42/renew",
        params={"rent": 2000},
        base_url=BASE,
        auth_type="none",
        auth_header=None,
        token="",
        allowed_writes=_allow(),
    )
    assert result["ok"] is True

    assert graph.store.count() == 1
    decisions = await graph.find()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action == "mark_renewed"
    assert decision.pocket_id == "p1"
    # The auto-approve policy emit lands as `instinct_policy="auto"`
    # with `instinct_policy_passed=True` so the chain shape is uniform
    # with the parked-then-approved future Slice 3 path.
    assert decision.instinct_policy == "auto"
    assert decision.instinct_policy_passed is True
    # Direct success has no rejection — the OutcomeRef stays None until
    # the outcomes service back-references (or stays None for v1 if no
    # outcome was named).
    assert decision.outcome is None or decision.outcome.status != "rejected"


# ---------------------------------------------------------------------------
# 9. Schema-2 parked blob: stale schema-1 approval fails loud (Captain Decision 5)
# ---------------------------------------------------------------------------


async def test_schema_1_parked_blob_marked_failed_on_post_deploy_approval(
    monkeypatch,
):
    """RFC 09 Captain Decision 5 — no backwards compat. A stale schema-1
    blob (from a build before this deploy) reaches `execute_approved_write`
    after approval; the existing schema-mismatch check marks it failed
    rather than executing a misinterpreted write. Drain the Instinct
    queue before deploying Slice 2."""
    captured_failures: list[str] = []

    class _StubStore:
        async def mark_failed(self, action_id: str, reason: str) -> None:
            captured_failures.append(reason)

        async def mark_executed(self, action_id: str, reason: str) -> None:  # noqa: ARG002
            pytest.fail("should not have executed a schema-1 blob")

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: _StubStore())

    # Schema-1 blob — pre-Slice-2 shape, no correlation_id field.
    schema_1_blob = {
        "schema": 1,
        "action": "mark_renewed",
        "method": "POST",
        "path": "/leases/42/renew",
        "params": {},
        "idempotency_key": None,
        "outcome": "renewal_completed",
        "workspace_id": "w1",
        "requested_by": "u_alice",
    }

    class _Action:
        id = "stale_action_id"
        pocket_id = "p1"
        parameters = {"_pocket_write": schema_1_blob}
        approved_by = "u_alice"

    await instinct_bridge_module.execute_approved_write(_Action())

    assert len(captured_failures) == 1
    assert "schema mismatch" in captured_failures[0]
