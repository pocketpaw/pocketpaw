# tests/cloud/test_instinct_auto_triage.py — smart-approval auto-triage tests.
# Created: 2026-06-16 (feat/instinct-smart-triage) — TDD safety suite for the
#   cheap-model auto-triage classifier (``ee.instinct.auto_triage``) and its
#   router hook (``_run_auto_triage`` in ``ee.instinct.router``). Proves the
#   five acceptance gates from the PRD:
#     1. A template ESCALATE_APPROVAL / BLOCK-ruled action (``rule_flagged``)
#        is NEVER auto-approved, at ANY approval level.
#     2. Every auto-approval produces a hash-chained ledger entry with
#        ``actor="system:triager"`` + the verdict + reasoning, and the chain
#        verifies intact.
#     3. A triager model failure → ESCALATE (no auto-approve), proven with a
#        faked failing TriagerLlm.
#     4. ASK level = today's behaviour — the triager is never invoked and the
#        action stays PENDING.
#     5. Tenancy — the auto-approval audit row is stamped with the action's
#        workspace; a cross-tenant read does not surface it.
#   Every test injects a FAKE TriagerLlm — a real ``claude -p`` call never runs.

from __future__ import annotations

import anyio
import pytest
from pocketpaw_ee.instinct.auto_triage import (
    ApprovalLevel,
    TriageContext,
    TriageDecision,
    TriageVerdict,
    maybe_auto_approve,
    resolve_approval_level,
    triage_action,
)

from pocketpaw.instinct.models import ActionStatus
from pocketpaw.instinct.store import InstinctStore

# Reuse the router test harness building blocks (the fake admin user + auth
# overrides + the shared propose payload). ``make_trigger`` and
# ``PROPOSE_PAYLOAD`` are plain helpers/values; the FastAPI app + client fixtures
# are defined locally below so the test-method ``client`` / ``router_store``
# parameters don't shadow an imported fixture (ruff F811).
from tests.cloud.test_ee_instinct import _FakeUser, make_trigger  # noqa: F401

PROPOSE_PAYLOAD = {
    "pocket_id": "pocket-router-triage-test",
    "title": "Send restock alert",
    "description": "Stock at 5 units",
    "recommendation": "Order 30 units from default supplier",
    "trigger": {"type": "agent", "source": "claude", "reason": "unit test trigger"},
    "category": "alert",
    "priority": "high",
    "parameters": {"quantity": 30},
}

# ---------------------------------------------------------------------------
# Fake LLM transports — a real ``claude -p`` call NEVER runs in these tests.
# ---------------------------------------------------------------------------


class _FakeApproveLlm:
    """Returns a high-confidence APPROVE verdict as strict JSON."""

    def __init__(self, confidence: float = 0.95) -> None:
        self.confidence = confidence
        self.calls = 0

    async def triage(self, *, prompt: str, context: TriageContext) -> str:
        self.calls += 1
        return (
            '{"verdict": "APPROVE", "reasoning": "Routine reorder within policy.", '
            f'"confidence": {self.confidence}}}'
        )


class _FakeFailingLlm:
    """Simulates a transport failure (timeout / CLI error / crash)."""

    def __init__(self) -> None:
        self.calls = 0

    async def triage(self, *, prompt: str, context: TriageContext) -> str:
        self.calls += 1
        raise RuntimeError("claude CLI timed out after 60.0s")


class _FakeMalformedLlm:
    """Returns non-JSON garbage — must fail-safe to ESCALATE."""

    def __init__(self) -> None:
        self.calls = 0

    async def triage(self, *, prompt: str, context: TriageContext) -> str:
        self.calls += 1
        return "I think this looks fine, approve it!"


class _NeverCalledLlm:
    """Raises if invoked — proves the gate short-circuited before the model."""

    async def triage(self, *, prompt: str, context: TriageContext) -> str:  # pragma: no cover
        raise AssertionError("triager LLM must NOT be invoked here")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return InstinctStore(tmp_path / "triage_test.db")


@pytest.fixture
def router_store(tmp_path):
    """Isolated store the router-level tests run against."""
    return InstinctStore(tmp_path / "router_triage_test.db")


@pytest.fixture
def test_app(monkeypatch):
    """FastAPI app with the instinct router + seeded admin auth context.

    Mirrors the harness in ``test_ee_instinct.py``: license is a no-op, the
    current user is a fake admin (instinct.approve/audit are ADMIN-tier), and the
    workspace plan is 'enterprise' so the plan-feature gate passes."""
    from unittest.mock import AsyncMock

    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.instinct.router import router

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    fake_user = _FakeUser()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: fake_user.active_workspace
    return app


