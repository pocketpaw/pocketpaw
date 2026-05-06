"""Tests for the cloud_* commands in ``pocketpaw.tools.cli``.

These commands are how subprocess agents (Codex, gemini-cli, opencode)
do cloud-side pocket reads/writes — they shell out to the CLI which
calls into ``ee.cloud.pockets.agent_context``. We mock the agent_context
helpers so the tests never touch Mongo.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _stub_db_init():
    """Pretend Beanie is already initialized — the CLI's lazy boot path
    is exercised separately in ``test_ensures_db_when_missing``."""
    with patch(
        "pocketpaw.tools.cli._ensure_cloud_db_initialized", new=AsyncMock()
    ):
        yield


@pytest.mark.asyncio
async def test_cloud_get_pocket_calls_agent_context():
    from pocketpaw.tools.cli import _cloud_get_pocket

    with patch(
        "ee.cloud.pockets.agent_context.fetch_pocket_for_agent",
        new=AsyncMock(return_value={"ok": True, "pocket": {"_id": "p1"}}),
    ) as mock_fetch:
        result = await _cloud_get_pocket({"pocket_id": "p1"})

    mock_fetch.assert_awaited_once_with("p1")
    assert result == {"ok": True, "pocket": {"_id": "p1"}}


@pytest.mark.asyncio
async def test_cloud_get_pocket_falls_back_to_env(monkeypatch):
    """If the JSON body omits ``pocket_id``, the CLI uses ``POCKETPAW_POCKET_ID``
    so the agent can drop it from common-case calls."""
    from pocketpaw.tools.cli import _cloud_get_pocket

    monkeypatch.setenv("POCKETPAW_POCKET_ID", "from-env")
    with patch(
        "ee.cloud.pockets.agent_context.fetch_pocket_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_fetch:
        await _cloud_get_pocket({})

    mock_fetch.assert_awaited_once_with("from-env")


@pytest.mark.asyncio
async def test_cloud_add_widget_passes_widget_dict():
    from pocketpaw.tools.cli import _cloud_add_widget

    widget = {"name": "Revenue", "type": "metric", "data": {"value": 100}}
    with patch(
        "ee.cloud.pockets.agent_context.add_widget_for_agent",
        new=AsyncMock(return_value={"ok": True, "pocket": {}}),
    ) as mock_add:
        await _cloud_add_widget({"pocket_id": "p1", "widget": widget})

    mock_add.assert_awaited_once_with("p1", widget)


@pytest.mark.asyncio
async def test_cloud_update_widget_passes_fields():
    from pocketpaw.tools.cli import _cloud_update_widget

    with patch(
        "ee.cloud.pockets.agent_context.update_widget_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_upd:
        await _cloud_update_widget(
            {"pocket_id": "p1", "widget_id": "w1", "fields": {"name": "X"}}
        )

    mock_upd.assert_awaited_once_with("p1", "w1", {"name": "X"})


@pytest.mark.asyncio
async def test_cloud_remove_widget_targets_id():
    from pocketpaw.tools.cli import _cloud_remove_widget

    with patch(
        "ee.cloud.pockets.agent_context.remove_widget_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_rm:
        await _cloud_remove_widget({"pocket_id": "p1", "widget_id": "w1"})

    mock_rm.assert_awaited_once_with("p1", "w1")


@pytest.mark.asyncio
async def test_cloud_update_pocket_only_sends_provided_fields():
    """``update_pocket_for_agent`` accepts ``None`` for fields the caller
    didn't pass — make sure we don't backfill missing ones."""
    from pocketpaw.tools.cli import _cloud_update_pocket

    with patch(
        "ee.cloud.pockets.agent_context.update_pocket_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_upd:
        await _cloud_update_pocket({"pocket_id": "p1", "name": "Renamed"})

    mock_upd.assert_awaited_once_with(
        "p1",
        name="Renamed",
        description=None,
        icon=None,
        color=None,
        ripple_spec=None,
    )


@pytest.mark.asyncio
async def test_cloud_create_pocket_uses_env_identity(monkeypatch):
    from pocketpaw.tools.cli import _cloud_create_pocket

    monkeypatch.setenv("POCKETPAW_WORKSPACE_ID", "ws-1")
    monkeypatch.setenv("POCKETPAW_USER_ID", "u-1")
    fake_view = {"_id": "p-new"}
    with patch(
        "ee.cloud.pockets.service.agent_create",
        new=AsyncMock(return_value=(fake_view, "p-new", None)),
    ) as mock_create:
        result = await _cloud_create_pocket({"name": "My Pocket"})

    assert result == {"ok": True, "pocket": fake_view, "pocket_id": "p-new"}
    mock_create.assert_awaited_once()
    kwargs = mock_create.await_args.kwargs
    assert kwargs["workspace_id"] == "ws-1"
    assert kwargs["owner_id"] == "u-1"
    assert kwargs["name"] == "My Pocket"


@pytest.mark.asyncio
async def test_cloud_create_pocket_errors_when_identity_missing(monkeypatch):
    from pocketpaw.tools.cli import _cloud_create_pocket

    monkeypatch.delenv("POCKETPAW_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("POCKETPAW_USER_ID", raising=False)
    result = await _cloud_create_pocket({"name": "x"})
    assert result["ok"] is False
    assert "POCKETPAW_WORKSPACE_ID" in result["error"]


@pytest.mark.asyncio
async def test_run_cloud_handler_serializes_to_json_line():
    from pocketpaw.tools.cli import _run_cloud_handler

    async def handler(args):
        return {"ok": True, "echo": args}

    out = await _run_cloud_handler(handler, {"hello": "world"})
    parsed = json.loads(out)
    assert parsed == {"ok": True, "echo": {"hello": "world"}}


@pytest.mark.asyncio
async def test_run_cloud_handler_catches_exceptions():
    from pocketpaw.tools.cli import _run_cloud_handler

    async def handler(_):
        raise RuntimeError("boom")

    out = await _run_cloud_handler(handler, {})
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "RuntimeError" in parsed["error"]
    assert "boom" in parsed["error"]
