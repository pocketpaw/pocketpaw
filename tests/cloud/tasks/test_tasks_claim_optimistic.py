# test_tasks_claim_optimistic.py — race condition coverage for claim.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend.
#   Exercises the optimistic single-writer claim contract: if two
#   agents (or two retries from the same agent) call ``claim_task`` on
#   the same ``proposed`` task at the same time, exactly one wins and
#   the other gets ``{ok: False, reason}``.
"""Optimistic claim contention tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.tasks import service as tasks_service
from ee.cloud.tasks.dto import AssigneeDTO, ClaimTaskRequest, CreateTaskRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="creator",
        workspace_id="w1",
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _create_proposed_task(agent_id: str = "agent-a") -> str:
    created = await tasks_service.agent_create_task(
        _ctx(),
        CreateTaskRequest(
            title="claim-me",
            assignee=AssigneeDTO(kind="agent", id=agent_id, name="A"),
        ),
    )
    assert created.status == "proposed"
    return created.id


# ---------------------------------------------------------------------------
# Race conditions
# ---------------------------------------------------------------------------


async def test_two_simultaneous_claims_only_one_succeeds() -> None:
    """Exactly one claim wins when two concurrent calls hit the same
    proposed task. mongomock-motor serialises individual operations on
    the same collection so this is a deterministic check of the
    matcher-condition logic, not of true Mongo-level concurrency."""

    task_id = await _create_proposed_task()
    body = ClaimTaskRequest(agent_id="agent-a")

    results = await asyncio.gather(
        tasks_service.agent_claim_task(_ctx(), task_id, body),
        tasks_service.agent_claim_task(_ctx(), task_id, body),
    )

    wins = [r for r in results if r["ok"]]
    losses = [r for r in results if not r["ok"]]

    assert len(wins) == 1
    assert len(losses) == 1
    assert losses[0]["reason"] == "already_claimed"
    assert wins[0]["task"]["status"] == "in_progress"


async def test_second_claim_after_first_succeeds_reports_already_claimed() -> None:
    task_id = await _create_proposed_task()
    body = ClaimTaskRequest(agent_id="agent-a")

    first = await tasks_service.agent_claim_task(_ctx(), task_id, body)
    assert first["ok"] is True

    second = await tasks_service.agent_claim_task(_ctx(), task_id, body)
    assert second["ok"] is False
    assert second["reason"] == "already_claimed"


async def test_claim_by_different_agent_id_rejected() -> None:
    task_id = await _create_proposed_task(agent_id="agent-a")
    body = ClaimTaskRequest(agent_id="agent-b")

    result = await tasks_service.agent_claim_task(_ctx(), task_id, body)
    assert result["ok"] is False
    assert result["reason"] == "not_assigned_to_agent"


async def test_claim_nonexistent_returns_not_found() -> None:
    # Valid ObjectId shape, no corresponding doc.
    result = await tasks_service.agent_claim_task(
        _ctx(),
        "507f1f77bcf86cd799439011",
        ClaimTaskRequest(agent_id="agent-a"),
    )
    assert result["ok"] is False
    assert result["reason"] == "not_found"


async def test_claim_malformed_id_returns_not_found() -> None:
    result = await tasks_service.agent_claim_task(
        _ctx(), "garbage", ClaimTaskRequest(agent_id="agent-a")
    )
    assert result["ok"] is False
    assert result["reason"] == "not_found"


async def test_claim_cross_workspace_returns_not_found() -> None:
    task_id = await _create_proposed_task()
    cross_ctx = RequestContext(
        user_id="x",
        workspace_id="w2",
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )
    result = await tasks_service.agent_claim_task(
        cross_ctx, task_id, ClaimTaskRequest(agent_id="agent-a")
    )
    assert result["ok"] is False
    assert result["reason"] == "not_found"


async def test_many_concurrent_claims_only_one_wins() -> None:
    """Stress: 10 concurrent claim attempts. Exactly one must win."""

    task_id = await _create_proposed_task()
    body = ClaimTaskRequest(agent_id="agent-a")

    results = await asyncio.gather(
        *[tasks_service.agent_claim_task(_ctx(), task_id, body) for _ in range(10)]
    )

    wins = [r for r in results if r["ok"]]
    assert len(wins) == 1
    for r in results:
        if not r["ok"]:
            assert r["reason"] == "already_claimed"
