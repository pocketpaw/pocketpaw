---
{
  "title": "Tool Registry Validation: Required Parameter Guards and Audit Logging (Issue #793)",
  "summary": "These tests cover the fix for issue #793, where `ToolRegistry.execute` was accepting empty strings and whitespace-only values for required tool parameters, allowing agent-generated calls to silently bypass input validation. The fix adds strict pre-execution validation that rejects `None`, missing, empty, and whitespace-only values for string parameters marked as required, and emits an audit log entry on failure.",
  "concepts": [
    "ToolRegistry",
    "required_parameter_validation",
    "issue_793",
    "empty_string_rejection",
    "audit_logging",
    "DummyTool",
    "whitespace_validation",
    "execute",
    "BaseTool"
  ],
  "categories": [
    "tool-system",
    "security",
    "testing",
    "test"
  ],
  "source_docs": [
    "35c1abd29a032e30"
  ],
  "backlinks": null,
  "word_count": 440,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Before issue #793, a tool marked with required string parameters would accept `command=""` or `command="   "` as valid inputs and proceed to execute. This was dangerous: an agent that generated an empty shell command, an empty file path, or whitespace-only content would cause unpredictable tool behavior rather than receiving a clear error.

## The Validation Rules

The `ToolRegistry` now validates required string parameters before dispatching to the tool's `execute` method. A value is rejected if it is:

- `None` — explicitly null
- Missing entirely from the kwargs — not provided at all
- An empty string `""` — the core bug in #793
- Whitespace-only — `" "`, `"\t"`, `"\n"`, or combinations thereof

Values that are **not** rejected:
- Non-string types (`int`, `bool`, `list`) — these are not subject to the string-empty check
- `False` booleans — a falsy but valid value
- Strings with leading/trailing spaces around real content — `" valid "` passes

```python
async def test_whitespace_only_rejected(self):
    result = await registry.execute("test_tool", command="   ")
    assert "Missing required parameter" in result

async def test_string_with_leading_spaces_accepted(self):
    result = await registry.execute("test_tool", command="  actual content  ")
    assert "Missing required parameter" not in result
```

## Multi-Parameter Handling

When a tool has multiple required parameters, all are validated. If one is empty and another is valid, the error message identifies the specific failing parameter. If both are empty, both may be reported. This helps agents and developers pinpoint which argument triggered the rejection.

## Tool Not Found

Attempting to execute a tool name that has not been registered returns an error string rather than raising — consistent with the registry's pattern of never throwing from `execute`:

```python
async def test_tool_not_found(self):
    result = await registry.execute("nonexistent_tool")
    # Returns error string, does not raise
```

## Audit Logging on Validation Failure

The `TestAuditLoggingOnValidationFailure` class verifies that when validation rejects a parameter, the failure is written to the audit trail. This is important for security and debugging: operators can inspect the audit log to see whether agents are repeatedly generating empty arguments (a sign of prompt injection, model confusion, or a tool schema mismatch).

## DummyTool Fixture Pattern

The tests use a `DummyTool` that accepts a `required` list at construction time, making it easy to test tools with zero, one, or multiple required parameters without maintaining separate classes. The `_make_registry` helper encapsulates registry construction and registration, keeping test bodies focused on the validation behavior being asserted.

## Known Gaps

No TODOs. The validation only covers string-type required parameters — if a parameter's schema type is `integer` or `boolean`, the empty-string check does not apply, which is intentional. Future work might add type-coercion validation for numeric parameters.