@pytest.fixture
def client(test_app, router_store):
    """TestClient with the router ``_store`` patched to the isolated store."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    with patch("pocketpaw_ee.instinct.router._store", return_value=router_store):
        yield TestClient(test_app)


def _ctx(action, *, workspace_id="ws-A", rule_flagged=False, parked_blob=None):
    return TriageContext(
        workspace_id=workspace_id,
        pocket_id=action.pocket_id,
        action_id=action.id,
        title=action.title,
        description=action.description,
        recommendation=action.recommendation,
        rule_flagged=rule_flagged,
        parked_blob=parked_blob or {},
        instinct_rules=[{"when": "amount > 1000", "action": "require_approval"}],
    )


async def _propose(store, *, workspace_id="ws-A", pocket_id="pocket-1"):
    return await store.propose(
        pocket_id=pocket_id,
        title="Reorder inventory",
        description="Stock at 4, threshold 10",
        recommendation="Order 20 units",
        trigger=make_trigger(),
        workspace_id=workspace_id,
    )


# ===========================================================================
# AC1 — rule-flagged actions are NEVER auto-approved, at any level.
# ===========================================================================


class TestRuleFlaggedNeverAutoApproved:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", [ApprovalLevel.TRIAGE, ApprovalLevel.TRUSTED])
    async def test_rule_flagged_escalates_without_calling_model(self, level):
        """A rule-flagged action short-circuits to ESCALATE and never reaches
        the model — even one that would have approved."""
        llm = _NeverCalledLlm()
        ctx = TriageContext(
            workspace_id="ws-A",
            pocket_id="p",
            action_id="act-1",
            title="t",
            description="d",
            recommendation="r",
            rule_flagged=True,
        )
        decision = await triage_action(ctx, level=level, llm=llm)
        assert decision.verdict == TriageVerdict.ESCALATE

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", [ApprovalLevel.TRIAGE, ApprovalLevel.TRUSTED])
    async def test_rule_flagged_action_stays_pending(self, store, level):
        """Through the orchestration: a rule-flagged action is NOT auto-approved
        even when the (would-be) model says APPROVE."""
        action = await _propose(store)
        outcome = await maybe_auto_approve(
            store=store,
            action=action,
            context=_ctx(action, rule_flagged=True),
            level=level,
            llm=_FakeApproveLlm(),  # would approve, but the gate blocks it
        )
        assert outcome.auto_approved is False
        assert outcome.decision.verdict == TriageVerdict.ESCALATE
        fetched = await store.get_action(action.id)
        assert fetched.status == ActionStatus.PENDING


# ===========================================================================
# AC2 — every auto-approval is hash-chained + actor=system:triager + reasoning.
# ===========================================================================


class TestAutoApprovalIsAudited:
    @pytest.mark.asyncio
    async def test_auto_approval_writes_chained_ledger_row(self, store):
        action = await _propose(store)
        outcome = await maybe_auto_approve(
            store=store,
            action=action,
            context=_ctx(action),
            level=ApprovalLevel.TRIAGE,
            llm=_FakeApproveLlm(),
        )
        assert outcome.auto_approved is True

        # The action flipped to APPROVED by the triager.
        fetched = await store.get_action(action.id)
        assert fetched.status == ActionStatus.APPROVED
        assert fetched.approved_by == "system:triager"

        # The audit row exists with the right event + actor + reasoning.
        entries = await store.query_audit(pocket_id=action.pocket_id)
        auto = [e for e in entries if e.event == "action_auto_approved"]
        assert len(auto) == 1
        entry = auto[0]
        assert entry.actor == "system:triager"
        assert entry.context.get("triager_verdict") == "APPROVE"
        assert "Routine reorder" in entry.context.get("triager_reasoning", "")
        assert entry.context.get("triager_confidence") == pytest.approx(0.95)

        # The hash chain verifies intact after the auto-approval append.
        report = await store.verify_audit_chain()
        assert report["intact"] is True
        assert report["broken_at"] is None
        assert report["hashed"] >= 2  # proposed + auto_approved

    @pytest.mark.asyncio
    async def test_auto_approval_chain_links_to_propose(self, store):
        """The auto_approved row chains onto the propose row (prev_hash linkage)
        — tampering with either breaks verification."""
        import sqlite3

        action = await _propose(store)
        await maybe_auto_approve(
            store=store,
            action=action,
            context=_ctx(action),
            level=ApprovalLevel.TRIAGE,
            llm=_FakeApproveLlm(),
        )
        # Tamper: rewrite the auto_approved row's reasoning directly in SQLite,
        # bypassing the loud append. The chain must now report broken.
        con = sqlite3.connect(store._db_path)
        con.execute(
            "UPDATE instinct_audit SET context = ? WHERE event = 'action_auto_approved'",
            ('{"triager_verdict": "APPROVE", "triager_reasoning": "TAMPERED"}',),
        )
        con.commit()
        con.close()

        report = await store.verify_audit_chain()
        assert report["intact"] is False
        assert report["broken_at"] is not None


# ===========================================================================
# AC3 — triager failure / malformed / low-confidence → ESCALATE (fail-safe).
# ===========================================================================


class TestFailSafeEscalation:
    @pytest.mark.asyncio
    async def test_model_failure_escalates_no_auto_approve(self, store):
        action = await _propose(store)
        outcome = await maybe_auto_approve(
            store=store,
            action=action,
            context=_ctx(action),
            level=ApprovalLevel.TRIAGE,
            llm=_FakeFailingLlm(),
        )
        assert outcome.auto_approved is False
        assert outcome.decision.verdict == TriageVerdict.ESCALATE
        fetched = await store.get_action(action.id)
        assert fetched.status == ActionStatus.PENDING
        # No auto-approval audit row was written.
        entries = await store.query_audit(pocket_id=action.pocket_id)
        assert not [e for e in entries if e.event == "action_auto_approved"]

    @pytest.mark.asyncio
    async def test_malformed_json_escalates(self, store):
        action = await _propose(store)
        decision = await triage_action(
            _ctx(action), level=ApprovalLevel.TRIAGE, llm=_FakeMalformedLlm()
        )
        assert decision.verdict == TriageVerdict.ESCALATE

    @pytest.mark.asyncio
    async def test_low_confidence_approve_is_downgraded_to_escalate(self, store):
        action = await _propose(store)
        decision = await triage_action(
            _ctx(action),
            level=ApprovalLevel.TRIAGE,
            llm=_FakeApproveLlm(confidence=0.4),  # below the 0.75 floor
        )
        assert decision.verdict == TriageVerdict.ESCALATE


# ===========================================================================
# AC4 — ASK level = today's behaviour: triager never invoked, action pending.
# ===========================================================================


class TestAskLevelUnchanged:
    @pytest.mark.asyncio
    async def test_ask_level_never_invokes_model(self, store):
        action = await _propose(store)
        llm = _NeverCalledLlm()  # raises if called
        outcome = await maybe_auto_approve(
            store=store,
            action=action,
            context=_ctx(action),
            level=ApprovalLevel.ASK,
            llm=llm,
        )
        assert outcome.auto_approved is False
        assert outcome.decision.verdict == TriageVerdict.ESCALATE
        fetched = await store.get_action(action.id)
        assert fetched.status == ActionStatus.PENDING
        # Only the propose audit row exists — no auto-approve row.
        entries = await store.query_audit(pocket_id=action.pocket_id)
        assert [e.event for e in entries] == ["action_proposed"]


# ===========================================================================
# AC5 — tenancy: the auto-approval audit row is scoped to the action's
# workspace; a cross-tenant read does not surface it.
# ===========================================================================


class TestTenancyScoping:
    @pytest.mark.asyncio
    async def test_auto_approval_audit_scoped_to_workspace(self, store):
        action = await _propose(store, workspace_id="ws-A")
        await maybe_auto_approve(
            store=store,
            action=action,
            context=_ctx(action, workspace_id="ws-A"),
            level=ApprovalLevel.TRIAGE,
            llm=_FakeApproveLlm(),
        )
        # Tenant A sees the auto-approval row.
        a_rows = await store.query_audit(pocket_id=action.pocket_id, workspace_id="ws-A")
        assert any(e.event == "action_auto_approved" for e in a_rows)
        # Tenant B does NOT see it.
        b_rows = await store.query_audit(pocket_id=action.pocket_id, workspace_id="ws-B")
        assert not any(e.event == "action_auto_approved" for e in b_rows)


# ===========================================================================
# Level resolution precedence.
# ===========================================================================


class TestApprovalLevelResolution:
    def test_default_is_triage(self, monkeypatch):
        monkeypatch.delenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", raising=False)
        assert resolve_approval_level() == ApprovalLevel.TRIAGE

    def test_pocket_override_wins(self, monkeypatch):
        monkeypatch.delenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", raising=False)
        assert (
            resolve_approval_level(workspace_level="TRUSTED", pocket_level="ASK")
            == ApprovalLevel.ASK
        )

    def test_env_is_a_fallback(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", "TRUSTED")
        assert resolve_approval_level() == ApprovalLevel.TRUSTED

    def test_bad_value_falls_through_to_default(self, monkeypatch):
        monkeypatch.delenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", raising=False)
        assert resolve_approval_level(pocket_level="garbage") == ApprovalLevel.TRIAGE


# ===========================================================================
# TriageDecision confidence clamping.
# ===========================================================================


class TestConfidenceClamp:
    def test_percentage_confidence_is_normalized(self):
        d = TriageDecision(verdict=TriageVerdict.APPROVE, confidence=95)
        assert d.confidence == pytest.approx(0.95)

    def test_out_of_range_clamps_to_one(self):
        d = TriageDecision(verdict=TriageVerdict.APPROVE, confidence=250)
        assert d.confidence == 1.0


# ===========================================================================
# Router-level integration — the propose_action hook end to end.
# ===========================================================================


class TestProposeRouteAutoTriage:
    """POST /instinct/actions runs the auto-triage hook synchronously.

    The route resolves the transport via ``resolve_triager_llm`` (a real
    ``ClaudeCliTriagerLlm`` in production); each test monkeypatches it to a fake
    so a real ``claude -p`` call never runs."""

    def test_approve_verdict_auto_approves_via_route(self, client, router_store, monkeypatch):
        """At TRIAGE level with an APPROVE-ing model, the proposed action comes
        back already APPROVED and carries an action_auto_approved ledger row."""
        import pocketpaw_ee.instinct.auto_triage as at

        monkeypatch.setenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", "TRIAGE")
        monkeypatch.setattr(at, "resolve_triager_llm", lambda: _FakeApproveLlm())

        resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "approved"
        assert body["approved_by"] == "system:triager"

        entries = anyio.run(
            lambda: router_store.query_audit(pocket_id=PROPOSE_PAYLOAD["pocket_id"])
        )
        assert any(e.event == "action_auto_approved" for e in entries)

    def test_ask_level_returns_pending_unchanged(self, client, router_store, monkeypatch):
        """At ASK level the triager is never invoked — the proposed action comes
        back PENDING exactly as it does today (byte-for-byte behaviour)."""
        import pocketpaw_ee.instinct.auto_triage as at

        monkeypatch.setenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", "ASK")
        # If the model were invoked it would raise — proves ASK never calls it.
        monkeypatch.setattr(at, "resolve_triager_llm", lambda: _NeverCalledLlm())

        resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "pending"
        assert body["approved_by"] is None

        entries = anyio.run(
            lambda: router_store.query_audit(pocket_id=PROPOSE_PAYLOAD["pocket_id"])
        )
        assert [e.event for e in entries] == ["action_proposed"]

    def test_rule_flagged_via_route_stays_pending(self, client, router_store, monkeypatch):
        """A proposal carrying a rule_flagged triage hint is never auto-approved
        even with an APPROVE-ing model."""
        import pocketpaw_ee.instinct.auto_triage as at

        monkeypatch.setenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", "TRUSTED")
        monkeypatch.setattr(at, "resolve_triager_llm", lambda: _FakeApproveLlm())

        payload = {
            **PROPOSE_PAYLOAD,
            "parameters": {"quantity": 30, "_triage": {"rule_flagged": True}},
        }
        resp = client.post("/instinct/actions", json=payload)
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"

    def test_model_failure_via_route_stays_pending(self, client, router_store, monkeypatch):
        """A failing triager model never auto-approves — the route returns the
        pending proposal (fail-safe)."""
        import pocketpaw_ee.instinct.auto_triage as at

        monkeypatch.setenv("POCKETPAW_INSTINCT_APPROVAL_LEVEL", "TRIAGE")
        monkeypatch.setattr(at, "resolve_triager_llm", lambda: _FakeFailingLlm())

        resp = client.post("/instinct/actions", json=PROPOSE_PAYLOAD)
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"
