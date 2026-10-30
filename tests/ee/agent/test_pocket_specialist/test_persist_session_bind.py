"""Regression test: persist_pocket must bind the active chat session to
the newly-created pocket atomically with creation.

Past bug: the specialist runs in an isolated backend and uses
``_agent_create`` directly, bypassing ``create_pocket_for_agent`` which
was the only place that called ``attach_pocket_to_session_doc``. The
fallback path (parsing the main agent's tool_result via
``_maybe_handle_specialist_response``) is backend-dependent and silently
fails when the main backend doesn't surface the tool result in the
exact expected JSON shape — so sessions stayed orphaned from their
pockets.

Fix: ``make_persist_pocket_tool._run`` reads
``current_session_mongo_id`` from the ContextVar set by
``agent_router.attach_agent_identity`` and calls
``attach_pocket_to_session_doc`` directly after a successful create.
The bind is atomic with creation and works for every parent backend
(claude_agent_sdk, deep_agents, codex_cli, etc.) since the in-process
MCP tool call inherits the parent stream's contextvars.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.agent.pocket_specialist.tools import make_persist_pocket_tool


@pytest.mark.asyncio
async def test_persist_pocket_binds_session_when_contextvar_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ContextVar is set → attach_pocket_to_session_doc is
    called with (session_mongo_id, user_id, new_pocket_id)."""
    from pocketpaw_ee.cloud.chat.agent_service import _active_session_mongo_id

    pocket_view: dict[str, Any] = {"id": "pid_abc", "name": "Demo"}
    new_pocket_id = "pid_abc"

    # Stub agent_create to return our fake pocket.
    fake_create = AsyncMock(return_value=(pocket_view, new_pocket_id, None))
    # Stub the validator manifest so we skip the network fetch.
    fake_manifest = AsyncMock(return_value=None)
    attach = AsyncMock(return_value=str(new_pocket_id))

    token = _active_session_mongo_id.set("session_mongo_xyz")
    try:
        with (
            patch("pocketpaw_ee.agent.pocket_specialist.tools._agent_create", fake_create),
            patch("pocketpaw_ee.agent.pocket_specialist.tools._get_manifest", fake_manifest),
            patch(
                "pocketpaw_ee.cloud.sessions.service.attach_pocket_to_session_doc",
                attach,
            ),
        ):
            tool = make_persist_pocket_tool(
                workspace_id="ws_1",
                user_id="user_1",
                capture={},
                max_validation_retries=0,  # don't gate on warnings here
            )
            result = await tool.coroutine(
                ripple_spec={"version": "1.0", "ui": {"type": "text"}},
                name="Demo",
            )
    finally:
        _active_session_mongo_id.reset(token)

    assert result == pocket_view
    fake_create.assert_awaited_once()
    attach.assert_awaited_once_with("session_mongo_xyz", "user_1", "pid_abc")


@pytest.mark.asyncio
async def test_persist_pocket_skips_bind_when_no_session_context() -> None:
    """When no chat session is active (ContextVar=None), bind is skipped
    silently — creation still succeeds."""
    pocket_view: dict[str, Any] = {"id": "pid_xyz", "name": "Standalone"}
    fake_create = AsyncMock(return_value=(pocket_view, "pid_xyz", None))
    fake_manifest = AsyncMock(return_value=None)
    attach = AsyncMock()

    with (
        patch("pocketpaw_ee.agent.pocket_specialist.tools._agent_create", fake_create),
        patch("pocketpaw_ee.agent.pocket_specialist.tools._get_manifest", fake_manifest),
        patch(
            "pocketpaw_ee.cloud.sessions.service.attach_pocket_to_session_doc",
            attach,
        ),
    ):
        tool = make_persist_pocket_tool(
            workspace_id="ws_1",
            user_id="user_1",
            capture={},
            max_validation_retries=0,
        )
        result = await tool.coroutine(
            ripple_spec={"version": "1.0", "ui": {"type": "text"}},
            name="Standalone",
        )

    assert result == pocket_view
    attach.assert_not_called()


@pytest.mark.asyncio
async def test_persist_pocket_survives_bind_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """attach_pocket_to_session_doc returning None (owner mismatch,
    missing doc) must NOT break pocket creation — the pocket already
    exists, that's the primary contract."""
    from pocketpaw_ee.cloud.chat.agent_service import _active_session_mongo_id

    pocket_view: dict[str, Any] = {"id": "pid_q", "name": "Q"}
    fake_create = AsyncMock(return_value=(pocket_view, "pid_q", None))
    fake_manifest = AsyncMock(return_value=None)
    # Simulate owner mismatch: service returns None
    attach = AsyncMock(return_value=None)
    capture: dict[str, Any] = {}

    token = _active_session_mongo_id.set("session_stale")
    try:
        with (
            patch("pocketpaw_ee.agent.pocket_specialist.tools._agent_create", fake_create),
            patch("pocketpaw_ee.agent.pocket_specialist.tools._get_manifest", fake_manifest),
            patch(
                "pocketpaw_ee.cloud.sessions.service.attach_pocket_to_session_doc",
                attach,
            ),
        ):
            tool = make_persist_pocket_tool(
                workspace_id="ws_1",
                user_id="user_1",
                capture=capture,
                max_validation_retries=0,
            )
            result = await tool.coroutine(
                ripple_spec={"version": "1.0", "ui": {"type": "text"}},
                name="Q",
            )
    finally:
        _active_session_mongo_id.reset(token)

    assert result == pocket_view  # creation still succeeded
    attach.assert_awaited_once()
    # Soft warning surfaced for the runtime to include in output
    assert any("session->pocket bind skipped" in w for w in capture.get("warnings", [])), capture
