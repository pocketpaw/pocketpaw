"""run_specialist end-to-end with a mocked backend."""

from typing import Any
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


def _persist_factory_stub(pocket: dict[str, Any]):
    """Build a side_effect for ``make_persist_pocket_tool`` that simulates
    the tool running successfully: when the runtime constructs the tool,
    the stub immediately writes ``pocket`` into the supplied capture dict.

    The returned MagicMock stands in for the StructuredTool — the mocked
    backend never invokes it, so its surface is irrelevant.
    """

    def _stub(
        *,
        workspace_id: str,
        user_id: str,
        capture: dict[str, Any] | None = None,
        max_validation_retries: int = 3,
    ):
        if capture is not None:
            capture["pocket"] = pocket
        return MagicMock()

    return _stub


def _validate_factory_stub(warnings: list[str] | None = None):
    """Build a side_effect for ``make_validate_spec_tool`` that simulates
    the validator producing the given warnings (default: none)."""

    def _stub(*, capture: dict[str, Any] | None = None):
        if capture is not None:
            capture["last_validation"] = {
                "ok": not warnings,
                "warnings": list(warnings or []),
            }
        return MagicMock()

    return _stub


class TestRunSpecialistHappyPath:
    @pytest.mark.asyncio
    async def test_returns_persisted_pocket_via_tool_capture(self):
        captured_pocket = {"id": "p-new", "name": "Repos", "color": "#0ea5e9"}
        # Real backends only emit {"name": tool_name} in tool_result metadata
        # - they never include the tool's return dict. The runtime now relies
        # on a side-channel capture dict that the persist tool factory mutates
        # when its tool runs. We simulate that mutation via a factory stub.
        events = [
            AgentEvent(type="tool_use", content="", metadata={"name": "list_pockets"}),
            AgentEvent(type="tool_result", content="[]", metadata={"name": "list_pockets"}),
            AgentEvent(type="tool_use", content="", metadata={"name": "persist_pocket"}),
            AgentEvent(type="tool_result", content="", metadata={"name": "persist_pocket"}),
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
                "ee.agent.pocket_specialist.runtime.make_persist_pocket_tool",
                side_effect=_persist_factory_stub(captured_pocket),
            ),
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

    @pytest.mark.asyncio
    async def test_hints_target_pocket_id_locks_update_path(self):
        captured_pocket = {"id": "p-1", "name": "Updated"}
        events = [
            AgentEvent(type="tool_use", content="", metadata={"name": "persist_pocket"}),
            AgentEvent(type="tool_result", content="", metadata={"name": "persist_pocket"}),
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
                "ee.agent.pocket_specialist.runtime.make_persist_pocket_tool",
                side_effect=_persist_factory_stub(captured_pocket),
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


class TestRunSpecialistFailureMode:
    """When the LLM run never produces a persisted pocket (errored mid-
    run, exhausted validation retries, transport 400, etc.), the
    specialist must return ok=false with an error and NOT ship a
    placeholder. User-facing rule: empty canvases captioned "auto-created
    from a brief" are worse than "I couldn't build that, try again"."""

    @pytest.mark.asyncio
    async def test_no_pocket_returns_failure_result(self):
        events = [
            AgentEvent(type="message", content="I'm done."),
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
        ):
            out = await run_specialist(
                PocketSpecialistCreateInput(brief="A vague brief here for testing"),
                workspace_id="ws-1",
                user_id="user-A",
                settings=Settings(),
            )

        assert out.ok is False
        assert out.action == "failed"
        assert out.pocket is None
        assert out.error is not None
        assert "Specialist did not produce" in out.error or "retries" in out.error
