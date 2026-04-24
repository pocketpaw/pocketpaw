---
{
  "title": "Agent Backend Protocol: Capability Flags, BackendInfo, and Identity Contract Tests",
  "summary": "This test suite validates the core contracts of PocketPaw's agent backend abstraction layer, covering the `_DEFAULT_IDENTITY` fallback string, the `Capability` flag enum, the immutable `BackendInfo` descriptor, and the `AgentBackend.run()` method signature. Together these tests enforce the interface that every backend adapter must satisfy to be a valid plugin in PocketPaw's multi-backend architecture.",
  "concepts": [
    "AgentBackend",
    "BackendInfo",
    "Capability",
    "bitflag enum",
    "_DEFAULT_IDENTITY",
    "system prompt",
    "backend protocol",
    "frozen dataclass",
    "session_key",
    "inspect"
  ],
  "categories": [
    "agent backends",
    "testing",
    "core protocol",
    "capability flags",
    "test"
  ],
  "source_docs": [
    "525285e815754a30"
  ],
  "backlinks": null,
  "word_count": 520,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple AI backends (Claude SDK, OpenAI Agents, Google ADK, and others) behind a single unified interface. The `AgentBackend` protocol and its supporting types define the contracts every backend must fulfil. This test file validates those contracts at the unit level, protecting against regressions that would break the plugin system.

## `_DEFAULT_IDENTITY` — System-Prompt Fallback

```python
from pocketpaw.agents.backend import _DEFAULT_IDENTITY

class TestDefaultIdentity:
    def test_default_identity_is_nonempty(self):
        assert isinstance(_DEFAULT_IDENTITY, str)
        assert len(_DEFAULT_IDENTITY.strip()) > 0

    def test_default_identity_mentions_pocketpaw(self):
        assert "PocketPaw" in _DEFAULT_IDENTITY
```

`_DEFAULT_IDENTITY` is the safety net used when an operator hasn't supplied a custom system prompt. The tests enforce two invariants: the string must be non-empty (preventing the agent from starting with no persona), and it must at least mention "PocketPaw" (ensuring the agent self-identifies correctly rather than presenting as a blank shell). If either invariant broke, deployments that rely on the built-in identity would silently produce mis-identified agents.

## `Capability` — Feature-Flag Enum

```python
class TestCapability:
    def test_flag_combination(self):
        combo = Capability.STREAMING | Capability.TOOLS | Capability.MCP
        assert Capability.STREAMING in combo
        assert Capability.MULTI_TURN not in combo
```

`Capability` is a bitflag enum. The tests verify that individual flags have non-zero values (ruling out accidental zero-value enum members), that bitwise OR composition works correctly, and that a combined flag correctly excludes capabilities not added. These guards prevent the class of bug where two flags share a value and every `in` check returns a false positive, or where a flag is accidentally set to `0` and always tests falsy.

## `BackendInfo` — Immutable Backend Descriptor

```python
class TestBackendInfo:
    def test_frozen(self):
        ...  # verifies BackendInfo is frozen/immutable

    def test_required_keys_and_supported_providers(self):
        ...  # verifies the required_env_keys and supported_provider fields
```

`BackendInfo` is the static metadata record each backend exposes: name, display name, required environment variables, and capability flags. The frozen test confirms it is a frozen Pydantic model or frozen dataclass, which matters because `BackendInfo` instances are cached in the registry and shared across threads. Mutating a cached descriptor would silently corrupt all subsequent capability checks.

`test_with_tools` verifies that `BackendInfo` correctly accepts a list of built-in tools, which is needed for backends like OpenCode that advertise their own tool set.

## `AgentBackend.run()` — Session Key Parameter Contract

```python
class TestAgentBackendProtocol:
    def test_run_has_session_key_param(self):
        import inspect
        sig = inspect.signature(AgentBackend.run)
        assert "session_key" in sig.parameters
```

This is a protocol-compliance test using Python's `inspect` module. The `run()` method signature is part of the public contract: every backend must accept a `session_key` so that the memory manager can scope conversation history to a particular session. Using `inspect` rather than a direct call guards against drift between the protocol definition and concrete backend implementations.

## Why These Tests Matter

The backend system is a plugin boundary. New backends contributed by the community or added internally must conform to `BackendInfo` and `AgentBackend.run()`. These tests act as a living specification that documents minimum requirements without requiring a real AI backend to be running.

## Known Gaps

No gaps flagged in the source. The test for `test_all_capabilities` implicitly covers the grouping but does not exhaustively assert every possible future capability flag — new flags added to `Capability` won't be tested until this file is updated.