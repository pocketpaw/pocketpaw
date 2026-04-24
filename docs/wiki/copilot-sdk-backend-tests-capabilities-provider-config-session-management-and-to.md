---
{
  "title": "Copilot SDK Backend Tests: Capabilities, Provider Config, Session Management, and Tool Injection",
  "summary": "This test module validates the `CopilotSDKBackend` — PocketPaw's adapter for GitHub Copilot's agent SDK — entirely through mocks, requiring no real CLI binary or SDK installation. It covers static capability metadata, provider-specific configuration dispatch, multi-turn session reuse, event stream mapping, and the injection of PocketPaw tool instructions into the Copilot prompt.",
  "concepts": [
    "CopilotSDKBackend",
    "BackendInfo",
    "Capability",
    "shutil.which",
    "provider configuration",
    "session reuse",
    "event stream mapping",
    "tool injection",
    "tool policy map",
    "backend registry",
    "history injection",
    "Copilot SDK"
  ],
  "categories": [
    "agent runtime",
    "testing",
    "backend adapters",
    "tool integration",
    "test"
  ],
  "source_docs": [
    "fd326b5ca003f486"
  ],
  "backlinks": null,
  "word_count": 580,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `CopilotSDKBackend` bridges PocketPaw's agent runtime to the GitHub Copilot SDK, which itself wraps multiple LLM providers (Copilot, OpenAI, Azure, Anthropic). Because the SDK is an optional dependency and the CLI binary may not exist in every environment, all tests mock at the boundary, ensuring the test suite passes in CI without any Copilot credentials.

## Static Metadata (`TestCopilotSDKInfo`)

The `BackendInfo` returned by `CopilotSDKBackend.info()` is the contract consumed by PocketPaw's backend registry and the dashboard UI. Tests assert:
- `name == "copilot_sdk"` and `display_name == "Copilot SDK"` (display strings used in configuration UI)
- `Capability.STREAMING`, `TOOLS`, `MULTI_TURN`, and `CUSTOM_SYSTEM_PROMPT` are present; `Capability.MCP` is absent (the Copilot SDK handles tools via its own protocol, not MCP)
- `required_keys == []` — the backend relies on the Copilot CLI's credential store rather than PocketPaw's credential system
- `tool_policy_map` maps PocketPaw tool categories to Copilot SDK permission strings: `shell` → `"shell"`, `file_ops` → `"write_file"`, `web_search` → `"browser"`

## Initialization Probes (`TestCopilotSDKInit`)

The backend detects CLI availability via `shutil.which("copilot")` and SDK availability by attempting to import the `copilot` module. Tests patch both probes and assert the resulting `_cli_available` and `_sdk_available` flags, which gate behavior in `run()` and `get_status()`.

When the CLI is missing, `run()` yields an error event instead of crashing. When the SDK is missing, the same graceful degradation applies. These tests prevent regressions where a missing optional dependency causes an unhandled `ImportError` mid-stream.

## Provider Configuration (`TestCopilotSDKHelpers`)

`_get_provider_config` translates a PocketPaw provider name into the configuration dict expected by the Copilot SDK session constructor. Tests assert distinct config shapes for `copilot`, `openai`, `azure`, and `anthropic` — ensuring each provider receives its required fields (endpoint URL, API key, model name) without leaking fields meant for other providers.

## History Injection

`test_inject_history` confirms that prior conversation turns are prepended to the outbound prompt string. `test_inject_history_truncates` verifies that when history exceeds a configured limit, older messages are dropped from the front — preventing the injected history from overflowing the Copilot SDK's own context window before PocketPaw's budget assembler has a chance to enforce limits.

## Event Stream Mapping (`TestCopilotSDKRun`)

The Copilot SDK emits typed events; PocketPaw normalizes them to its own internal event dict format. Each event type has a dedicated test:
- `message_delta` → streaming text chunk
- `thinking_delta` → reasoning trace (distinct display in the dashboard)
- `complete_message` → signals stream end
- `tool_call` and tool result → round-trip tool execution events
- `error` → propagated as a PocketPaw error event, not an exception

`test_max_turns_limit` verifies that when the backend's turn counter exceeds the configured limit, processing stops gracefully rather than looping forever.

## Session Reuse (`TestCopilotSDKCrossBackend`)

`test_session_reuse` confirms that a second call with the same `session_key` receives the same SDK session object rather than creating a new one, preserving the Copilot SDK's own internal conversation state.

## Tool Instructions (`TestCopilotSDKToolInstructions`)

PocketPaw injects descriptions of available tools into the system prompt so the LLM knows how to invoke them. `test_prompt_includes_tool_instructions` asserts these descriptions appear in the outbound message. `test_tool_instructions_respect_policy` confirms that tools not permitted by the active policy are excluded from the injected instructions.

## Registry Integration

`TestCopilotSDKRegistry` confirms the backend is registered under `"copilot_sdk"` in PocketPaw's backend registry and is surfaced in the `list_backends()` result, so the configuration UI can discover and display it.

## Known Gaps

No TODO or FIXME markers are present. The MCP capability is explicitly absent from the backend's declared capabilities, but there is no test asserting that MCP-specific code paths are unreachable through this backend.