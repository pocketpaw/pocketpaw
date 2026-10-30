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
    is exercised separately in ``test_ensures_db_when_missing``.

    ``_ensure_cloud_db_initialized`` is just an alias for
    ``_ensure_cloud_runtime_initialized``, but ``_run_cloud_handler``
    calls the canonical name directly — so the patch has to target
    that, not the alias, or the boot logic still runs and fails on the
    missing ``POCKETPAW_MONGO_URI`` env var.
    """
    with (
        patch("pocketpaw.tools.cli._ensure_cloud_runtime_initialized", new=AsyncMock()),
        patch("pocketpaw.tools.cli._ensure_cloud_db_initialized", new=AsyncMock()),
    ):
        yield


@pytest.mark.asyncio
async def test_cloud_get_pocket_calls_agent_context():
    from pocketpaw.tools.cli import _cloud_get_pocket

    with patch(
        "pocketpaw_ee.cloud.pockets.agent_context.fetch_pocket_for_agent",
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
        "pocketpaw_ee.cloud.pockets.agent_context.fetch_pocket_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_fetch:
        await _cloud_get_pocket({})

    mock_fetch.assert_awaited_once_with("from-env")


@pytest.mark.asyncio
async def test_cloud_add_widget_passes_widget_dict():
    from pocketpaw.tools.cli import _cloud_add_widget

    widget = {"name": "Revenue", "type": "metric", "data": {"value": 100}}
    with patch(
        "pocketpaw_ee.cloud.pockets.agent_context.add_widget_for_agent",
        new=AsyncMock(return_value={"ok": True, "pocket": {}}),
    ) as mock_add:
        await _cloud_add_widget({"pocket_id": "p1", "widget": widget})

    mock_add.assert_awaited_once_with("p1", widget)


@pytest.mark.asyncio
async def test_cloud_update_widget_passes_fields():
    from pocketpaw.tools.cli import _cloud_update_widget

    with patch(
        "pocketpaw_ee.cloud.pockets.agent_context.update_widget_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_upd:
        await _cloud_update_widget({"pocket_id": "p1", "widget_id": "w1", "fields": {"name": "X"}})

    mock_upd.assert_awaited_once_with("p1", "w1", {"name": "X"})


@pytest.mark.asyncio
async def test_cloud_remove_widget_targets_id():
    from pocketpaw.tools.cli import _cloud_remove_widget

    with patch(
        "pocketpaw_ee.cloud.pockets.agent_context.remove_widget_for_agent",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_rm:
        await _cloud_remove_widget({"pocket_id": "p1", "widget_id": "w1"})

    mock_rm.assert_awaited_once_with("p1", "w1")


def test_legacy_pocket_mutation_handlers_are_dropped():
    """``cloud_create_pocket`` / ``cloud_update_pocket`` were the
    calling-agent equivalents of the specialist tool. They are now
    filtered out of the CLI dispatcher so subprocess agents (codex_cli,
    opencode, gemini_cli, copilot_sdk) can't bypass the specialist —
    matching the read-only-only surface the ``pocketpaw_pocket`` MCP
    server exposes to the claude_agent_sdk backend."""
    from pocketpaw.tools import cli as cli_module

    assert "cloud_create_pocket" not in cli_module._CLOUD_HANDLERS
    assert "cloud_update_pocket" not in cli_module._CLOUD_HANDLERS
    # The specialist tool itself must remain available.
    assert "cloud_pocket_specialist_create" in cli_module._CLOUD_HANDLERS
    # Read-only + live-widget editing flows are kept.
    assert "cloud_list_pockets" in cli_module._CLOUD_HANDLERS
    assert "cloud_get_pocket" in cli_module._CLOUD_HANDLERS
    assert "cloud_add_widget" in cli_module._CLOUD_HANDLERS
    assert "cloud_update_widget" in cli_module._CLOUD_HANDLERS
    assert "cloud_remove_widget" in cli_module._CLOUD_HANDLERS
    # The Python-level helpers are gone too.
    assert not hasattr(cli_module, "_cloud_create_pocket")
    assert not hasattr(cli_module, "_cloud_update_pocket")


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
