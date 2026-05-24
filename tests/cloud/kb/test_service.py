# tests/cloud/kb/test_service.py — Workspace KB scope listing service.
#
# Updated: 2026-05-24 — Added ``test_list_scopes_caps_concurrent_probes``
# proving the new ``_PROBE_CONCURRENCY`` semaphore actually bounds the
# parallel kb-go probe fan-out. Seeds 50 fake candidate scopes via
# monkeypatch and observes peak in-flight probe count under a lock.
#
# Created: 2026-05-24 — Covers ``kb.service.list_scopes`` with an
# in-memory kb_list shim. Four guarantees:
#   1. A workspace with one pocket + one agent + workspace-level
#      articles returns all three scopes when the shim says they
#      all have content.
#   2. Scopes the shim reports as empty are filtered out.
#   3. Cross-workspace stamping (calling with a workspace id the user
#      doesn't belong to) returns no pocket scopes — the underlying
#      ``pockets_service.list_pockets`` filter already enforces tenant
#      isolation, and the service must not synthesize anything past it.
#   4. A blank ``workspace_id`` short-circuits to ``[]`` — we never
#      probe an unkeyed scope.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest
from pocketpaw_ee.cloud.kb import service as kb_service
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

WORKSPACE = "ws-kb-scopes"
OTHER_WORKSPACE = "ws-kb-other"


async def _seed_user(email: str = "owner@kb.test", workspace: str = WORKSPACE) -> str:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="KB Owner",
        active_workspace=workspace,
    )
    await doc.insert()
    return str(doc.id)


def _ctx(user_id: str, workspace_id: str) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r-kb",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _kb_list_with(populated: set[str]):
    """Build a kb_list shim where the given scope strings carry one row.

    Mirrors the kb-go subprocess contract: returns a list of rows or
    ``[]``. One row per scope is enough — ``list_scopes`` only checks
    "is the list non-empty" before keeping the scope.
    """

    def _shim(scope: str) -> list[dict]:
        if scope in populated:
            return [{"id": f"art-{scope}", "title": "x"}]
        return []

    return _shim


async def test_list_scopes_returns_workspace_pocket_agent_scopes() -> None:
    """Happy path — every populated scope appears, in workspace→pocket→agent order."""
    user_id = await _seed_user()
    pocket = await pockets_service.create(WORKSPACE, user_id, CreatePocketRequest(name="Sales"))
    agent = await agents_service.create(
        _ctx(user_id, WORKSPACE),
        WORKSPACE,
        CreateAgentRequest(name="Helper", slug="helper-kb"),
    )

    pocket_scope = f"pocket:{pocket['_id']}"
    agent_scope = f"agent:{agent.id}"
    workspace_scope = f"workspace:{WORKSPACE}"

    scopes = await kb_service.list_scopes(
        WORKSPACE,
        user_id,
        kb_list=_kb_list_with({workspace_scope, pocket_scope, agent_scope}),
    )

    assert scopes == [workspace_scope, pocket_scope, agent_scope]


async def test_list_scopes_filters_out_empty_scopes() -> None:
    """A candidate scope with no kb articles must not appear in the result."""
    user_id = await _seed_user("owner-filter@kb.test")
    pocket = await pockets_service.create(WORKSPACE, user_id, CreatePocketRequest(name="Drafts"))
    # Create an agent but leave its scope empty — should NOT appear.
    await agents_service.create(
        _ctx(user_id, WORKSPACE),
        WORKSPACE,
        CreateAgentRequest(name="Idle", slug="idle-kb"),
    )

    pocket_scope = f"pocket:{pocket['_id']}"
    workspace_scope = f"workspace:{WORKSPACE}"

    # Only workspace + pocket populated — agent scope is silent.
    scopes = await kb_service.list_scopes(
        WORKSPACE,
        user_id,
        kb_list=_kb_list_with({workspace_scope, pocket_scope}),
    )

    assert workspace_scope in scopes
    assert pocket_scope in scopes
    assert all(not s.startswith("agent:") for s in scopes)


async def test_list_scopes_blocks_cross_workspace_pocket_reads() -> None:
    """Stamping the wrong workspace id returns no pocket/agent scopes.

    The same user owns a pocket in ``OTHER_WORKSPACE``; calling
    ``list_scopes`` with ``WORKSPACE`` must not surface that pocket's
    scope. The pockets service's ``workspace`` filter is what enforces
    this; the test pins that the kb service relies on it correctly.
    """
    user_id = await _seed_user("owner-cross@kb.test")
    other_pocket = await pockets_service.create(
        OTHER_WORKSPACE, user_id, CreatePocketRequest(name="Other-Workspace Pocket")
    )

    # The shim says BOTH the other pocket's scope and our workspace
    # scope have content. If the service forgets the workspace filter
    # it'll happily include the cross-workspace pocket.
    other_pocket_scope = f"pocket:{other_pocket['_id']}"
    scopes = await kb_service.list_scopes(
        WORKSPACE,
        user_id,
        kb_list=_kb_list_with({f"workspace:{WORKSPACE}", other_pocket_scope}),
    )

    assert other_pocket_scope not in scopes
    # The workspace scope itself is fine — we asked for WORKSPACE's.
    assert f"workspace:{WORKSPACE}" in scopes


async def test_list_scopes_blank_workspace_returns_empty() -> None:
    """Blank ``workspace_id`` short-circuits to ``[]`` without probing."""

    probed: list[str] = []

    def _spy(scope: str) -> list[dict]:
        probed.append(scope)
        return [{"id": "x"}]

    scopes = await kb_service.list_scopes("", "u-1", kb_list=_spy)

    assert scopes == []
    assert probed == []  # never reached the probe


async def test_list_scopes_caps_concurrent_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe fan-out must stay bounded by ``_PROBE_CONCURRENCY``.

    Reviewer-flagged P1: unbounded ``asyncio.gather`` on a 100-pocket +
    50-agent workspace would spawn 151 concurrent kb-go subprocesses
    even though the default-executor thread pool can only service ~32
    at a time. The fix is a semaphore around ``_probe_scope``; this
    test seeds 50 fake candidate scopes, has each probe announce its
    in-flight status under a lock, and asserts the observed peak never
    exceeds the cap.
    """
    candidates = [f"pocket:p-{i}" for i in range(50)]

    async def _fake_candidates(_workspace_id: str, _user_id: str) -> list[str]:
        return candidates

    monkeypatch.setattr(kb_service, "_candidate_scopes", _fake_candidates)

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _probe(scope: str) -> list[dict]:
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Yield so other gated tasks can attempt the semaphore — without
        # an await the whole coroutine completes synchronously and the
        # peak collapses to 1.
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return [{"id": f"art-{scope}"}]

    scopes = await kb_service.list_scopes("ws-cap", "u-cap", kb_list=_probe)

    assert len(scopes) == len(candidates), "every populated probe should keep its scope"
    assert peak <= kb_service._PROBE_CONCURRENCY, (
        f"observed peak in-flight {peak} exceeded cap {kb_service._PROBE_CONCURRENCY}"
    )
    # Cap actually engaged — if peak is 1 we proved nothing about the
    # semaphore. With 50 candidates and a 10ms sleep, the loop must
    # have saturated the semaphore at least once.
    assert peak == kb_service._PROBE_CONCURRENCY, (
        f"probe fan-out never reached the cap (peak={peak}); "
        "the test is no longer exercising the gate"
    )
