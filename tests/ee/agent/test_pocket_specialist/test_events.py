"""Pocket-specialist status events — names and emit helper."""

from unittest.mock import AsyncMock, patch

import pytest

from ee.agent.pocket_specialist.events import (
    SpecialistEvent,
    emit_specialist_event,
)


class TestSpecialistEventNames:
    def test_known_events(self):
        assert SpecialistEvent.START.value == "specialist:start"
        assert SpecialistEvent.LISTING.value == "specialist:listing"
        assert SpecialistEvent.DECIDED.value == "specialist:decided"
        assert SpecialistEvent.DRAFTING.value == "specialist:drafting"
        assert SpecialistEvent.VALIDATING.value == "specialist:validating"
        assert SpecialistEvent.REVISING.value == "specialist:revising"
        assert SpecialistEvent.PERSISTING.value == "specialist:persisting"
        assert SpecialistEvent.DONE.value == "specialist:done"


class TestEmitSpecialistEvent:
    @pytest.mark.asyncio
    async def test_emit_writes_to_bus(self):
        with patch(
            "ee.agent.pocket_specialist.events.event_bus",
        ) as mock_bus:
            mock_bus.emit = AsyncMock()
            await emit_specialist_event(SpecialistEvent.LISTING, {})
            mock_bus.emit.assert_awaited_once()
            event_name, data = mock_bus.emit.await_args.args
            assert event_name == "specialist:listing"
            assert data == {}

    @pytest.mark.asyncio
    async def test_emit_includes_payload(self):
        with patch(
            "ee.agent.pocket_specialist.events.event_bus",
        ) as mock_bus:
            mock_bus.emit = AsyncMock()
            await emit_specialist_event(SpecialistEvent.DECIDED, {"action": "create"})
            event_name, data = mock_bus.emit.await_args.args
            assert event_name == "specialist:decided"
            assert data == {"action": "create"}

    @pytest.mark.asyncio
    async def test_emit_swallows_bus_failure(self, caplog):
        with patch(
            "ee.agent.pocket_specialist.events.event_bus",
        ) as mock_bus:
            mock_bus.emit = AsyncMock(side_effect=RuntimeError("bus down"))
            await emit_specialist_event(SpecialistEvent.START, {"brief": "x"})
            assert "specialist event emit failed" in caplog.text.lower()
