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

from pocketpaw.instinct.store import InstinctStore

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "approver-1", workspace_id: str = "w1") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _make_ctx(workspace_id: str | None, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _build_growth_app(workspace_id: str) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(growth_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return _make_ctx(workspace_id)

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[require_license] = lambda: None
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
