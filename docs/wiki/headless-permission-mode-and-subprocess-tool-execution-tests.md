---
{
  "title": "Headless Permission Mode and Subprocess Tool Execution Tests",
  "summary": "This regression test suite guards against the permission hang bug introduced in commit 24f16e2, where gating `bypassPermissions` behind a user setting caused all Bash-based tool calls on messaging channels (Telegram, Discord, Slack) to hang indefinitely waiting for interactive approval. It also exercises the full subprocess path that the Claude SDK takes when executing memory tools.",
  "concepts": [
    "bypassPermissions",
    "headless mode",
    "Claude SDK backend",
    "permission hang bug",
    "messaging channels",
    "source introspection",
    "subprocess tool execution",
    "memory tools CLI",
    "tool policy",
    "regression test"
  ],
  "categories": [
    "testing",
    "security",
    "Claude SDK",
    "headless channels",
    "bug regression",
    "test"
  ],
  "source_docs": [
    "c1bee8ecffa25e80"
  ],
  "backlinks": null,
  "word_count": 455,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Background: The Permission Hang Bug

PocketPaw's Claude SDK backend runs in headless environments — messaging channels like Telegram, Discord, and Slack where there is no terminal. The Claude Agent SDK requires the `permission_mode = "bypassPermissions"` option to allow tool calls without prompting the user interactively.

In commit 24f16e2, this option was moved inside an `if self.settings.bypass_permissions:` guard that defaulted to `False`. The result: every tool call on every messaging channel hung indefinitely because the SDK waited for interactive approval that would never arrive. This broke memory tools, web search, Gmail integration, and any other Bash-based tool since PocketPaw v0.3.0.

The module header itself documents the bug, commit hash, and affected versions — a pattern that future maintainers can use to understand the regression context without digging through git history.

## `TestHeadlessPermissionMode`

This class uses source code introspection (`inspect.getsource`) rather than actually running the SDK backend. This approach avoids needing a real API key or a real SDK environment while still catching the structural bug:

- **`test_permission_mode_set_when_bypass_false`** — the core regression test. With `bypass_permissions=False` (the default), the source of `ClaudeSDKBackend.run` must NOT contain `"if self.settings.bypass_permissions"` and MUST contain `"bypassPermissions"`. If the guard returns, the test fails with a human-readable message explaining exactly what will break.
- **`test_permission_mode_set_when_bypass_true`** — confirms the fix works when the setting is `True` as well (belt-and-suspenders).
- **`test_no_conditional_bypass_in_options_build`** — parses the source line by line, finds every line containing a `permission_mode` assignment, and asserts that none of those lines start with `if`. This structural check prevents a future refactor from reintroducing the conditional even with different variable names.

The `_make_settings` helper constructs a fully-specified `MagicMock` Settings object. Every field is populated to prevent `AttributeError` in backend initialization — the mock needs to look like a real settings object.

## `TestToolExecutionInSubprocess`

This class simulates the actual path the Claude SDK takes: spawning `python -m pocketpaw.tools.cli` as a subprocess. The SDK runs Bash commands in subprocesses; these tests verify that the CLI tool completes without hanging.

- **`test_remember_via_subprocess`** — calls `pocketpaw.tools.cli remember` with a JSON payload, asserts `returncode == 0` and "Remembered" in stdout, with a 10-second timeout (the hang manifested as infinite wait; 10 seconds is a generous upper bound).
- **`test_remember_then_recall_via_subprocess`** — full round-trip: save a fact then retrieve it, both via subprocess. Confirms the memory store is readable from a fresh process.
- **`test_subprocess_tool_does_not_hang`** — deliberately simple: just verifies the CLI exits. The `HOME` and `USERPROFILE` environment variables are overridden to isolate from the developer's real config.

## Known Gaps

The source introspection approach is brittle — if `run` is significantly refactored (e.g., split into helper methods), `inspect.getsource` may not capture the relevant code. No test covers the OpenAI Agents or Google ADK backends, which may have similar permission requirements.