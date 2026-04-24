---
{
  "title": "GuardianAgent Empty Response Regression Tests (Issue #636)",
  "summary": "This test module documents the fix for issue #636, where an empty `response.content` list from the Anthropic API caused an `IndexError` inside `GuardianAgent.check_command`. The fix implements fail-closed behavior: any empty, malformed, or missing API response defaults to `DANGEROUS` rather than crashing or defaulting to safe.",
  "concepts": [
    "GuardianAgent",
    "check_command",
    "fail-closed",
    "empty response",
    "IndexError regression",
    "issue #636",
    "security filter",
    "Layer 6",
    "SAFE",
    "DANGEROUS",
    "Anthropic API"
  ],
  "categories": [
    "testing",
    "security",
    "guardian",
    "test"
  ],
  "source_docs": [
    "8f533bdda1ecc754"
  ],
  "backlinks": null,
  "word_count": 411,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Background: Issue #636

`GuardianAgent` is PocketPaw's Layer 6 AI security filter. It calls Claude to classify shell commands as `SAFE` or `DANGEROUS` before the shell tool executes them. The bug occurred when the Anthropic API returned a response with `content = []` — an empty content list. The original code accessed `response.content[0]` without checking the length, raising `IndexError`.

From a security perspective, an uncaught exception in the guardian is equivalent to bypassing it — the exception would propagate up and the command might execute unguarded. The fix ensures that any anomalous response defaults to `DANGEROUS` (fail-closed).

## Fixture

```python
@pytest.fixture
def guardian():
    with (
        patch("pocketpaw.security.guardian.get_settings"),
        patch("pocketpaw.security.guardian.get_audit_logger"),
    ):
        agent = GuardianAgent()
        agent.client = MagicMock()
        return agent
```

Both `get_settings` and `get_audit_logger` are patched to avoid real configuration and logging setup. The `agent.client` is replaced with a `MagicMock` so individual tests can control what the mock API returns.

## TestGuardianEmptyResponse

Four tests cover the regression and the healthy paths:

**`test_empty_content_returns_dangerous`** — the primary regression test. `response.content = []` must produce `(is_safe=False, reason)` where `reason` contains `"empty"`:

```python
mock_response.content = []
guardian.client.messages.create = AsyncMock(return_value=mock_response)
is_safe, reason = await guardian.check_command("rm -rf /")
assert is_safe is False
assert "empty" in reason.lower()
```

The `"empty"` assertion confirms the reason is descriptive — it distinguishes an empty-response block from a `DANGEROUS` classification, which is important for audit log analysis.

**`test_empty_content_does_not_raise`** — explicitly catches `IndexError` and fails the test if it is raised:

```python
try:
    await guardian.check_command("ls -la")
except IndexError:
    pytest.fail("IndexError raised on empty response.content")
```

This test exists separately from the `is_safe` assertion because the original bug was a crash, not just a wrong return value. Having an explicit no-raise test makes the contract clear.

**`test_valid_safe_response_still_works`** — confirms that the empty-content fix did not break the normal `SAFE` path. A well-formed `{"status": "SAFE", "reason": "Read-only command"}` response must still return `(True, "Read-only command")`.

**`test_valid_dangerous_response_still_blocked`** — confirms the normal `DANGEROUS` path is intact. A `{"status": "DANGEROUS", "reason": "Destructive command"}` response must return `(False, "Destructive command")`.

## Why Fail-Closed Matters

In security systems, fail-open behavior (allowing an action when the guard cannot complete its check) creates an exploitable window. An attacker who can trigger API rate limits or cause transient failures could potentially bypass the guardian. Fail-closed means that even under degraded conditions, no command executes without explicit guardian approval.

## Known Gaps

This test file covers only the empty-content regression. Comprehensive coverage of the guardian (LLM classification, malformed JSON, local fallback, prompt injection, audit logging) is in `test_guardian_comprehensive.py`.