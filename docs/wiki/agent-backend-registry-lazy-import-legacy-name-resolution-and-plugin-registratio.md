---
{
  "title": "Agent Backend Registry: Lazy Import, Legacy Name Resolution, and Plugin Registration Tests",
  "summary": "This test suite covers PocketPaw's backend registry, which provides lazy-loaded access to agent backends (Claude SDK, OpenAI Agents, Google ADK, OpenCode) while maintaining backward compatibility through a legacy name mapping. Tests verify that deprecated names like `gemini_cli` transparently resolve to `google_adk`, that unknown backends return `None` rather than raising, and that third-party plugins can register themselves at runtime.",
  "concepts": [
    "backend registry",
    "lazy import",
    "legacy backend names",
    "gemini_cli",
    "google_adk",
    "plugin registration",
    "register_backend",
    "list_backends",
    "BackendInfo",
    "optional dependencies"
  ],
  "categories": [
    "agent backends",
    "testing",
    "registry",
    "plugin system",
    "test"
  ],
  "source_docs": [
    "0ee867dc3b61ed77"
  ],
  "backlinks": null,
  "word_count": 461,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The backend registry is the single source of truth for which AI backends are available in a PocketPaw installation. It uses lazy imports to avoid loading heavy dependencies (torch, grpc, etc.) at startup, instead importing a backend's module only when it's first requested. This test file validates correctness of the registry's three main concerns: listing available backends, resolving classes and metadata by name (including legacy aliases), and supporting plugin registration.

## `list_backends()` — Canonical Name Set

```python
class TestListBackends:
    def test_returns_all_registered(self):
        backends = list_backends()
        assert "claude_agent_sdk" in backends
        assert "openai_agents" in backends
        assert "google_adk" in backends
        assert "opencode" in backends

    def test_does_not_include_legacy(self):
        backends = list_backends()
        assert "pocketpaw_native" not in backends
        assert "gemini_cli" not in backends
```

The two tests together enforce a deliberate design: the public listing only includes canonical names, not deprecated aliases. Exposing legacy names in the list would confuse operators building configuration files or UIs, since the same underlying backend would appear twice under different names.

## `get_backend_class()` — Lazy Import with Legacy Fallback

```python
class TestGetBackendClass:
    def test_claude_agent_sdk_loads(self):
        cls = get_backend_class("claude_agent_sdk")
        assert cls.__name__ == "ClaudeSDKBackend"

    def test_unknown_returns_none(self):
        assert get_backend_class("nonexistent_xyz") is None

    def test_missing_dep_returns_none(self):
        # Simulates ImportError from missing optional dependency
```

The `None`-on-unknown contract is critical. If the registry raised `ImportError` or `KeyError` for unknown backend names, a misconfigured `settings.yaml` would crash the entire PocketPaw process at startup. The `test_missing_dep_returns_none` test uses `patch` to simulate an `ImportError` during lazy import, verifying that a backend whose optional dependencies aren't installed gracefully returns `None` rather than propagating the import failure.

## `get_backend_info()` — Metadata Resolution

```python
class TestGetBackendInfo:
    def test_gemini_cli_legacy_info(self):
        # gemini_cli resolves to google_adk info

    def test_opencode_no_keys(self):
        # opencode needs no required env keys

    def test_unknown_returns_none(self):
        assert get_backend_info("does_not_exist") is None
```

The `required_keys` tests matter operationally: each backend specifies which environment variables must be present before it can be used. The tests ensure claude_agent_sdk requires its API key, openai_agents requires its key, google_adk requires its credentials, and opencode requires none. A missing required-keys field would allow users to select a backend that will immediately fail at the first LLM call.

## `register_backend()` — Plugin Registration

```python
class TestRegisterBackend:
    def test_plugin_registration(self):
        # registers a mock backend class under a new name
        # verifies it appears in list_backends() and get_backend_class()
```

This test validates the extension point. Third-party packages can call `register_backend(name, cls, info)` to add new backends without modifying PocketPaw's core. The test uses a mock class to confirm the registry stores it correctly.

## Known Gaps

The `_LEGACY_BACKENDS` dict is imported directly in tests to verify the mapping structure, but there is no test asserting that all keys in `_LEGACY_BACKENDS` are excluded from `list_backends()`. If someone added a legacy alias to `_LEGACY_BACKENDS` but accidentally also added it to the canonical registry, `list_backends()` would expose the alias.