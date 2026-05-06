"""Test that ``AgentLoop._get_router`` rebuilds when ``agent_backend``
changes in settings — without this the user can flip backends in the
dashboard and the running loop keeps the previously-cached one until
the process restarts."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.loop import AgentLoop


def _make_loop(agent_id: str | None = None) -> AgentLoop:
    """Construct an AgentLoop with the absolute minimum mocked deps.

    ``AgentLoop.__init__`` reads ``settings`` / ``message_bus`` /
    ``memory_manager`` from module-level singletons rather than ctor
    args, so we patch the getters during construction.
    """
    settings = MagicMock()
    settings.agent_backend = "claude_agent_sdk"
    settings.fallback_backends = []
    settings.max_concurrent_conversations = 4

    with (
        patch("pocketpaw.agents.loop.get_settings", return_value=settings),
        patch("pocketpaw.agents.loop.get_message_bus", return_value=MagicMock()),
        patch(
            "pocketpaw.agents.loop.get_memory_manager", return_value=MagicMock()
        ),
        patch("pocketpaw.agents.loop.AgentContextBuilder", return_value=MagicMock()),
    ):
        return AgentLoop(agent_id=agent_id)


def _stub_router(backend_name: str) -> MagicMock:
    router = MagicMock()
    router._active_backend_name = backend_name
    return router


def test_default_loop_rebuilds_router_when_backend_changes():
    """Default (no agent_id) loop must rebuild when settings on disk
    flip the backend."""
    loop = _make_loop(agent_id=None)
    loop._router = _stub_router("claude_agent_sdk")
    original_router = loop._router

    fresh_settings = MagicMock(agent_backend="codex_cli", fallback_backends=[])

    with (
        patch(
            "pocketpaw.agents.loop.Settings.load", return_value=fresh_settings
        ),
        patch(
            "pocketpaw.agents.loop.AgentRouter",
            return_value=_stub_router("codex_cli"),
        ) as mock_router_cls,
    ):
        new_router = loop._get_router()

    assert new_router is not original_router
    mock_router_cls.assert_called_once_with(fresh_settings)
    # The loop should also pick up the fresh Settings so downstream
    # reads (system prompt build, etc.) see the new config.
    assert loop.settings is fresh_settings


def test_default_loop_keeps_router_when_backend_unchanged():
    """Re-reading settings every call shouldn't churn the router when
    the backend hasn't changed — that would tear down a healthy
    Codex / Claude SDK subprocess for nothing."""
    loop = _make_loop(agent_id=None)
    loop._router = _stub_router("codex_cli")
    original_router = loop._router

    same = MagicMock(agent_backend="codex_cli", fallback_backends=[])

    with (
        patch("pocketpaw.agents.loop.Settings.load", return_value=same),
        patch("pocketpaw.agents.loop.AgentRouter") as mock_router_cls,
    ):
        result = loop._get_router()

    assert result is original_router
    mock_router_cls.assert_not_called()


def test_per_agent_loop_does_not_reload_settings():
    """Per-agent loops carry agent-specific overrides on
    ``self.settings``. Reloading from disk would clobber them."""
    loop = _make_loop(agent_id="agent-123")
    loop._router = _stub_router("claude_agent_sdk")
    original_router = loop._router

    with (
        patch("pocketpaw.agents.loop.Settings.load") as mock_load,
        patch("pocketpaw.agents.loop.AgentRouter") as mock_router_cls,
    ):
        result = loop._get_router()

    assert result is original_router
    mock_load.assert_not_called()
    mock_router_cls.assert_not_called()


def test_first_call_for_default_loop_loads_fresh_settings():
    """No cached router yet → use fresh settings, not the captured
    ``self.settings`` snapshot from loop construction."""
    loop = _make_loop(agent_id=None)
    assert loop._router is None

    fresh_settings = MagicMock(agent_backend="codex_cli", fallback_backends=[])

    with (
        patch(
            "pocketpaw.agents.loop.Settings.load", return_value=fresh_settings
        ),
        patch(
            "pocketpaw.agents.loop.AgentRouter",
            return_value=_stub_router("codex_cli"),
        ) as mock_router_cls,
    ):
        loop._get_router()

    mock_router_cls.assert_called_once_with(fresh_settings)


@pytest.mark.asyncio
async def test_old_router_stop_is_scheduled_on_swap():
    """When swapping backends, the old router's ``stop()`` should be
    fire-and-forget scheduled so its subprocess (Codex, etc.) gets a
    chance to clean up — but the swap itself shouldn't block on it."""
    import asyncio

    loop = _make_loop(agent_id=None)
    old_router = MagicMock()
    old_router._active_backend_name = "codex_cli"
    stop_called = asyncio.Event()

    async def fake_stop():
        stop_called.set()

    old_router.stop = fake_stop
    loop._router = old_router

    fresh = MagicMock(agent_backend="claude_agent_sdk", fallback_backends=[])
    with (
        patch("pocketpaw.agents.loop.Settings.load", return_value=fresh),
        patch(
            "pocketpaw.agents.loop.AgentRouter",
            return_value=_stub_router("claude_agent_sdk"),
        ),
    ):
        loop._get_router()

    # Yield once so the scheduled task runs.
    await asyncio.sleep(0)
    assert stop_called.is_set()
