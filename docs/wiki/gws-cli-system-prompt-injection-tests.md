---
{
  "title": "GWS CLI System Prompt Injection Tests",
  "summary": "This test suite verifies that the AgentContextBuilder conditionally injects Google Workspace CLI guidance into the system prompt only when the google-workspace MCP server is active and enabled. It also confirms that the gws.md guidance file exists on disk with the required content.",
  "concepts": [
    "GWS CLI",
    "system prompt injection",
    "AgentContextBuilder",
    "MCP server",
    "MCPServerConfig",
    "conditional prompt",
    "gws.md",
    "Google Workspace",
    "build_system_prompt",
    "MCP configuration",
    "bootstrap"
  ],
  "categories": [
    "testing",
    "MCP integration",
    "prompt engineering",
    "Google Workspace",
    "test"
  ],
  "source_docs": [
    "bda8aa2df12fbd15"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `test_gws_prompt.py` module guards against a subtle but important failure mode: injecting tool-specific guidance into the agent system prompt when the relevant tool is not present, or—worse—not injecting it when the tool is active. Both cases degrade agent behavior.

The Google Workspace CLI (`gws`) is an optional MCP server that gives the agent access to Gmail, Calendar, Drive, and other Google services. When it is configured and enabled, the agent needs explicit instructions about how to use the `gws` command-line interface correctly. These instructions live in a markdown file (`gws.md`) co-located with the bootstrap module.

## Why Conditional Injection Matters

System prompts have token costs. Injecting guidance for every possible tool regardless of whether it is configured would bloat the prompt and reduce context budget for actual conversation. The `AgentContextBuilder.build_system_prompt()` method therefore reads the current MCP configuration at call time and includes tool-specific guidance sections only for tools that are present and enabled.

This design also prevents confusing the agent. If `gws.md` content were always present, the agent might attempt to invoke `gws` commands even when the tool is unavailable, producing errors that are hard to diagnose.

## Test Classes

### `TestGwsPromptInjection`

This class uses `unittest.mock.patch` to control what `load_mcp_config` returns, isolating the test from the real filesystem configuration:

- **`test_gws_prompt_injected_when_mcp_active`** — verifies that both "Google Workspace CLI" and "gws" appear in the prompt when a properly configured, enabled `google-workspace` server is present. This is the happy path.
- **`test_gws_prompt_not_injected_when_absent`** — confirms the prompt is clean when no MCP servers are configured at all. Prevents false positives.
- **`test_gws_prompt_not_injected_when_disabled`** — the server is configured but `enabled=False`. This edge case matters because users may disable MCP servers temporarily without removing them from config. The agent must not receive guidance for a tool it cannot call.
- **`test_gws_prompt_not_injected_for_other_servers`** — a different MCP server (e.g., `github`) being active must not trigger GWS injection. Prevents cross-tool contamination in prompt logic.

### `TestGwsMdFile`

This class performs filesystem sanity checks:

- **`test_gws_md_file_exists`** — the `gws.md` file must exist in `src/pocketpaw/bootstrap/`. If a developer accidentally deletes or moves the file, the agent silently loses GWS guidance. This test catches that.
- **`test_gws_md_has_content`** — the file must contain at minimum 100 characters, mention "Google Workspace CLI", include the `--dry-run` flag, and reference `gws auth login`. These assertions encode the minimum viable contract for the guidance document. A stub or placeholder file would fail here.

## Fixture Design

The `builder()` fixture creates a fresh `AgentContextBuilder` instance for each test, ensuring no state leaks between tests. The `_make_gws_config()` helper centralizes MCPServerConfig construction so test bodies stay readable.

## Known Gaps

No known TODOs or FIXMEs appear in this module. The test coverage focuses on the conditional injection logic but does not test the exact wording of the injected GWS prompt section beyond checking for two string anchors (`"Google Workspace CLI"` and `"gws"`). Future tests could assert that specific command examples from `gws.md` appear in the prompt.