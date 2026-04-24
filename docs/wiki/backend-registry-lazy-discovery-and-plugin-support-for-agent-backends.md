---
{
  "title": "Backend Registry: Lazy Discovery and Plugin Support for Agent Backends",
  "summary": "The backend registry maps short backend names to `(module_path, class_name)` pairs and imports them on demand, so PocketPaw starts cleanly even when optional SDKs like `openai-agents` or `google-adk` are not installed. It also handles legacy backend name migration and exposes a plugin registration API.",
  "concepts": [
    "backend registry",
    "lazy import",
    "importlib",
    "plugin support",
    "legacy migration",
    "optional dependencies",
    "TYPE_CHECKING guard",
    "register_backend",
    "list_backends",
    "fallback backends"
  ],
  "categories": [
    "agents",
    "registry",
    "plugin architecture",
    "dependency management"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`registry.py` solves a concrete startup problem: PocketPaw ships with support for seven agent backends, each requiring a different optional dependency. If the registry used eager imports, a user who only has the Claude Agent SDK installed would see `ImportError` at startup because `google-adk` or `openai-agents` are missing. Lazy import defers this cost to the moment a specific backend is actually requested.

## How the Registry Works

The `_BACKEND_REGISTRY` dictionary maps each backend name to a `(module_path, class_name)` tuple:

```python
_BACKEND_REGISTRY = {
    "claude_agent_sdk": ("pocketpaw.agents.claude_sdk", "ClaudeSDKBackend"),
    "openai_agents":    ("pocketpaw.agents.openai_agents", "OpenAIAgentsBackend"),
    "google_adk":       ("pocketpaw.agents.google_adk", "GoogleADKBackend"),
    ...
}
```

`get_backend_class(name)` uses `importlib.import_module` to load the module and `getattr` to retrieve the class. If the import fails (missing optional dependency), it logs a warning and returns `None` rather than raising. The `AgentRouter` then handles the `None` case by falling back to a default backend.

`list_backends()` returns all registered names regardless of whether the backend's dependencies are installed. This supports UI features like "available backends" dropdowns where the user should see all options, with unavailable ones grayed out.

## Legacy Backend Migration

`_LEGACY_BACKENDS` maps old names that were removed to their recommended replacements:

```python
_LEGACY_BACKENDS = {
    "pocketpaw_native": "claude_agent_sdk",
    "open_interpreter":  "claude_agent_sdk",
    "claude_code":       "claude_agent_sdk",
    "gemini_cli":        "google_adk",
}
```

When `get_backend_class` receives a legacy name, it logs a deprecation warning, resolves to the fallback, and proceeds. This prevents configuration files written for older PocketPaw versions from breaking silently — users get a clear log message explaining the rename rather than an obscure `KeyError`.

## Plugin Support via `register_backend`

`register_backend(name, module, cls)` lets third-party code (or community plugins) add their own backends at runtime without forking PocketPaw. The function validates that the name does not collide with an existing built-in, then inserts the `(module, cls)` pair into `_BACKEND_REGISTRY`. The lazy-import mechanism works identically for plugin-registered backends.

## Import Guard Pattern

The `TYPE_CHECKING` import guard for `AgentBackend` and `BackendInfo` is critical. These types are only needed for type annotations, not at runtime. Without the guard, importing `registry.py` would trigger the import of `backend.py`, which in turn imports heavier dependencies, defeating the purpose of lazy loading.

## Known Gaps

- There is no capability introspection at the registry level. Callers cannot ask "does this backend support tool use?" or "does it support streaming?" without instantiating the backend first. A `BackendInfo` metadata structure exists in `backend.py` but is not surfaced through the registry API.
- Plugin backends are not persisted across restarts — `register_backend` only modifies the in-memory dict.
