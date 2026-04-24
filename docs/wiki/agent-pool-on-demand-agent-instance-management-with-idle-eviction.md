---
{
  "title": "Agent Pool — On-Demand Agent Instance Management with Idle Eviction",
  "summary": "Implements `AgentPool`, a lifecycle manager for cloud-deployed agent instances. Each agent gets its own `AgentBackend`, `SoulManager`, and isolated memory namespace. Instances are cached for reuse and evicted after configurable idle timeout (default 5 minutes) to bound memory usage.",
  "concepts": [
    "AgentPool",
    "AgentInstance",
    "SoulManager",
    "idle eviction",
    "LRU eviction",
    "build lock",
    "memory namespace",
    "GC background task",
    "multi-agent",
    "on-demand creation",
    "capacity cap"
  ],
  "categories": [
    "agent-runtime",
    "resource-management",
    "multi-agent",
    "lifecycle"
  ],
  "source_docs": [
    "736db97f48068359"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`AgentPool` solves the resource management challenge in multi-agent PocketPaw deployments where many named agents (each with distinct personalities, tools, and souls) can be active simultaneously. Creating a new `AgentBackend` and `SoulManager` for every incoming message would be prohibitively expensive; the pool caches live instances and reuses them across requests.

## Per-Instance Isolation

Each `AgentInstance` encapsulates:
- Its own `AgentBackend` (SDK client or subprocess)
- Its own `SoulManager` (separate soul state, personality, and memory)
- An isolated `memory_namespace` — agents cannot read each other's stored memories
- A `last_active` timestamp for eviction tracking
- The config snapshot (`created_from_updated_at`) so stale instances can be rebuilt after config changes

This isolation is critical in multi-tenant deployments where agent A and agent B must not share memories or personality drift into each other's personas.

## On-Demand Creation with Build Lock

`get()` acquires `_build_lock` (a single `asyncio.Lock`) before calling `_build()`. Without this lock, two concurrent requests for the same unbuilt agent would each race to create an instance, resulting in two backends and two soul managers — one of which would be immediately evicted, wasting initialisation cost and potentially creating a race on soul state.

## LRU Capacity Cap

`max_instances` (default 20) caps the total number of live instances. When the pool is at capacity and a new agent is requested, `_evict_oldest()` selects the instance with the oldest `last_active` timestamp and tears it down gracefully before building the new one. This least-recently-used eviction strategy preserves frequently used agents while reclaiming memory from inactive ones.

## GC Background Task

`_gc_loop()` runs every 60 seconds and evicts instances idle longer than `max_idle` seconds (default 300 seconds / 5 minutes). In multi-tenant scenarios with large agent rosters, many agents may be used rarely. Without GC, idle backends accumulate open SDK connections, subprocess handles, and memory indefinitely.

## Soul Initialisation and Idempotency

`_init_soul()` creates a `SoulManager` per agent with its own settings and memory namespace. `ensure_soul()` is idempotent — it checks whether a soul already exists for the agent before calling `_init_soul()`. This prevents double-initialisation when the pool is rebuilt after a PocketPaw restart while the soul file already exists on disk.

## Observation Pipeline

`observe()` forwards user/agent exchange pairs to the instance's `SoulManager`, allowing the soul to learn from conversations even when invoked through the pool rather than through `AgentLoop`. This keeps soul state current regardless of the call path.

## Known Gaps

- `_build_lock` is global to the pool; concurrent requests for two different unbuilt agents serialise unnecessarily. A per-agent-ID lock would allow parallel builds.
- Eviction does not check whether an instance is actively serving a request; evicting mid-stream would corrupt that conversation.
- Soul memories accumulated during a session are not checkpointed before teardown; a GC cycle racing with a final `observe()` call may lose the last memory update.
