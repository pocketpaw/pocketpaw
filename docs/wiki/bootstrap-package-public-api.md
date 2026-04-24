---
{
  "title": "Bootstrap Package Public API",
  "summary": "The bootstrap package exposes the four core primitives needed to assemble an agent's initial context: the protocol contracts, the default file-based provider, the cloud-agent provider, and the context builder. Centralising these re-exports here keeps consumer imports clean and makes it easy to see what the subsystem offers at a glance.",
  "concepts": [
    "bootstrap",
    "BootstrapContext",
    "BootstrapProviderProtocol",
    "DefaultBootstrapProvider",
    "AgentContextBuilder",
    "public API",
    "re-exports",
    "__all__",
    "agent identity"
  ],
  "categories": [
    "bootstrap",
    "agent-identity",
    "package-structure"
  ],
  "source_docs": [
    "39808c6a34fedd79"
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

The `pocketpaw.bootstrap` package is the entry point to the agent identity and startup subsystem. It re-exports four symbols from three sub-modules and nothing else, acting as a stable, narrow public surface for everything that touches agent bootstrapping.

## What Is Exported and Why

### `BootstrapProviderProtocol`
A structural `Protocol` that any bootstrap provider must satisfy. Exporting it here means consumers can type-hint against the protocol without knowing which concrete implementation they will receive at runtime. This is the foundation of PocketPaw's two-track design: OSS deployments use `DefaultBootstrapProvider` while cloud deployments swap in `CloudAgentBootstrapProvider` — both satisfy the same protocol.

### `BootstrapContext`
A dataclass that carries the assembled identity, soul, style, instructions, knowledge snippets, and user profile that will be rendered into the agent's system prompt. Exporting it alongside the protocol keeps the data contract visible: callers that receive a `BootstrapContext` know exactly what fields they can read.

### `DefaultBootstrapProvider`
Reads identity from local markdown files (`IDENTITY.md`, `USER.md`, etc.) on disk. This is the default path for self-hosted or developer deployments where no MongoDB agent record exists. It uses mtime-based caching so identity file edits are picked up without a restart.

### `AgentContextBuilder`
The high-level orchestrator that calls a provider to get a `BootstrapContext`, then layers in memory recalls, knowledge-base results, channel hints, health state, and other injected blocks. Callers only need this single class to build a complete system prompt. It enforces a character budget and assigns each injection block a priority so that critical blocks (identity, instructions) survive even when context is tight.

## Two-Track Design

The bootstrap subsystem is built around a clean protocol boundary precisely because PocketPaw serves two very different deployment models. In the OSS track, a developer runs a local instance with markdown identity files. In the cloud track, agents are records in MongoDB with their own persona, archetype, and values. By programming the rest of the runtime against `BootstrapProviderProtocol` rather than a concrete class, the agent loop, context builder, and all tool code remain identical across both tracks — only the provider changes.

## Design Rationale

Grouping these four exports under a single `__init__` removes the need for consumers to know the internal module layout. If `AgentContextBuilder` later moves from `context_builder.py` to a sub-package, only this file needs updating — all callers stay unchanged. The `__all__` list makes the intended surface explicit and prevents accidental re-export of internal helpers such as the `_IdentityCache` dataclass or the `_read_identity_file` utility.

## How the Pieces Fit Together

A typical startup sequence goes: the entry point calls `DefaultBootstrapProvider` (or `CloudAgentBootstrapProvider`), passes it to `AgentContextBuilder`, and calls `AgentContextBuilder.build()` with the current message to get the assembled system prompt string. The `BootstrapContext` object is an intermediate value — it is created by the provider, consumed by the builder, and never held long-term.

## Known Gaps

None identified. The package initialiser contains no logic of its own; all complexity lives in the sub-modules it re-exports.