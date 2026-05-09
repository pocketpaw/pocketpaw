"""run_specialist end-to-end with a mocked backend."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
    run_specialist,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings


def _stream(events: list[AgentEvent]):
    """Build an async generator that yields the given events."""

    async def gen(*args, **kwargs):
        for e in events:
            yield e

    return gen


class TestRunSpecialistHappyPath:
    @pytest.mark.asyncio
    async def test_returns_persisted_pocket_via_tool_capture(self):
        captured_pocket = {"id": "p-new", "name": "Repos", "color": "#0ea5e9"}
        events = [
            AgentEvent(type="tool_use", content="", metadata={"name": "list_pockets"}),
            AgentEvent(type="tool_result", content="[]", metadata={"name": "list_pockets"}),
            AgentEvent(type="tool_use", content="", metadata={"name": "persist_pocket"}),
            AgentEvent(
                type="tool_result",
                content=str(captured_pocket),
                metadata={"name": "persist_pocket", "result": captured_pocket},
            ),
            AgentEvent(type="done", content=""),
        ]
        fake_backend = MagicMock()
        fake_backend.run = _stream(events)
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with (
            patch(
                "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
                return_value=fake_backend,
            ),
            patch(
                "ee.agent.pocket_specialist.runtime.emit_specialist_event",
                new=AsyncMock(),
            ) as mock_emit,
        ):
            out = await run_specialist(
                PocketSpecialistCreateInput(brief="Track my repos across repos foo, bar, baz"),
                workspace_id="ws-1",
                user_id="user-A",
                settings=Settings(),
            )

        assert out.ok is True
        assert out.action in ("created", "extended")
        assert out.pocket["id"] == "p-new"
        emitted = [c.args[0].value for c in mock_emit.await_args_list]
        assert emitted[0] == "specialist:start"
        assert emitted[-1] == "specialist:done"

    @pytest.mark.asyncio
    async def test_hints_target_pocket_id_locks_update_path(self):
        captured_pocket = {"id": "p-1", "name": "Updated"}
        events = [
            AgentEvent(type="tool_use", content="", metadata={"name": "persist_pocket"}),
            AgentEvent(
                type="tool_result",
                content="",
                metadata={"name": "persist_pocket", "result": captured_pocket},
            ),
            AgentEvent(type="done", content=""),
        ]
        fake_backend = MagicMock()
        fake_backend.run = _stream(events)
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with (
            patch(
                "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
                return_value=fake_backend,
            ),
            patch(
                "ee.agent.pocket_specialist.runtime.emit_specialist_event",
                new=AsyncMock(),
            ),
        ):
            out = await run_specialist(
                PocketSpecialistCreateInput(
                    brief="Update repos pocket - change colors",
                    hints=PocketSpecialistHints(target_pocket_id="p-1"),
                ),
                workspace_id="ws-1",
                user_id="user-A",
                settings=Settings(),
            )

        assert out.action == "extended"
        assert out.pocket["id"] == "p-1"
