---
{
  "title": "Deep Work Module: Public API for AI Project Orchestration",
  "summary": "The `pocketpaw.deep_work` package exposes a high-level API for AI-driven project orchestration — from natural language goal parsing through multi-agent execution, pause/resume lifecycle, and crash recovery. The singleton `DeepWorkSession` is lazily initialized and wired to the MessageBus for task completion events.",
  "concepts": [
    "Deep Work",
    "project orchestration",
    "GoalParser",
    "DeepWorkSession",
    "lazy singleton",
    "crash recovery",
    "project lifecycle",
    "MessageBus",
    "SimulationClock",
    "multi-agent execution"
  ],
  "categories": [
    "Deep Work",
    "Orchestration"
  ],
  "source_docs": [
    "7fef6454bb97aeb1"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/deep_work/__init__.py` is the public interface for PocketPaw's Deep Work subsystem — an AI project orchestration layer that breaks a natural language goal into structured tasks, assigns them to AI agents, and manages execution through approval, pause, resume, and cancel states.

## Design: Facade Over DeepWorkSession

The module exposes a flat set of async functions (`start_deep_work`, `approve_project`, `pause_project`, etc.) rather than requiring callers to interact with `DeepWorkSession` directly. This facade pattern keeps the public API stable even if the internal session implementation changes. The session is a singleton (via `get_deep_work_session()`) initialized lazily on first use.

## Lazy Singleton Initialization

`get_deep_work_session()` constructs `DeepWorkSession` with its dependencies on first call:

```python
manager = get_mission_control_manager()
executor = get_mc_task_executor()
session = DeepWorkSession(manager, executor)
session.subscribe_to_bus()
```

The `subscribe_to_bus()` call wires the session to the `MessageBus` so it receives task completion events from agents without polling. Lazy initialization avoids importing Beanie/MongoDB models at module load time, which would fail in environments where the database isn't configured.

`reset_deep_work_session()` clears the singleton, primarily for testing — it allows each test to start with a fresh session without side effects from previous tests.

## Project Lifecycle

The typical flow:

1. **`parse_goal(user_input)`** — calls `GoalParser` to analyze the natural language description and return a `GoalAnalysis` (domain, complexity, AI vs. human roles, clarification questions).
2. **`start_deep_work(user_input, research_depth)`** — submits the goal for planning. Returns immediately with a `Project` in `AWAITING_APPROVAL` status; actual planning runs in the background.
3. **`approve_project(project_id)`** — transitions to `EXECUTING` and begins task dispatch to agents.
4. **`pause_project(project_id)`** — suspends execution; in-flight tasks complete but no new tasks are dispatched.
5. **`resume_project(project_id)`** — restarts dispatch.
6. **`cancel_project(project_id)`** — stops all tasks and marks the project terminal. Added in v2 (2026-02-26).

## Crash Recovery

`recover_interrupted_projects()` handles the scenario where the server restarts while projects are executing. It scans persisted project state for projects in `EXECUTING` status and resumes them. Without this, a server restart would permanently strand in-progress projects. The function returns the count of recovered projects.

## SimulationClock and TickSnapshot Exports

As of 2026-03-26 (issue #633), `SimulationClock` and `TickSnapshot` are exported from the package `__init__`. These support discrete tick-based simulation for testing multi-agent execution without wall-clock time.

## Known Gaps

- `research_depth` in `start_deep_work()` defaults to `"standard"` at this layer but `"auto"` at the API layer (`deep_work/api.py`). The mismatch could cause confusion about which default applies when calling the Python API directly.
- There is no timeout for projects stuck in `AWAITING_APPROVAL`. A project that is never approved accumulates persisted state indefinitely.