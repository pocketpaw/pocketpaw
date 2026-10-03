# backend_adapter — Adapter that makes PocketPaw's agent backends usable as knowledge base CompilerBackends

> This module provides `PocketPawCompilerBackend`, an adapter class that implements the knowledge_base compiler protocol by delegating to PocketPaw's pluggable agent backend registry (Claude SDK, OpenAI, etc.). It exists to decouple the standalone knowledge-base package from PocketPaw's specific LLM infrastructure, allowing KB compilation to automatically use whatever agent backend is currently active in the system. This bridges the gap between the generic knowledge_base.compiler.CompilerBackend interface and PocketPaw's concrete backend implementations.

**Categories:** Knowledge Base — Integration Layer, Adapter/Bridge Pattern, LLM Backend Abstraction, Agent Infrastructure  
**Concepts:** PocketPawCompilerBackend, adapter pattern, facade pattern, CompilerBackend protocol, agent registry, get_backend_class, Settings, async streaming, lazy initialization, event-driven architecture  
**Words:** 1266 | **Version:** 1

---

## Purpose

This module solves a critical architectural problem: the `knowledge_base` package is designed to be standalone and backend-agnostic, but it needs to call large language models (LLMs) during KB compilation (e.g., to generate structured JSON from prompts). Rather than embedding specific LLM dependencies into knowledge_base itself, PocketPaw uses an **adapter pattern** to bridge the two systems.

`PocketPawCompilerBackend` implements the `knowledge_base.compiler.CompilerBackend` protocol—a simple async interface requiring a `complete(prompt, system_prompt)` method—and delegates all actual LLM work to PocketPaw's agent registry. This allows KB compilation to respect PocketPaw's runtime configuration: whichever backend is active (Claude SDK, OpenAI, custom) automatically becomes the KB compiler's backend.

**In the system architecture**: Knowledge base lives in `/ee/cloud/kb/` as a relatively isolated subsystem. KB compilation operations (triggered by `router` or `knowledge` modules) import this adapter, which then reaches into PocketPaw's agent infrastructure. This allows PocketPaw to manage all LLM backend state in one place (the registry) while letting knowledge base remain decoupled.

## Key Classes and Methods

### PocketPawCompilerBackend

**Purpose**: Adapter class that makes PocketPaw's agent backends conform to the knowledge_base.compiler.CompilerBackend protocol.

**Key Methods**:

#### `__init__(backend_name: str = "", model: str = "")`
Initializes the adapter with optional overrides. If `backend_name` is provided, it overrides the default backend from settings. If `model` is provided, it updates the corresponding model setting (e.g., `claude_sdk_model` or `openai_model`). These parameters allow callers to request a specific backend or model without globally changing PocketPaw's configuration.

**Business logic**: Stores backend name and model as instance state so that `complete()` can apply these overrides when instantiating the actual backend.

#### `async def complete(prompt: str, system_prompt: str = "") -> str`
The core method implementing the CompilerBackend protocol. It orchestrates the full LLM call: loading settings, resolving the backend class, instantiating it, streaming its response, and cleaning up.

**Control flow**:

1. **Settings Resolution**: Loads PocketPaw's configuration via `Settings.load()`. Uses the provided `self._backend_name` if set; otherwise falls back to `settings.agent_backend` (the system's active backend).

2. **Model Override** (if `self._model` is set): Updates the appropriate model field in settings based on backend name. For example, if backend is "claude", sets `settings.claude_sdk_model`. This allows the caller to change models without mutating global config.

3. **Backend Resolution**: Calls `get_backend_class(backend_name)` to retrieve the backend class from PocketPaw's registry (e.g., `ClaudeBackend`, `OpenAIBackend`). If the backend isn't registered, logs a warning and returns an empty string (safe failure).

4. **Agent Instantiation**: Creates an instance of the backend class, passing the modified settings. This backend instance is responsible for authentication, HTTP setup, and LLM communication.

5. **Streaming and Aggregation**: Calls `agent.run(prompt, system_prompt=sys_prompt)` which returns an async generator of events. The method iterates over events, extracting message chunks (events where `type == "message"`). It stops when it receives a `"done"` event.

6. **Default System Prompt**: If no system_prompt is provided, uses a hardcoded default: `"You are a knowledge compiler. Output only valid JSON."` This guides the LLM to output structured data suitable for KB compilation.

7. **Cleanup**: The `finally` block ensures `await agent.stop()` is called, allowing backends to close connections, free resources, or log telemetry.

**Business logic**: The method treats streaming responses as chunks and concatenates them into a single string. This is idiomatic for LLM APIs that return tokens incrementally. The aggregated response is stripped of whitespace before returning.

