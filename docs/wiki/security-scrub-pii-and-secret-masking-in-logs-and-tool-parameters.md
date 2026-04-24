---
{
  "title": "Security Scrub: PII and Secret Masking in Logs and Tool Parameters",
  "summary": "The logging scrub test suite validates PocketPaw's defense-in-depth approach to preventing secrets and personally identifiable information from appearing in logs, audit trails, and tool execution records. Tests cover pattern-based field masking, nested dict traversal, audit log fallback behavior, and dangerous-command detection logging introduced in security sprint cluster C.",
  "concepts": [
    "security scrubbing",
    "PII masking",
    "scrub_params",
    "audit log",
    "secret detection",
    "pattern matching",
    "nested dict traversal",
    "dangerous command detection",
    "tool parameters",
    "defense in depth"
  ],
  "categories": [
    "security",
    "logging",
    "test"
  ],
  "source_docs": [
    "1f6b71b26d95d63d"
  ],
  "backlinks": null,
  "word_count": 516,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw runs user-supplied tools and logs their parameters for debugging and audit purposes. Without scrubbing, API keys, passwords, and auth tokens submitted as tool parameters would appear in plaintext in log files and audit records. The `pocketpaw.security.scrub` module provides `scrub_params()` — a recursive masking function — and the test suite (added in security sprint cluster C, issues #890 and #893) validates its behavior exhaustively.

## Known Secret Field Names

`TestScrubParams.test_known_secret_field_masked` verifies that `openai_api_key` (and by extension all fields in the known-secrets list) is replaced with `"***"` while adjacent benign fields like `prompt` are preserved. This allowlist approach ensures zero false negatives for the most dangerous fields.

## Pattern-Based Matching

`test_pattern_matched_fields_masked` validates the regex-or-suffix pattern layer, which catches fields whose names contain `_api_key`, `_token`, `_secret`, `password`, or `Authorization`. This catches credentials that are not in the known-secrets list but follow naming conventions — for example, a custom `slack_token` or `client_secret` passed from a third-party tool.

The `harmless` field in the same test confirms that non-matching fields are untouched, preventing false positives that would break tool execution.

## Nested Dict Traversal

`test_nested_dict_is_scrubbed` passes a params dict that contains another dict as a value (e.g., `{"config": {"openai_api_key": "sk-x", "temperature": 0.5}}`). The scrubber must recurse into nested dicts and mask secrets at any depth, while leaving numeric values like `temperature` intact. Without recursive traversal, structured tool configs that embed credentials inside sub-objects would bypass scrubbing entirely.

## Edge Cases

- **Empty dict**: returns an empty dict without error.
- **Non-string values**: numeric, boolean, and `None` values in secret-named fields are masked too (they would be coerced to strings in logs anyway).
- **List values**: the test suite verifies behavior when a secret field holds a list.

## Audit Log Fallback

The security sprint also added tests for audit log fallback behavior — when the primary audit log (`audit.jsonl`) is not writable (e.g., disk full, permissions error), the system falls back to a secondary location or logs a warning rather than silently dropping the audit record or crashing the request handler.

## Dangerous-Command Detection

Issue #893 added a check that logs a warning when a tool execution involves a dangerous shell command (e.g., `rm -rf`, `chmod 777`, pipe chains to `sh`). The test verifies that the warning is emitted at the correct log level and contains enough context (command fragment, tool name) for an operator to investigate.

## Why This Matters

PocketPaw's multi-tenant architecture means a single deployment may serve multiple users whose API keys flow through the same logging pipeline. A single missed scrub could expose one user's credentials to another user's log export or to a shared observability stack. The defense-in-depth approach — known list plus pattern matching plus recursive traversal — minimizes the blast radius of any single omission.

## Known Gaps

- The scrubber operates on dict keys only; if a secret value appears as a dict value under a non-secret key (e.g., `{"notes": "key is sk-abcdef"}`), it is not masked. Content-level PII scanning is out of scope.
- The dangerous-command detection is pattern-based and can be bypassed by encoding or splitting commands across arguments.