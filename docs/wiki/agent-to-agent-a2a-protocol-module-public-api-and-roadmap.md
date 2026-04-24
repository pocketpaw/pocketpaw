---
{
  "title": "Agent-to-Agent (A2A) Protocol Module: Public API and Roadmap",
  "summary": "The `pocketpaw.a2a` package implements Google's Agent-to-Agent (A2A) protocol, allowing PocketPaw to act as both an A2A server (accepting tasks from external agents) and an A2A client (delegating tasks to external agents). This `__init__.py` re-exports the full public surface and documents the three-phase implementation roadmap.",
  "concepts": [
    "A2A protocol",
    "Agent Card",
    "agent interoperability",
    "Task",
    "TaskState",
    "JSON-RPC",
    "register_routes",
    "multi-agent",
    "streaming",
    "TextPart",
    "FilePart",
    "DataPart"
  ],
  "categories": [
    "A2A protocol",
    "agent runtime",
    "interoperability",
    "package structure"
  ],
  "source_docs": [
    "51548547167dcff1"
  ],
  "backlinks": null,
  "word_count": 283,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## What is the A2A Protocol?

A2A (Agent-to-Agent) is an open protocol by Google for inter-agent communication. It defines a standard way for AI agents to discover each other's capabilities (via Agent Cards), send tasks, receive streaming results, and exchange structured messages with typed content parts (text, file, data). PocketPaw implements this protocol to become interoperable with any A2A-compatible agent ecosystem.

## Implementation Phases

The module comment documents a deliberate three-phase rollout:

- **Phase 1 — A2A Server**: Exposes PocketPaw as a remote A2A agent. External systems can send tasks to PocketPaw.
- **Phase 2 — A2A Client**: PocketPaw can delegate tasks to external A2A agents, enabling orchestration of external specialized agents.
- **Phase 3 — A2A Registry**: Multi-agent orchestration and a dashboard UI for managing agent networks.

## Public API Surface

The `__init__.py` re-exports everything a consumer of `pocketpaw.a2a` needs:

- **Models**: `Task`, `TaskStatus`, `TaskState`, `TaskSendParams`, `A2AMessage`, `Part`, `TextPart`, `FilePart`, `DataPart`, `Artifact`, `AgentCard`, `AgentSkill`, `TaskStatusUpdateEvent`, `TaskArtifactUpdateEvent`
- **JSON-RPC**: `JSONRPCRequest`, `JSONRPCResponse`, `JSONRPCError`
- **Server registration**: `register_routes` (mounts A2A endpoints on a FastAPI app)

This flat re-export pattern means consumers write `from pocketpaw.a2a import Task, AgentCard` rather than importing from the internal submodules directly.

## Design Principles

The A2A module is designed to be self-contained and independent from the rest of PocketPaw's agent runtime. It imports from `pocketpaw.a2a.*` only, with no cross-dependencies on the session, memory, or channel adapter systems. This makes it portable — it could theoretically be extracted into a standalone library.

## Known Gaps

Phases 2 and 3 are documented as planned but the current source only confirms Phase 1 (server) and Phase 2 (client via `a2a/client.py`) exist. Phase 3 (registry + dashboard) is not yet implemented per the roadmap comment.