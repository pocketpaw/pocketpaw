# tests/cloud/growth/test_gate.py — the /growth Instinct send gate (G-4).
#
# The security-critical suite. What it proves:
#
#   1. GATE INTEGRITY — no code path flips a draft to approved/sent without an
#      approved ``_growth_send`` proposal: the PUBLIC status route refuses the
#      gate-owned edges (403 ``draft.gate_required``) even though they are
#      legal per the transition table, and the dispatch STUB marks nothing
#      sent.
#   2. Propose files the Instinct proposal (blob carries draft/prospect/
#      channel/preview + tenancy) and flips the draft to ``proposed``.
#   3. Single-approve AND bulk-approve both flip the draft to ``approved`` and
#      enqueue the ``growth.dispatch`` job on the ``growth`` arq queue (fake
#      pool records the call).
#   4. A cross-tenant approve/reject is a 403 from ``_assert_growth_workspace``
#      BEFORE any mutation (single + bulk).
#   5. Reject (single + bulk) flips the draft to ``rejected`` and enqueues
#      NOTHING.
#   6. Enqueue failure → ``store.mark_failed(error=...)`` (Action terminal
#      ``failed``); a since-demoted proposer fails closed with no enqueue.
#
# Security-review regressions (F1 / F2 / F3) live in the last three classes:
#   F1 — approve-with-edits can no longer re-point a gated blob. The tenancy
#        sweep ran on the PRE-edit Action while the executor reads the
#        POST-edit blob, so a permitted ``parameters`` edit let a cleared
#        approver aim the operation at another tenant's draft. Identity fields
#        (tenancy, proposer, target, verb, chain) are now pinned back from the
#        stored proposal for EVERY reserved gated kind, and an edit can neither
#        add nor delete a gated blob. Content stays editable.
#   F2 — the generic ``POST /instinct/actions`` route (MEMBER-tier) can no
#        longer mint a gated blob at all, AND the executor resolves the
#        proposer from the ACTION's trigger instead of the blob's
#        ``requested_by`` — so a forged blob can't nominate whose role gets
#        re-checked.
#   F3 — the growth routes carry real RBAC: reads MEMBER, writes MEMBER,
#        the outbound propose verb ADMIN.
#
# Harness: the growth router (RequestContext override per workspace, mongomock
# Beanie via ``mongo_db``) and the REAL instinct router (fake admin user +
# workspace overrides) mounted in separate ASGI apps sharing ONE tmp
# InstinctStore — ``pocketpaw.stores.get_instinct_store`` is monkeypatched so
# the propose path, the router's ``_store`` and the executor all hit it. The
# arq pool and the proposer-RBAC re-check are patched at the executor seams
# (``_get_pool`` / ``_proposer_still_authorized``), mirroring the ship gate
# tests.
#
# Created 2026-07-27 (feat/growth-g4): new module.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.growth import executor as growth_executor
from pocketpaw_ee.cloud.growth.router import router as growth_router
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.instinct.router import router as instinct_router

from pocketpaw.instinct.models import ActionTrigger
from pocketpaw.instinct.store import InstinctStore

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(
        self, user_id: str = "approver-1", workspace_id: str = "w1", role: str = "admin"
    ) -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role=role)]


