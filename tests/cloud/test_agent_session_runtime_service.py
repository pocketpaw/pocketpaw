# tests/cloud/test_agent_session_runtime_service.py
# Created: 2026-06-30 (feat/session-supervisor SS-3) — proves the durable
# (workspace, session_id, agent_id) -> cli_session_id resume mapping over a real
# Beanie query path (mongomock-motor — no live mongod). Covers the four
# done-when cases: turn-1 persist + lookup, upsert/backfill (no duplicate row,
# the unique index holds), tenancy isolation (a foreign workspace reads None),
# and an absent key reading None. Uses the shared ``mongo_db`` fixture which
# init_beanie's ALL_DOCUMENTS (now including AgentSessionRuntimeDoc).

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.agent_sessions import runtime_service
from pocketpaw_ee.cloud.models.agent_session_runtime import AgentSessionRuntimeDoc

WS = "ws-A"
SESSION = "sess-1"
AGENT = "agent-7"


async def test_persist_then_lookup(mongo_db) -> None:  # noqa: ARG001 — forces Beanie init
    """Turn-1 persist: set then get returns the native id (+ project_key kept)."""
    await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "cli-abc", project_key="proj-1")
    assert await runtime_service.get_cli_session_id(WS, SESSION, AGENT) == "cli-abc"

    # project_key was persisted alongside the id.
    row = await AgentSessionRuntimeDoc.find_one(
        AgentSessionRuntimeDoc.workspace == WS,
        AgentSessionRuntimeDoc.session_id == SESSION,
        AgentSessionRuntimeDoc.agent_id == AGENT,
    )
    assert row is not None
    assert row.project_key == "proj-1"


async def test_upsert_backfill_updates_in_place(mongo_db) -> None:  # noqa: ARG001
    """Calling set again for the same key UPDATES the id — no duplicate row."""
    await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "cli-old")
    await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "cli-new")

    assert await runtime_service.get_cli_session_id(WS, SESSION, AGENT) == "cli-new"

    # The unique (workspace, session_id, agent_id) index holds: exactly one row.
    rows = await AgentSessionRuntimeDoc.find(
        AgentSessionRuntimeDoc.workspace == WS,
        AgentSessionRuntimeDoc.session_id == SESSION,
        AgentSessionRuntimeDoc.agent_id == AGENT,
    ).to_list()
    assert len(rows) == 1


async def test_upsert_preserves_project_key_when_omitted(mongo_db) -> None:  # noqa: ARG001
    """A backfill that omits project_key leaves the prior one intact."""
    await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "cli-1", project_key="proj-1")
    # Resume only re-stamps the id; it has no project_key to pass.
    await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "cli-2")

    row = await AgentSessionRuntimeDoc.find_one(
        AgentSessionRuntimeDoc.workspace == WS,
        AgentSessionRuntimeDoc.session_id == SESSION,
        AgentSessionRuntimeDoc.agent_id == AGENT,
    )
    assert row is not None
    assert row.cli_session_id == "cli-2"
    assert row.project_key == "proj-1"


async def test_tenancy_isolation(mongo_db) -> None:  # noqa: ARG001
    """A different workspace can't read tenant A's mapping."""
    await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "cli-abc")

    assert await runtime_service.get_cli_session_id("ws-B", SESSION, AGENT) is None
    # A still sees its own row.
    assert await runtime_service.get_cli_session_id(WS, SESSION, AGENT) == "cli-abc"


async def test_absent_key_returns_none(mongo_db) -> None:  # noqa: ARG001
    """An unknown (ws, session, agent) resolves to None, not an error."""
    assert await runtime_service.get_cli_session_id(WS, "no-such-session", AGENT) is None
    # A row for a different agent in the same session doesn't bleed across.
    await runtime_service.set_cli_session_id(WS, SESSION, "agent-1", "cli-1")
    assert await runtime_service.get_cli_session_id(WS, SESSION, "agent-2") is None


async def test_empty_inputs_rejected(mongo_db) -> None:  # noqa: ARG001
    """Inputs are validated at entry — empty scope-key components raise."""
    with pytest.raises(ValidationError):
        await runtime_service.set_cli_session_id("", SESSION, AGENT, "cli-1")
    with pytest.raises(ValidationError):
        await runtime_service.set_cli_session_id(WS, SESSION, AGENT, "")
    with pytest.raises(ValidationError):
        await runtime_service.get_cli_session_id(WS, "", AGENT)
