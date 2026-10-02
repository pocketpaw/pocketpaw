# tests/ee/agent/test_connectors_mcp_server/test_audit_shape.py — pins the exact
# rows ``connectors._audit_connector_execute`` writes to both audit sinks, so the
# CN-5 move onto the shared ``_audit`` sink helpers changes no stored shape.
#
# Created: 2026-10-01 (refactor/canon-proposal-helpers, CN-5) — written against the
#   pre-refactor code first.

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pocketpaw_ee")


class _SpyLogger:
    def __init__(self) -> None:
        self.events: list = []

    def log(self, event) -> None:
        self.events.append(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
async def test_connector_audit_rows_keep_their_shape(monkeypatch, ok):
    from pocketpaw_ee.agent.mcp_servers.connectors import _audit_connector_execute

    from pocketpaw.security.audit import AuditSeverity

    spy = _SpyLogger()
    monkeypatch.setattr("pocketpaw.security.audit.get_audit_logger", lambda: spy)
    record = AsyncMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.audit.service.record", record)

    _audit_connector_execute(
        workspace_id="ws-1",
        user_id="u-1",
        pocket_id="pk-1",
        connector_name="gmail",
        action="list_messages",
        status="ok" if ok else "error",
        ok=ok,
        via_sense="email",
    )
    await asyncio.sleep(0)

    assert len(spy.events) == 1
    ev = spy.events[0]
    assert ev.severity == (AuditSeverity.INFO if ok else AuditSeverity.WARNING)
    assert (ev.actor, ev.action, ev.target, ev.status) == (
        "u-1",
        "connector.execute",
        "gmail",
        "ok" if ok else "error",
    )
    assert ev.context == {
        "category": "pocket_tool_run",
        "workspace_id": "ws-1",
        "connector_action": "list_messages",
        "pocket_id": "pk-1",
        "via_sense": "email",
    }

    record.assert_called_once_with(
        workspace_id="ws-1",
        actor_id="u-1",
        action="workspace.agent.tool_executed",
        target_type="connector",
        target_id="gmail.list_messages",
        metadata={
            "connector": "gmail",
            "connector_action": "list_messages",
            "pocket_id": "pk-1",
            "status": "ok" if ok else "error",
            "ok": ok,
            "via_sense": "email",
            "source": "chat_page",
        },
    )


@pytest.mark.asyncio
async def test_connector_audit_defaults_for_missing_identity(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers.connectors import _audit_connector_execute

    spy = _SpyLogger()
    monkeypatch.setattr("pocketpaw.security.audit.get_audit_logger", lambda: spy)
    record = AsyncMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.audit.service.record", record)

    _audit_connector_execute(
        workspace_id="ws-1",
        user_id=None,
        pocket_id=None,
        connector_name="gmail",
        action="send",
        status="pending_approval",
    )
    await asyncio.sleep(0)

    ev = spy.events[0]
    assert ev.actor == "agent"
    assert ev.context["pocket_id"] == "" and ev.context["via_sense"] == ""
    kwargs = record.call_args.kwargs
    assert kwargs["actor_id"] == "agent"
    assert kwargs["metadata"]["pocket_id"] == ""
    assert kwargs["metadata"]["via_sense"] == ""
    assert kwargs["metadata"]["ok"] is True
