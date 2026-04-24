---
{
  "title": "SimulationClock and Tick-Synchronized Scheduler Tests",
  "summary": "This test file covers `SimulationClock` (a deterministic discrete-time clock for agent simulation), `TickSnapshot` (tick-state serialization), `DependencyScheduler.run_tick_synchronized` (tick-driven task dispatch), and simulation tick metadata injection into task prompts. Tests validate clock advancement, snapshot immutability, scheduler dispatch order, and prompt metadata inclusion.",
  "concepts": [
    "SimulationClock",
    "TickSnapshot",
    "DependencyScheduler",
    "run_tick_synchronized",
    "discrete-time simulation",
    "tick-driven dispatch",
    "task metadata",
    "prompt injection",
    "snapshot serialization",
    "deep work",
    "MCTaskExecutor"
  ],
  "categories": [
    "testing",
    "simulation",
    "scheduler",
    "task execution",
    "test"
  ],
  "source_docs": [
    "e65f83d1105b3486"
  ],
  "backlinks": null,
  "word_count": 458,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_simulation_clock.py` (created 2026-03-26) tests the simulation and deep-work subsystems: `SimulationClock` and `TickSnapshot` from `pocketpaw.deep_work.clock`, and `DependencyScheduler` from `pocketpaw.deep_work.scheduler`. These components implement discrete-time simulation — a mode where agent tasks execute in lockstep ticks rather than real-time, enabling reproducible multi-agent coordination and scenario testing.

## SimulationClock

`TestSimulationClock` validates the core clock mechanics:

- **Initial state** — `current_tick == 0`, `elapsed() == 0`.
- **Advance** — each `await clock.advance()` returns the new tick value and increments `current_tick`.
- **Multiple advances** — tick increments monotonically; tested over five iterations.
- **Reset** — `clock.reset()` returns `current_tick` to 0 and clears all recorded snapshots.
- **Record and retrieve snapshots** — `record_snapshot(TickSnapshot(...))` accumulates state snapshots; `get_snapshots()` returns them in insertion order.
- **Get snapshot at tick** — `get_snapshot_at(tick=3)` returns the snapshot for that specific tick.
- **Snapshots return a copy** — mutating the returned list does not affect internal state. This is defensive: if a consumer appends to the returned list, the clock's internal record must not change.

```python
async def test_get_snapshots_returns_copy(self):
    # Appending to the returned list does not affect clock.get_snapshots()
```

## TickSnapshot Serialization

`TestTickSnapshot` covers `TickSnapshot.to_dict()` / `from_dict()`:

- **`to_dict()`** — converts to a plain dict with `tick` and `task_states`.
- **`from_dict()`** — reconstructs from a dict.
- **Round-trip** — `from_dict(snap.to_dict())` produces an equal snapshot.
- **`from_dict` defaults** — missing optional fields get default values rather than raising.

Serialization is needed because snapshots are stored to disk (for replay and audit) and transmitted between agents.

## Run Tick Synchronized

`TestRunTickSynchronized` tests `DependencyScheduler.run_tick_synchronized`:

- **Without clock** — raises an error if no `SimulationClock` is attached. This prevents silent non-determinism from running without a clock.
- **Empty project** — a project with no ready tasks yields no snapshots; the scheduler does not spin forever.
- **Single tick — dispatches all ready tasks** — tasks with no unresolved dependencies are dispatched in one tick; snapshots are recorded.
- **Tick metadata stamped on task** — when a task is dispatched during tick N, its metadata includes `simulation_tick=N`. This allows the task executor to include the tick number in the prompt, giving the LLM temporal context.

```python
async def test_tick_metadata_is_stamped_on_task(mock_manager, mock_executor, clock):
    # task.metadata["simulation_tick"] is set before executor.execute is called
```

## Simulation Tick in Prompt

`TestSimulationTickInPrompt` tests `MCTaskExecutor._build_task_prompt`:

- **With `simulation_tick` in metadata** — the tick number appears in the constructed prompt string.
- **Without `simulation_tick`** — the tick number is absent from the prompt, not replaced with a default or placeholder.

This matters for reproducibility: agents that receive the same prompt for tick 3 should produce the same output regardless of wall-clock time.

## Known Gaps

No `TODO` or `FIXME` markers. Tests do not cover `clock.wait_for_tick()` (mentioned in the module docstring), parallel tick dispatch across multiple agents, or snapshot persistence to disk.