def _make_ctx(workspace_id: str | None, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_growth_app(workspace_id: str, *, role: str = "admin", user_id: str = "u1") -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(growth_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id, user_id)

    # G-4 / F3 — the growth routes carry real RBAC guards now, so the app has
    # to supply a user the guard can resolve. ``role`` lets a test drive an
    # under-privileged caller (member vs admin).
    user = _FakeUser(user_id, workspace_id, role=role)
    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


def _build_instinct_app(user: _FakeUser) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(instinct_router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


class FakePool:
    """Records ``enqueue_job`` calls; optionally raises to simulate Redis down."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._raises = raises

    async def enqueue_job(self, job_name: str, *args: Any, **kwargs: Any) -> None:
        if self._raises is not None:
            raise self._raises
        self.calls.append((job_name, args, kwargs))


@pytest.fixture
def gate_store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """One shared InstinctStore behind every seam that resolves a store."""
    store = InstinctStore(tmp_path / "growth_gate.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


@pytest.fixture
def pool(monkeypatch) -> FakePool:
    fake = FakePool()

    async def _pool() -> FakePool:
        return fake

    monkeypatch.setattr(growth_executor, "_get_pool", _pool)
    return fake


@pytest.fixture
def authorized(monkeypatch) -> None:
    """The proposer still holds ``growth.manage`` (mirrors the ship tests)."""

    async def _ok(_workspace_id: str, _user_id: str) -> bool:
        return True

    monkeypatch.setattr(growth_executor, "_proposer_still_authorized", _ok)


@pytest.fixture(autouse=True)
def _clear_locks():
    """The executor's per-action locks are module state — reset between tests."""
    growth_executor._LOCKS.clear()
    yield
    growth_executor._LOCKS.clear()


@pytest.fixture(autouse=True)
def _enterprise_plan(monkeypatch):
    """The instinct router is plan-gated; pretend enterprise everywhere."""
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))


@pytest_asyncio.fixture
async def w1(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_growth_app("w1"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any) -> AsyncClient:
    transport = ASGITransport(app=_build_growth_app("w2"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def tray_w1() -> AsyncClient:
    """Instinct client for a w1 admin approver."""
    transport = ASGITransport(app=_build_instinct_app(_FakeUser("approver-1", "w1")))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


async def _drafted_prospect(client: AsyncClient, **overrides: Any) -> tuple[dict, dict]:
    """Create a prospect + one email draft; returns (prospect, draft)."""
    payload = {
        "name": "Sam Founder",
        "company": "Acme Dental",
        "domain": "acme-dental.com",
        "source": "manual",
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/growth/prospects", json=payload)
    assert resp.status_code == 200, resp.text
    prospect = resp.json()
    resp = await client.post(
        f"/api/v1/growth/prospects/{prospect['id']}/drafts",
        json={
            "channel": "email",
            "subject": "Quick idea for Acme Dental",
            "body": "Saw your booking flow — here's a live demo.",
        },
    )
    assert resp.status_code == 200, resp.text
    return prospect, resp.json()


async def _propose(client: AsyncClient, draft_id: str) -> dict:
    resp = await client.post(f"/api/v1/growth/drafts/{draft_id}/propose")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _draft_status(client: AsyncClient, draft_id: str) -> str:
    resp = await client.get("/api/v1/growth/drafts")
    assert resp.status_code == 200
    for draft in resp.json():
        if draft["id"] == draft_id:
            return draft["status"]
    raise AssertionError(f"draft {draft_id} not found")


# ---------------------------------------------------------------------------
# Propose — the blob + the flip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_files_gated_proposal_and_flips_draft(w1, gate_store):
    prospect, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])

    assert body["proposal_id"]
    assert body["draft"]["status"] == "proposed"
    assert await _draft_status(w1, draft["id"]) == "proposed"

    action = await gate_store.get_action(body["proposal_id"])
    assert action is not None
    status = getattr(action.status, "value", action.status)
    assert str(status) == "pending"
    blob = action.parameters["_growth_send"]
    assert blob["kind"] == "growth_send"
    assert blob["workspace_id"] == "w1"
    assert blob["draft_id"] == draft["id"]
    assert blob["prospect_id"] == prospect["id"]
    assert blob["channel"] == "email"
    assert blob["prospect_name"] == "Sam Founder"
    assert blob["prospect_company"] == "Acme Dental"
    assert blob["preview"] == {
        "subject": "Quick idea for Acme Dental",
        "body": "Saw your booking flow — here's a live demo.",
    }


@pytest.mark.asyncio
async def test_repropose_is_422_and_files_no_second_proposal(w1, gate_store):
    _, draft = await _drafted_prospect(w1)
    await _propose(w1, draft["id"])

    resp = await w1.post(f"/api/v1/growth/drafts/{draft['id']}/propose")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "draft.illegal_transition"

    actions = await gate_store.list_actions(limit=50)
    assert len(actions) == 1


@pytest.mark.asyncio
async def test_propose_on_foreign_draft_is_404(w1, w2, gate_store):
    """Tenancy on the propose route itself: w2 cannot propose w1's draft."""
    _, draft = await _drafted_prospect(w1)
    resp = await w2.post(f"/api/v1/growth/drafts/{draft['id']}/propose")
    assert resp.status_code == 404
    assert await _draft_status(w1, draft["id"]) == "draft"
    assert await gate_store.list_actions(limit=50) == []


# ---------------------------------------------------------------------------
# Gate integrity — the public route can NEVER approve or mark sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_status_route_cannot_approve(w1, gate_store):
    """The gate owns proposed→approved: a direct transition via the public
    status route is refused even though the edge is legal per the table."""
    _, draft = await _drafted_prospect(w1)
    await _propose(w1, draft["id"])

    resp = await w1.post(f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": "approved"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "draft.gate_required"
    assert await _draft_status(w1, draft["id"]) == "proposed"


@pytest.mark.asyncio
async def test_public_status_route_cannot_mark_sent(w1, tray_w1, gate_store, pool, authorized):
    """Even a legitimately gate-approved draft cannot be flipped to ``sent``
    over HTTP — that edge belongs to the dispatch worker (G-5/G-6)."""
    _, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])
    resp = await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
    assert resp.status_code == 200, resp.text
    assert await _draft_status(w1, draft["id"]) == "approved"

    resp = await w1.post(f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": "sent"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "draft.gate_required"
    assert await _draft_status(w1, draft["id"]) == "approved"


@pytest.mark.asyncio
async def test_dispatch_stub_sends_and_marks_nothing(w1, tray_w1, gate_store, pool, authorized):
    """G-4's job body is a STUB: running it flips no status and touches no
    provider — the draft stays ``approved`` until G-5/G-6 implement delivery."""
    from pocketpaw_ee.cloud.growth import worker as growth_worker

    _, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])
    await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
    assert pool.calls, "dispatch job was not enqueued"

    job_name, args, _kwargs = pool.calls[0]
    await growth_worker.dispatch({}, *args)  # run the stub the worker would run

    assert await _draft_status(w1, draft["id"]) == "approved"  # NOT sent


