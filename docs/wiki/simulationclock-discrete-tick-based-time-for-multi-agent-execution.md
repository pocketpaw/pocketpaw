---
{
  "title": "SimulationClock: Discrete Tick-Based Time for Multi-Agent Execution",
  "summary": "SimulationClock provides a tick-based replacement for wall-clock time in multi-agent simulations, allowing all agents to act synchronously on the same tick before the world advances. TickSnapshot captures immutable world state at each tick for replay and analysis.",
  "concepts": [
    "SimulationClock",
    "TickSnapshot",
    "discrete time simulation",
    "multi-agent coordination",
    "asyncio.Condition",
    "tick-based execution",
    "world state",
    "immutable snapshot",
    "deterministic simulation",
    "PawKit"
  ],
  "categories": [
    "Deep Work",
    "Simulation"
  ],
  "source_docs": [
    "cd74a54cd086f862"
  ],
  "backlinks": null,
  "word_count": 485,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/deep_work/clock.py` introduces a discrete time model for PocketPaw's simulation PawKits — agent configurations that run deterministic, fast-forwarded scenarios rather than real-time tasks. Created 2026-03-26 in response to issue #633, it solves a fundamental coordination problem in multi-agent simulation: how do you ensure all agents act on the same world state before the world updates?

## The Core Problem

In wall-clock time, agents act at slightly different moments. Agent A might read world state at `t=1.001s`, Agent B at `t=1.003s`. If Agent A's action changes the world between those reads, Agent B is acting on stale data. For deterministic simulation — where you want reproducible outputs and the ability to fast-forward time — this is unacceptable.

`SimulationClock` solves this with a tick model: all agents act on tick N, the world updates, then `advance()` moves to tick N+1. No agent can read tick N+1 state until every agent has finished acting on tick N.

## SimulationClock

The clock maintains `_current_tick` (starts at 0) and an `asyncio.Condition` (`_tick_cond`). Key methods:

- **`advance()`** — increments `_current_tick` and notifies all waiters via `_tick_cond.notify_all()`. Returns the new tick number.
- **`wait_for_tick(tick)`** — blocks until `_current_tick >= tick`. Agents call this to synchronize before acting.
- **`elapsed()`** — alias for `current_tick`. Useful for simulation logic that reasons about how many ticks have passed.
- **`reset()`** — returns to tick 0 and clears all snapshots. Used between simulation runs.
- **`record_snapshot(snapshot)`** — stores a `TickSnapshot` for the current tick.
- **`get_snapshot_at(tick)`** — retrieves a specific tick's snapshot for replay analysis.

## TickSnapshot

`TickSnapshot` is a frozen dataclass (immutable after creation) that captures:

- **`tick`** — which tick this snapshot represents.
- **`task_states`** — mapping of `task_id` to status string at this tick (e.g., `{"task_1": "running", "task_2": "complete"}`).
- **`metadata`** — arbitrary extra data (agent outputs, world metrics, etc.).

Immutability is enforced by `@dataclass(frozen=True)`. This prevents accidental mutation of historical state — a snapshot of tick 5 should remain exactly what it was at tick 5, even as the simulation continues.

`to_dict()` and `from_dict()` support JSON serialization for persistence and network transfer.

## asyncio.Condition for Synchronization

`_tick_cond` is an `asyncio.Condition`, which wraps an asyncio lock and a notify mechanism. `wait_for_tick()` uses `await self._tick_cond.wait_for(lambda: self._current_tick >= tick)`, which releases the lock and suspends the coroutine until `advance()` calls `notify_all()`. This is the standard asyncio pattern for condition-variable synchronization without busy-waiting.

## Usage Pattern

```python
clock = SimulationClock()
while not done:
    # All agents await clock.wait_for_tick(current_tick + 1)
    # Orchestrator collects all agent actions
    tick = await clock.advance()
    snapshot = TickSnapshot(tick=tick, task_states=current_states)
    clock.record_snapshot(snapshot)
```

## Known Gaps

- There is no built-in mechanism to enforce that all agents have finished acting before `advance()` is called. The orchestrator must coordinate this externally — for example, using a barrier or collecting futures before advancing.
- `get_snapshot_at()` iterates the full snapshot list linearly. For long simulations with many ticks, a dict-based index would be more efficient.