# Simulation Clock — Discrete tick-based time for synchronized multi-agent execution.
# Created: 2026-03-26
#
# Provides a lightweight clock that simulation PawKits can use for discrete
# time-stepped execution instead of wall-clock time. Agents all act on tick N,
# world updates, then tick N+1.
#
# See: https://github.com/pocketpaw/pocketpaw/issues/633

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TickSnapshot:
    """Immutable snapshot of world state at a specific tick.

    Attributes:
        tick: The tick number this snapshot was taken at.
        task_states: Mapping of task_id to status string at this tick.
        metadata: Arbitrary extra data captured at this tick.
    """

    tick: int
    task_states: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "tick": self.tick,
            "task_states": self.task_states,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TickSnapshot:
        """Create from dictionary."""
        return cls(
            tick=data.get("tick", 0),
            task_states=data.get("task_states", {}),
            metadata=data.get("metadata", {}),
        )


class SimulationClock:
    """Discrete tick-based clock for synchronized multi-agent simulation.

    All agents act on the same tick, the world updates, then the clock
    advances to the next tick. This replaces wall-clock time when you
    want deterministic, fast-forwarded simulation.

    Usage::

        clock = SimulationClock()
        while not done:
            # dispatch all ready tasks for this tick
            tick = await clock.advance()
            # world updates happen here

    Attributes:
        current_tick: The current tick number (starts at 0).
    """

    def __init__(self) -> None:
        self._current_tick: int = 0
        self._snapshots: list[TickSnapshot] = []
        self._tick_cond: asyncio.Condition = asyncio.Condition()

    @property
    def current_tick(self) -> int:
        """Return the current tick number."""
        return self._current_tick

    async def advance(self) -> int:
        """Advance one tick. Returns the new tick number."""
        self._current_tick += 1
        async with self._tick_cond:
            self._tick_cond.notify_all()
        logger.debug("SimulationClock advanced to tick %d", self._current_tick)
        return self._current_tick

    def elapsed(self) -> int:
        """Return ticks since start (same as current_tick)."""
        return self._current_tick

    def reset(self) -> None:
        """Reset the clock to tick 0 and clear all snapshots."""
        self._current_tick = 0
        self._snapshots.clear()
        logger.debug("SimulationClock reset")

    async def wait_for_tick(self, tick: int) -> None:
        """Block until the clock reaches the given tick.

        Args:
            tick: The tick number to wait for.
        """
        async with self._tick_cond:
            await self._tick_cond.wait_for(lambda: self._current_tick >= tick)

    def record_snapshot(self, snapshot: TickSnapshot) -> None:
        """Record a world-state snapshot for the current tick.

        Args:
            snapshot: The snapshot to record.
        """
        self._snapshots.append(snapshot)

    def get_snapshots(self) -> list[TickSnapshot]:
        """Return all recorded snapshots (for replay/analysis)."""
        return list(self._snapshots)

    def get_snapshot_at(self, tick: int) -> TickSnapshot | None:
        """Return the snapshot recorded at a specific tick, or None.

        Args:
            tick: The tick number to look up.
        """
        for snap in self._snapshots:
            if snap.tick == tick:
                return snap
        return None