## How It Works

**Data flow for a KB compilation request**:

```
router or knowledge module
  ↓
Calls: PocketPawCompilerBackend(backend_name, model).complete(prompt, system_prompt)
  ↓
complete() loads PocketPaw settings and resolves the backend from registry
  ↓
Instantiates the backend (e.g., ClaudeBackend, OpenAIBackend) with merged settings
  ↓
AsyncIO streams LLM response via agent.run()
  ↓
Aggregates chunks into single string
  ↓
Returns complete response (valid JSON, typically)
```

**Key design observations**:

- **Lazy initialization**: The backend class is resolved at runtime in `complete()`, not at `__init__()` time. This allows the registry to be populated after the adapter is instantiated, and lets the system swap backends dynamically.

- **Event-driven streaming**: Rather than awaiting a single response, the code iterates over async events. This is essential for long-running LLM calls—it can start processing output while the backend is still generating tokens. The code filters for `type == "message"` events, implying the backend emits multiple event types.

- **Graceful degradation**: If a backend isn't available, the method returns an empty string rather than raising an exception. Callers should handle empty responses (which `router` or `knowledge` presumably do).

- **Settings immutability at instance level**: The adapter takes `backend_name` and `model` as init parameters, but doesn't modify global settings. Each call to `complete()` loads settings fresh, applies overrides locally, and uses the modified settings only for that agent instance. This prevents cross-request state pollution.

## Authorization and Security

No explicit access controls are defined in this module. However, implicit security assumptions:

- **Backend registry access**: The call to `get_backend_class()` assumes the agent registry is available and populated. If authentication or registry ACLs are enforced elsewhere (in the agents subsystem), they would prevent unauthorized backends from being loaded.

- **Credentials delegation**: The module does not handle API keys or authentication. It passes settings to the backend class, which is responsible for using credentials (e.g., ANTHROPIC_API_KEY for Claude, openai.api_key for OpenAI) from PocketPaw's configuration.

- **Default system prompt injection**: The hardcoded system prompt (`"You are a knowledge compiler. Output only valid JSON."`) is benign but fixed. Callers can override it via the `system_prompt` parameter, so there's no prompt injection vulnerability from the default.

## Dependencies and Integration

**What this module depends on**:

- `pocketpaw.agents.registry.get_backend_class()`: Resolves backend classes by name. Indicates PocketPaw has a plugin architecture where backends are registered.
- `pocketpaw.config.Settings`: Loads PocketPaw's active configuration (backend name, model names, API keys, etc.). Suggests a centralized config system, likely environment-based or YAML-driven.
- `logging`: Standard Python logging for non-blocking warnings (e.g., backend not found).

**What depends on this module**:

- **`knowledge`**: Presumably imports `PocketPawCompilerBackend` to instantiate it when KB operations need LLM support (e.g., inferring KB structure from data).
- **`router`**: Likely uses this adapter to service HTTP endpoints that trigger KB compilation with a chosen backend.

**Integration pattern**: This module is a thin facade that translates between two independent protocols: the knowledge_base.compiler.CompilerBackend interface (async method signature) and PocketPaw's agent interface (streaming events, backend registry). It adds minimal logic, acting primarily as a bridge.

## Design Decisions

**1. Adapter Pattern (Facade Pattern variant)**
Rather than modifying knowledge_base to import PocketPaw directly (tight coupling), an adapter class was created. This allows knowledge_base to remain a standalone package; PocketPaw depends on knowledge_base, not vice versa.

**2. Lazy Backend Resolution**
Backend classes are resolved at call time (`complete()`) rather than init time (`__init__()`). This supports dynamic backend switching and defers the cost of looking up the backend to when it's actually needed.

**3. Streaming over Single Response**
The method consumes async events from `agent.run()` and concatenates chunks. This is more idiomatic for LLM APIs and allows responses to be processed incrementally (though this module concatenates the full response before returning).

**4. Per-Call Settings Override**
The `backend_name` and `model` init parameters are stored but applied only within `complete()`. They don't mutate PocketPaw's global settings. This keeps instances stateless from a global perspective and makes behavior predictable when multiple requests are in flight.

**5. Graceful Degradation**
If a backend is unavailable, the method returns `""` instead of raising. This allows callers to handle empty responses gracefully (e.g., fall back to a cached response, skip compilation). It's a tradeoff between fail-fast and resilience.

**6. Default System Prompt**
A fixed system prompt is provided if none is given. This ensures the LLM is primed to output JSON (a knowledge base compilation requirement) even if the caller doesn't specify one. It's a sensible default but not user-configurable at the module level.

---

## Related

- [untitled](untitled.md)