# ---------------------------------------------------------------------------
# Approve — single AND bulk: draft→approved + job enqueued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_approve_flips_draft_and_enqueues(w1, tray_w1, gate_store, pool, authorized):
    _, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])

    resp = await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"]["status"] == "approved"

    assert await _draft_status(w1, draft["id"]) == "approved"
    assert pool.calls == [
        ("growth.dispatch", (draft["id"], "email"), {"_queue_name": "growth"}),
    ]
    action = await gate_store.get_action(body["proposal_id"])
    assert str(getattr(action.status, "value", action.status)) == "executed"


@pytest.mark.asyncio
async def test_bulk_approve_flips_drafts_and_enqueues(w1, tray_w1, gate_store, pool, authorized):
    _, draft_a = await _drafted_prospect(w1)
    _, draft_b = await _drafted_prospect(w1, domain="beta.io", company="Beta")
    prop_a = await _propose(w1, draft_a["id"])
    prop_b = await _propose(w1, draft_b["id"])

    resp = await tray_w1.post(
        "/instinct/actions/bulk-approve",
        json={"ids": [prop_a["proposal_id"], prop_b["proposal_id"]]},
    )
    assert resp.status_code == 200, resp.text

    assert await _draft_status(w1, draft_a["id"]) == "approved"
    assert await _draft_status(w1, draft_b["id"]) == "approved"
    assert sorted(args[0] for _name, args, _kw in pool.calls) == sorted(
        [draft_a["id"], draft_b["id"]]
    )
    for job_name, args, kwargs in pool.calls:
        assert job_name == "growth.dispatch"
        assert args[1] == "email"
        assert kwargs == {"_queue_name": "growth"}


# ---------------------------------------------------------------------------
# Reject — single AND bulk: draft→rejected, nothing enqueued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_flips_draft_rejected(w1, tray_w1, gate_store, pool, authorized):
    _, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])

    resp = await tray_w1.post(
        f"/instinct/actions/{body['proposal_id']}/reject", json={"reason": "tone is off"}
    )
    assert resp.status_code == 200, resp.text

    assert await _draft_status(w1, draft["id"]) == "rejected"
    assert pool.calls == []  # the executor never ran — nothing queued, nothing sends


