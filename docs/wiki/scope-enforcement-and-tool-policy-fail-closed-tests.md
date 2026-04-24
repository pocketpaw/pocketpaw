---
{
  "title": "Scope Enforcement and Tool Policy Fail-Closed Tests",
  "summary": "This test module validates that PocketPaw's `require_scope` FastAPI dependency rejects requests that lack explicit authorization markers, preventing silent scope bypass via master, session, or cookie auth. It also verifies that `ToolPolicy` raises on unknown profile names rather than defaulting to an open or undefined state.",
  "concepts": [
    "require_scope",
    "scope enforcement",
    "fail-closed",
    "ToolPolicy",
    "tool profiles",
    "FastAPI dependency",
    "oauth token",
    "api key",
    "admin scope",
    "enforce_scope marker",
    "security sprint",
    "bypass prevention"
  ],
  "categories": [
    "security",
    "testing",
    "authorization",
    "tool policy",
    "test"
  ],
  "source_docs": [
    "d1502ba432640b41"
  ],
  "backlinks": null,
  "word_count": 539,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_require_scope_enforcement.py` is a security-focused test module written during sprint cluster B (issues #888 and #889). It exists to lock down two classes of fail-open behavior that were discovered: silent scope bypass through legacy auth paths, and silent fail-open when an unrecognized tool profile name is passed to `ToolPolicy`.

## Why This Module Uses `pytest.mark.enforce_scope`

The root conftest sets a global `_TESTING_FULL_ACCESS` bypass that allows test requests through without real credential checks. This shortcut is useful for most tests, but it would make scope-enforcement tests meaningless — they need the real fail-closed logic active. `pytestmark = pytest.mark.enforce_scope` opts the entire module out of that bypass so each test exercises the production code path.

## Issue #888 — Scope Bypass via Silent Fallback

Before the fix, `require_scope()` had a silent fallback at the end of its logic that allowed requests carrying master, session, cookie, or localhost auth through without actually checking whether the required scope was granted. The intention was probably convenience, but the effect was that any authenticated client could reach a scope-gated route simply by not setting `api_key` or `oauth_token`.

`TestRequireScopeNoFullAccessMarker` covers every variant of this:

- **No auth markers at all** — must 403, not pass silently.
- **`full_access=True` on request state** — the only legitimate way to bypass scope for trusted internal callers; must 200.
- **API key without the required scope** — must 403.
- **API key with the exact required scope** — must 200.
- **API key with `admin` scope** — must 200 (admin is a superscope).
- **OAuth token missing the required scope** — must 403.
- **OAuth token including the required scope in its space-separated list** — must 200.

The helper `_build_app_with_state(**state_kwargs)` constructs a minimal FastAPI app that injects arbitrary values onto `request.state` via middleware, then exposes a single `/protected` route gated by `require_scope("memory")`. This pattern keeps the tests isolated from the real PocketPaw API wiring while still exercising the actual `require_scope` dependency.

## Issue #889 — ToolPolicy Fail-Open on Unknown Profile

`ToolPolicy` maps a profile name (e.g., `"minimal"`, `"coding"`, `"full"`) to a set of allowed tools. The bug: passing an unrecognized profile name caused the policy to silently construct in an indeterminate state, potentially allowing or denying tools in unpredictable ways depending on internal defaults.

`TestToolPolicyUnknownProfile` covers three scenarios:

- **Unknown profile** — must raise `ValueError` with a message matching `"Unknown tool profile"` at construction time, not lazily at the first `is_tool_allowed` call.
- **Valid `minimal` profile** — grants `remember` but denies `shell`, confirming the profile is respected.
- **`full` profile** — unrestricted; any tool name, including ones not in any list, must return `True`.

The fail-at-construction design is deliberate: catching the error early gives operators a clear signal during startup or configuration validation rather than a confusing partial-execution failure later.

```python
class TestToolPolicyUnknownProfile:
    def test_unknown_profile_raises_at_construction(self):
        from pocketpaw.tools.policy import ToolPolicy
        with pytest.raises(ValueError, match="Unknown tool profile"):
            ToolPolicy(profile="this-profile-does-not-exist")
```

## Known Gaps

No explicit `TODO` or `FIXME` markers appear in this file. However, the `TestRequireScopeNoFullAccessMarker` docstring notes that "today the silent fallback ... lets master, session, cookie, and localhost auth through without any check" — the past tense assumption implies the fix was applied alongside these tests. If the fix is ever reverted or partially landed, these tests will catch the regression.
