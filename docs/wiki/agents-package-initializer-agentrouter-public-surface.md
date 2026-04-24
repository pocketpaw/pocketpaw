---
{
  "title": "Agents Package Initializer — AgentRouter Public Surface",
  "summary": "The `pocketpaw.agents` package init re-exports `AgentRouter` as the sole public symbol, establishing a clean import boundary for the agents subsystem. Consumers import the router without knowing its internal module location.",
  "concepts": [
    "AgentRouter",
    "package init",
    "__all__",
    "public API",
    "import boundary"
  ],
  "categories": [
    "agent-runtime",
    "package-structure"
  ],
  "source_docs": [
    "66d4fea70ce2c8c2"
  ],
  "backlinks": null,
  "word_count": 494,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/agents/__init__.py` is the public surface of the agents subsystem. It re-exports `AgentRouter` from `pocketpaw.agents.router`, making it available as `from pocketpaw.agents import AgentRouter`.

## Why a Dedicated Init Exists

Python packages can expose internal modules in two ways: let callers import directly from the internal path (`pocketpaw.agents.router`) or publish a controlled surface through `__init__.py`. PocketPaw chooses the latter, for good reason.

Internal module names and file layout are implementation details. `router.py` could be split into `router_core.py` and `router_registry.py` tomorrow, or merged with `loop.py`, without breaking any code that imports from `pocketpaw.agents`. The package init acts as a stable facade.

`__all__ = ["AgentRouter"]` is an explicit contract. Static analysis tools (mypy, pyright, Pylance), documentation generators (pdoc, Sphinx), and IDEs use `__all__` to determine which names belong to the public API. Anything not listed is treated as internal and may be excluded from autocomplete and docs. This is especially valuable in a large codebase where the agents package contains many internal helpers (backend adapters, loop utilities, protocol types) that should not be imported from the outside.

## AgentRouter as the Single Entry Point

`AgentRouter` is the one component that all callers — `AgentLoop`, the REST API, the A2A server, and the CLI — need from the agents subsystem. Everything else is an implementation detail. By limiting the public surface to a single name, the package enforces the principle of least privilege for imports: you can use the agents subsystem without knowing it has a `plan_mode.py`, a `pool.py`, or a `delegation.py`.

## Relationship to the Rest of the Subsystem

Inside the package, modules import from each other freely. `loop.py` imports `AgentRouter` from `pocketpaw.agents.router` (not from `__init__`); `backend.py` defines types that `claude_sdk.py` imports directly. The init is purely for external consumers. This avoids circular import risks that can arise when internal modules import from `__init__` in the same package.

## Known Gaps

None. This file is complete by design. Its minimal surface area is intentional and should be preserved — additions should only be made when a new type genuinely needs to be part of the agents subsystem's public contract.


## Design Pattern: Minimal Init as API Contract

This pattern — a nearly empty `__init__.py` that re-exports one or two names — is deliberately chosen over a fat init that re-exports everything. Fat inits create two problems: import cycles (when internal modules import from `__init__` in the same package, creating circular dependency chains) and accidental surface expansion (a developer adds an import to the init "for convenience" and it becomes load-bearing in production code, making it impossible to remove later).

By exporting only `AgentRouter`, the init signals that the agents package has exactly one thing it wants the rest of the codebase to know about. If a second symbol genuinely needs to be part of the public contract in the future — for example, if `AgentEvent` is promoted to a package-level type — it would be a deliberate and visible change to this file, not an accident.