@pytest.mark.asyncio
async def test_bulk_reject_flips_drafts_rejected(w1, tray_w1, gate_store, pool, authorized):
    _, draft_a = await _drafted_prospect(w1)
    _, draft_b = await _drafted_prospect(w1, domain="beta.io", company="Beta")
    prop_a = await _propose(w1, draft_a["id"])
    prop_b = await _propose(w1, draft_b["id"])

    resp = await tray_w1.post(
        "/instinct/actions/bulk-reject",
        json={"ids": [prop_a["proposal_id"], prop_b["proposal_id"]], "reason": "batch nope"},
    )
    assert resp.status_code == 200, resp.text

    assert await _draft_status(w1, draft_a["id"]) == "rejected"
    assert await _draft_status(w1, draft_b["id"]) == "rejected"
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Cross-tenant — _assert_growth_workspace 403s BEFORE any mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_single_approve_is_403(w1, w2, tray_w1, gate_store, pool, authorized):
    """A w1 approver cannot approve a w2 growth send — 403 from
    ``_assert_growth_workspace``, draft and Action untouched, nothing queued."""
    _, draft = await _drafted_prospect(w2)
    body = await _propose(w2, draft["id"])

    resp = await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"

    assert await _draft_status(w2, draft["id"]) == "proposed"
    action = await gate_store.get_action(body["proposal_id"])
    assert str(getattr(action.status, "value", action.status)) == "pending"
    assert pool.calls == []


@pytest.mark.asyncio
async def test_cross_tenant_bulk_approve_and_reject_are_403(
    w1, w2, tray_w1, gate_store, pool, authorized
):
    """Bulk paths: one foreign item fails the whole batch (a partial bulk that
    silently dropped it would hide the escalation attempt)."""
    _, own_draft = await _drafted_prospect(w1)
    _, foreign_draft = await _drafted_prospect(w2)
    own = await _propose(w1, own_draft["id"])
    foreign = await _propose(w2, foreign_draft["id"])
    ids = [own["proposal_id"], foreign["proposal_id"]]

    resp = await tray_w1.post("/instinct/actions/bulk-approve", json={"ids": ids})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"

    resp = await tray_w1.post("/instinct/actions/bulk-reject", json={"ids": ids, "reason": "no"})
    assert resp.status_code == 403

    assert await _draft_status(w1, own_draft["id"]) == "proposed"
    assert await _draft_status(w2, foreign_draft["id"]) == "proposed"
    assert pool.calls == []


@pytest.mark.asyncio
async def test_cross_tenant_single_reject_is_403(w1, w2, tray_w1, gate_store, pool, authorized):
    _, draft = await _drafted_prospect(w2)
    body = await _propose(w2, draft["id"])

    resp = await tray_w1.post(
        f"/instinct/actions/{body['proposal_id']}/reject", json={"reason": "no"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"
    assert await _draft_status(w2, draft["id"]) == "proposed"


# ---------------------------------------------------------------------------
# Failure paths — enqueue failure + demoted proposer fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_failure_marks_action_failed(
    w1, tray_w1, gate_store, authorized, monkeypatch
):
    """Redis down at enqueue → ``store.mark_failed(error=...)``; the approve
    response itself never breaks (best-effort contract)."""
    broken = FakePool(raises=ConnectionError("redis down"))

    async def _pool() -> FakePool:
        return broken

    monkeypatch.setattr(growth_executor, "_get_pool", _pool)

    _, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])

    resp = await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
    assert resp.status_code == 200, resp.text  # never break the approve response

    action = await gate_store.get_action(body["proposal_id"])
    assert str(getattr(action.status, "value", action.status)) == "failed"
    assert "enqueue failed" in (action.error or "")
    # The approval stands on the draft; the failure is recorded on the Action.
    assert await _draft_status(w1, draft["id"]) == "approved"


