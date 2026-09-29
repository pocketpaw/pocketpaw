# tests/test_channel_turn_context.py
# The channel path (AgentLoop -> AgentRouter) sends its per-message prompt
# layers (memory recall, KB hits, studio flow context) as ``turn_context`` to a
# backend whose ``run`` declares it, the same way AgentPool does on the cloud
# path. A warm Claude SDK client applies its system prompt only at connect, so
# without this the channel agent kept turn 1's recall forever. Backends that do
# not declare ``turn_context`` still get the one full system prompt.

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.router import AgentRouter
from pocketpaw.bus import Channel, InboundMessage
from pocketpaw.config import Settings
from pocketpaw.prompt import AssembledPrompt

pytestmark = pytest.mark.asyncio


def _info(name: str) -> BackendInfo:
    return BackendInfo(name=name, display_name=name, capabilities=Capability.STREAMING)


class _TakesTurnContext:
    seen: dict[str, Any] = {}

    @staticmethod
    def info() -> BackendInfo:
        return _info("takes_tc")

    def __init__(self, settings=None):  # noqa: ARG002
        pass

    async def run(  # noqa: ARG002
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        turn_context: str = "",
    ):
        type(self).seen = {"system_prompt": system_prompt, "turn_context": turn_context}
        yield AgentEvent(type="done", content="")


class _Plain:
    seen: dict[str, Any] = {}

    @staticmethod
    def info() -> BackendInfo:
        return _info("plain")

    def __init__(self, settings=None):  # noqa: ARG002
        pass

    async def run(  # noqa: ARG002
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ):
        type(self).seen = {"system_prompt": system_prompt}
        yield AgentEvent(type="done", content="")


def _register(monkeypatch, name: str, cls_name: str) -> None:
    from pocketpaw.agents import registry

    monkeypatch.setitem(
        registry._BACKEND_REGISTRY, name, ("tests.test_channel_turn_context", cls_name)
    )


async def test_router_splits_for_a_backend_that_takes_turn_context(monkeypatch):
    _register(monkeypatch, "takes_tc", "_TakesTurnContext")
    router = AgentRouter(Settings(agent_backend="takes_tc"))
    async for _ in router.run(
        "hi", system_prompt="STABLE\n\nRECALL", turn_split=("STABLE", "RECALL")
    ):
        pass
    assert _TakesTurnContext.seen == {"system_prompt": "STABLE", "turn_context": "RECALL"}


async def test_router_keeps_the_full_prompt_for_other_backends(monkeypatch):
    _register(monkeypatch, "plain", "_Plain")
    router = AgentRouter(Settings(agent_backend="plain"))
    async for _ in router.run(
        "hi", system_prompt="STABLE\n\nRECALL", turn_split=("STABLE", "RECALL")
    ):
        pass
    assert _Plain.seen == {"system_prompt": "STABLE\n\nRECALL"}


async def test_failover_chain_splits_per_harness(monkeypatch):
    _register(monkeypatch, "takes_tc", "_TakesTurnContext")
    router = AgentRouter(
        Settings(
            agent_backend="takes_tc",
            backend_failover_enabled=True,
            backend_failover_chain=["takes_tc"],
        )
    )
    async for _ in router.run_with_failover(
        "hi", system_prompt="STABLE\n\nRECALL", turn_split=("STABLE", "RECALL")
    ):
        pass
    assert _TakesTurnContext.seen == {"system_prompt": "STABLE", "turn_context": "RECALL"}


@patch("pocketpaw.agents.loop.get_message_bus")
@patch("pocketpaw.agents.loop.get_memory_manager")
@patch("pocketpaw.agents.loop.AgentContextBuilder")
@patch("pocketpaw.agents.loop.AgentRouter")
async def test_agent_loop_routes_per_message_layers_to_turn_split(
    mock_router_cls, mock_builder_cls, mock_get_memory, mock_get_bus
):
    from pocketpaw.agents.loop import AgentLoop
    from pocketpaw.bootstrap.protocol import BootstrapContext

    bus = MagicMock()
    bus.consume_inbound = AsyncMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_system = AsyncMock()
    mock_get_bus.return_value = bus

    memory = MagicMock()
    memory.add_to_session = AsyncMock()
    memory.get_compacted_history = AsyncMock(return_value=[])
    memory.resolve_session_key = AsyncMock(side_effect=lambda k: k)
    memory._store.get_session = AsyncMock(return_value=[])
    mock_get_memory.return_value = memory

    calls: list[dict] = []

    async def capturing_run(
        message,
        *,
        system_prompt=None,
        history=None,
        session_key=None,
        system_prompt_digest="",
        turn_split=None,
    ):
        calls.append({"system_prompt": system_prompt, "turn_split": turn_split})
        yield AgentEvent(type="done", content="")

    router = MagicMock()
    router.run = capturing_run
    router.stop = AsyncMock()
    mock_router_cls.return_value = router

    bootstrap = MagicMock()
    bootstrap.get_context = AsyncMock(
        return_value=BootstrapContext(
            name="T", identity="I", soul="S", style="St", user_profile="U"
        )
    )
    builder = mock_builder_cls.return_value
    builder.bootstrap = bootstrap
    builder.assemble_system_prompt = AsyncMock(
        return_value=AssembledPrompt(
            text="IDENTITY\n\nRECALL: likes tea\n\nKB: wiki hit",
            stable_digest="d",
            layer_texts=(
                ("channel.identity", "IDENTITY"),
                ("channel.memory_context", "RECALL: likes tea"),
                ("channel.kb_context", "KB: wiki hit"),
            ),
        )
    )

    with patch("pocketpaw.agents.loop.get_settings") as get_settings:
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        settings.max_concurrent_conversations = 5
        get_settings.return_value = settings
        with patch("pocketpaw.agents.loop.Settings") as settings_cls:
            settings_cls.load.return_value = settings
            loop = AgentLoop()
            await loop._process_message(
                InboundMessage(
                    channel=Channel.CLI,
                    sender_id="u",
                    chat_id="c",
                    content="hello",
                    metadata={"flow_context": {"flow_id": "F1"}},
                )
            )

    assert len(calls) == 1
    stable, turn = calls[0]["turn_split"]
    assert "RECALL" not in stable and "KB:" not in stable and "F1" not in stable
    assert "IDENTITY" in stable
    assert "RECALL: likes tea" in turn and "KB: wiki hit" in turn
    assert "ACTIVE FLOW ID: F1" in turn
    # The full prompt still carries everything for a backend without turn_context.
    assert "RECALL: likes tea" in calls[0]["system_prompt"]
    assert "ACTIVE FLOW ID: F1" in calls[0]["system_prompt"]