@pytest.mark.asyncio
async def test_demoted_proposer_fails_closed_and_enqueues_nothing(
    w1, tray_w1, gate_store, pool, monkeypatch
):
    """The proposer lost ``growth.manage`` while the send sat in the tray —
    the approved send must NOT dispatch (execute-time RBAC re-check)."""

    async def _denied(_workspace_id: str, _user_id: str) -> bool:
        return False

    monkeypatch.setattr(growth_executor, "_proposer_still_authorized", _denied)

    _, draft = await _drafted_prospect(w1)
    body = await _propose(w1, draft["id"])

    resp = await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
    assert resp.status_code == 200

    assert pool.calls == []
    action = await gate_store.get_action(body["proposal_id"])
    assert str(getattr(action.status, "value", action.status)) == "failed"
    assert "no longer authorized" in (action.error or "")
    # The RBAC guard runs BEFORE the draft flip — the draft never approved.
    assert await _draft_status(w1, draft["id"]) == "proposed"


# ---------------------------------------------------------------------------
# F1 — approve-with-edits cannot rewrite a gated blob
# ---------------------------------------------------------------------------


class TestApproveWithEditsCannotRewriteGatedBlob:
    """The tenancy sweep runs on the PRE-edit Action while the executor reads
    the POST-edit blob, so a wholesale ``parameters`` replacement during approve
    would let an approver who had already cleared the gate for their OWN
    workspace re-point the blob at another tenant's draft. Identity fields are
    pinned back from the stored proposal; content stays editable."""

    @pytest.mark.asyncio
    async def test_cross_tenant_edit_cannot_repoint_the_send(
        self, w1, w2, tray_w1, gate_store, pool, authorized
    ):
        _, own_draft = await _drafted_prospect(w1)
        _, victim_draft = await _drafted_prospect(w2)
        own = await _propose(w1, own_draft["id"])
        victim = await _propose(w2, victim_draft["id"])

        # The attacker approves their OWN (w1) proposal, but rewrites the blob
        # to carry w2's tenancy + w2's draft.
        stolen = dict(
            (await gate_store.get_action(victim["proposal_id"])).parameters["_growth_send"]
        )

        resp = await tray_w1.post(
            f"/instinct/actions/{own['proposal_id']}/approve",
            json={"parameters": {"_growth_send": stolen}},
        )
        assert resp.status_code == 200, resp.text

        # The identity fields were pinned back — the dispatch went to the
        # attacker's OWN draft, and the victim's draft never moved.
        assert pool.calls == [
            ("growth.dispatch", (own_draft["id"], "email"), {"_queue_name": "growth"}),
        ]
        assert await _draft_status(w1, own_draft["id"]) == "approved"
        assert await _draft_status(w2, victim_draft["id"]) == "proposed"

        blob = (await gate_store.get_action(own["proposal_id"])).parameters["_growth_send"]
        assert blob["workspace_id"] == "w1"
        assert blob["draft_id"] == own_draft["id"]

    @pytest.mark.asyncio
    async def test_edit_cannot_forge_the_proposer(self, w1, tray_w1, gate_store, pool, monkeypatch):
        """``requested_by`` is an identity field: an approver cannot swap in
        someone else's id to satisfy the executor's RBAC re-check."""
        asked: list[str] = []

        async def _spy(workspace_id: str, user_id: str) -> bool:
            asked.append(user_id)
            return True

        monkeypatch.setattr(growth_executor, "_proposer_still_authorized", _spy)

        _, draft = await _drafted_prospect(w1)
        body = await _propose(w1, draft["id"])
        blob = dict((await gate_store.get_action(body["proposal_id"])).parameters["_growth_send"])
        blob["requested_by"] = "someone-else"

        resp = await tray_w1.post(
            f"/instinct/actions/{body['proposal_id']}/approve",
            json={"parameters": {"_growth_send": blob}},
        )
        assert resp.status_code == 200, resp.text
        assert "someone-else" not in asked
        assert asked == ["u1"]

    @pytest.mark.asyncio
    async def test_content_edits_survive_the_pinning(
        self, w1, tray_w1, gate_store, pool, authorized
    ):
        """Only identity is pinned — the legitimate approve-with-edits flow
        (the mandates plan resolution filters a ``_belt_plan``'s tasks this
        way) must keep working."""
        _, draft = await _drafted_prospect(w1)
        body = await _propose(w1, draft["id"])
        blob = dict((await gate_store.get_action(body["proposal_id"])).parameters["_growth_send"])
        blob["preview"] = {"subject": "tightened", "body": "tightened copy"}

        resp = await tray_w1.post(
            f"/instinct/actions/{body['proposal_id']}/approve",
            json={"parameters": {"_growth_send": blob}},
        )
        assert resp.status_code == 200, resp.text

        after = (await gate_store.get_action(body["proposal_id"])).parameters["_growth_send"]
        assert after["preview"] == {"subject": "tightened", "body": "tightened copy"}
        assert after["draft_id"] == draft["id"]  # identity untouched

    @pytest.mark.asyncio
    async def test_smuggling_a_gated_blob_onto_a_plain_action_is_dropped(
        self, w1, tray_w1, gate_store, pool, authorized
    ):
        """The mirror case: a PLAIN pending Action whose approve call tries to
        ADD a gated blob. An edit may never mint a dispatch out of an
        innocuous Tray card, so the reserved key is dropped entirely."""
        _, draft = await _drafted_prospect(w1)
        body = await _propose(w1, draft["id"])
        stolen = dict((await gate_store.get_action(body["proposal_id"])).parameters["_growth_send"])

        plain = await gate_store.propose(
            pocket_id="w1",
            title="looks harmless",
            description="",
            recommendation="",
            trigger=ActionTrigger(type="agent", source="u1", reason="test"),
            parameters={"note": "nothing to see"},
            workspace_id="w1",
        )

        resp = await tray_w1.post(
            f"/instinct/actions/{plain.id}/approve",
            json={"parameters": {"_growth_send": stolen, "note": "still nothing"}},
        )
        assert resp.status_code == 200, resp.text

        assert pool.calls == [], "an edit must not mint a dispatch"
        after = await gate_store.get_action(plain.id)
        assert "_growth_send" not in after.parameters
        assert await _draft_status(w1, draft["id"]) == "proposed"

    @pytest.mark.asyncio
    async def test_edit_cannot_delete_the_gated_blob(
        self, w1, tray_w1, gate_store, pool, authorized
    ):
        """Dropping the blob from the edit must not silently change the
        Action's kind — the stored blob is restored."""
        _, draft = await _drafted_prospect(w1)
        body = await _propose(w1, draft["id"])

        resp = await tray_w1.post(
            f"/instinct/actions/{body['proposal_id']}/approve",
            json={"parameters": {"note": "blob removed"}},
        )
        assert resp.status_code == 200, resp.text

        after = (await gate_store.get_action(body["proposal_id"])).parameters
        assert after["_growth_send"]["draft_id"] == draft["id"]

    @pytest.mark.asyncio
    async def test_plain_parameters_edit_still_works(self, w1, tray_w1, gate_store):
        """An ordinary Action keeps its approve-with-edits behaviour untouched
        (the correction flow depends on it)."""
        plain = await gate_store.propose(
            pocket_id="w1",
            title="ordinary action",
            description="",
            recommendation="",
            trigger=ActionTrigger(type="agent", source="u1", reason="test"),
            parameters={"quantity": 30},
            workspace_id="w1",
        )

        resp = await tray_w1.post(
            f"/instinct/actions/{plain.id}/approve",
            json={"parameters": {"quantity": 45}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"]["status"] == "approved"
        after = await gate_store.get_action(plain.id)
        assert after.parameters == {"quantity": 45}


# ---------------------------------------------------------------------------
# F2 — the generic propose route cannot mint a gated blob, and the executor
#      does not trust a blob-supplied proposer
# ---------------------------------------------------------------------------


def _forged_blob(**overrides: Any) -> dict[str, Any]:
    blob: dict[str, Any] = {
        "kind": "growth_send",
        "schema": 1,
        "workspace_id": "w1",
        "draft_id": "some-draft",
        "prospect_id": "some-prospect",
        "channel": "email",
        "prospect_name": "Victim",
        "prospect_company": "Victim Co",
        "preview": {"subject": "s", "body": "b"},
        "idempotency_key": "k",
        "requested_by": "admin-1",
        "summary": "forged",
        "correlation_id": None,
        "proposed_event_id": None,
        "outcome": None,
    }
    blob.update(overrides)
    return blob


class TestGenericProposeRouteRefusesGatedBlobs:
    @pytest.mark.asyncio
    async def test_forged_growth_send_is_refused(self, tray_w1, gate_store):
        """A member (or anyone) filing a Tray card whose parameters carry a
        ``_growth_send`` blob would get an executor dispatch on approve. The
        route refuses the key outright."""
        resp = await tray_w1.post(
            "/instinct/actions",
            json={
                "pocket_id": "w1",
                "title": "Review Q3 numbers",
                "trigger": {"type": "agent", "source": "u1", "reason": "test"},
                "parameters": {"_growth_send": _forged_blob()},
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "instinct.reserved_parameter_key"
        assert await gate_store.list_actions(limit=50) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key", ["_ship_action", "_admin_action", "_pocket_write", "_artifact_change"]
    )
    async def test_every_reserved_kind_is_refused(self, tray_w1, gate_store, key):
        """The guard is generic — growth is not a special case."""
        resp = await tray_w1.post(
            "/instinct/actions",
            json={
                "pocket_id": "w1",
                "title": "innocuous",
                "trigger": {"type": "agent", "source": "u1", "reason": "test"},
                "parameters": {key: {"workspace_id": "w1"}},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "instinct.reserved_parameter_key"
        assert await gate_store.list_actions(limit=50) == []

    @pytest.mark.asyncio
    async def test_plain_parameters_still_propose(self, tray_w1, gate_store):
        resp = await tray_w1.post(
            "/instinct/actions",
            json={
                "pocket_id": "w1",
                "title": "ordinary",
                "trigger": {"type": "agent", "source": "u1", "reason": "test"},
                "parameters": {"quantity": 30, "_triage": {"rule_flagged": False}},
            },
        )
        assert resp.status_code == 201, resp.text
        assert len(await gate_store.list_actions(limit=50)) == 1


class _FakeAction:
    """The minimal Action surface the growth executor reads (ship's shape)."""

    def __init__(self, blob: dict[str, Any], *, trigger_source: str, status: str = "approved"):
        self.id = "act-forged"
        self.parameters = {"_growth_send": blob}
        self.status = status
        self.trigger = ActionTrigger(type="agent", source=trigger_source, reason="test")


class TestExecutorIgnoresForgedProposer:
    """The blob's ``requested_by`` is data inside ``parameters``. Trusting it for
    the authorization re-check made the guard self-referential — whoever wrote
    the blob also chose whose role gets checked."""

    @pytest.mark.asyncio
    async def test_forged_requested_by_never_reaches_the_rbac_check(
        self, gate_store, pool, monkeypatch
    ):
        asked: list[str] = []

        async def _spy(workspace_id: str, user_id: str) -> bool:
            asked.append(user_id)
            return True

        monkeypatch.setattr(growth_executor, "_proposer_still_authorized", _spy)

        # Filed by member-9, but the blob names admin-1 as the proposer.
        action = _FakeAction(_forged_blob(requested_by="admin-1"), trigger_source="member-9")
        await growth_executor.execute_approved_growth_send(action)

        assert "admin-1" not in asked, "the forged proposer was used for the RBAC re-check"
        assert pool.calls == [], "a tampered blob must not dispatch"

    @pytest.mark.asyncio
    async def test_untriggered_action_fails_closed(self, gate_store, pool, monkeypatch):
        """No resolvable trigger source → no trustworthy proposer → no send."""
        checked: list[str] = []

        async def _spy(workspace_id: str, user_id: str) -> bool:
            checked.append(user_id)
            return True

        monkeypatch.setattr(growth_executor, "_proposer_still_authorized", _spy)
        action = _FakeAction(_forged_blob(), trigger_source="")

        await growth_executor.execute_approved_growth_send(action)

        assert checked == []
        assert pool.calls == []

    @pytest.mark.asyncio
    async def test_consistent_proposer_still_dispatches(
        self, w1, tray_w1, gate_store, pool, authorized
    ):
        """The honest path is unaffected: the propose helper writes the same id
        into the trigger and the blob, so the send dispatches normally."""
        _, draft = await _drafted_prospect(w1)
        body = await _propose(w1, draft["id"])
        action = await gate_store.get_action(body["proposal_id"])
        assert action.trigger.source == action.parameters["_growth_send"]["requested_by"]

        resp = await tray_w1.post(f"/instinct/actions/{body['proposal_id']}/approve")
        assert resp.status_code == 200
        assert len(pool.calls) == 1


# ---------------------------------------------------------------------------
# F3 — the growth routes enforce RBAC
# ---------------------------------------------------------------------------


class TestGrowthRouteRbac:
    """``require_license`` alone let any authenticated member of any workspace
    hit every /growth route. Reads and authoring writes are MEMBER; the
    OUTBOUND propose verb is ADMIN — the tier ``growth.executor`` re-checks at
    dispatch, so a member-filed proposal would always fail closed at approve."""

    @pytest_asyncio.fixture
    async def member(self, mongo_db: Any) -> AsyncClient:
        transport = ASGITransport(app=_build_growth_app("w1", role="member"))
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            yield client

    @pytest.mark.asyncio
    async def test_member_cannot_propose_a_send(self, w1, member, gate_store):
        """The outbound verb is ADMIN-only."""
        _, draft = await _drafted_prospect(w1)

        resp = await member.post(f"/api/v1/growth/drafts/{draft['id']}/propose")

        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "workspace.insufficient_role"
        assert await _draft_status(w1, draft["id"]) == "draft"
        assert await gate_store.list_actions(limit=50) == []

    @pytest.mark.asyncio
    async def test_member_can_read_and_author(self, member):
        """Staging outreach copy stays ordinary team work."""
        prospect, draft = await _drafted_prospect(member)
        assert prospect["id"] and draft["status"] == "draft"
        assert (await member.get("/api/v1/growth/prospects")).status_code == 200
        assert (await member.get("/api/v1/growth/drafts")).status_code == 200
        resp = await member.post(
            f"/api/v1/growth/drafts/{draft['id']}/status", json={"status": "proposed"}
        )
        assert resp.status_code == 200

    def test_every_route_carries_a_guard(self):
        """Pins the WIRING, not one route: a /growth route added later without
        an RBAC guard fails here rather than shipping open (F3 was exactly that
        — the actions were registered but no route enforced them)."""
        from pocketpaw_ee.cloud.growth.router import router as _growth_router

        expected = {
            ("GET", "/growth/prospects"): "growth.read",
            ("GET", "/growth/prospects/facets"): "growth.read",
            ("GET", "/growth/prospects/{prospect_id}"): "growth.read",
            ("GET", "/growth/drafts"): "growth.read",
            ("POST", "/growth/prospects"): "growth.write",
            ("POST", "/growth/prospects/bulk"): "growth.write",
            ("POST", "/growth/prospects/{prospect_id}/drafts"): "growth.write",
            ("PATCH", "/growth/prospects/{prospect_id}"): "growth.write",
            ("POST", "/growth/drafts/{draft_id}/status"): "growth.write",
            # The outbound verb sits at the ADMIN tier the executor re-checks.
            ("POST", "/growth/drafts/{draft_id}/propose"): "growth.manage",
            # G-8's manual LinkedIn surface. mark-sent is an OUTBOUND verb —
            # same ADMIN tier as propose — and it walks the gate seam, since
            # ``sent`` is gate-owned.
            ("GET", "/growth/linkedin/queue"): "growth.read",
            ("POST", "/growth/linkedin/{draft_id}/mark-sent"): "growth.manage",
        }

        seen: dict[tuple[str, str], str] = {}
        for route in _growth_router.routes:
            names = [
                getattr(dep.dependency, "__name__", "")
                for dep in getattr(route, "dependencies", [])
            ]
            guards = [n for n in names if n.startswith("require_action_growth_")]
            for method in route.methods - {"HEAD", "OPTIONS"}:
                assert guards, f"{method} {route.path} has no growth RBAC guard"
                seen[(method, route.path)] = guards[0].replace("require_action_growth_", "growth.")

        assert seen == expected
