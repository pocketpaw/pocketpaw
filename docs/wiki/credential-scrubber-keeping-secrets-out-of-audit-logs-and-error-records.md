---
{
  "title": "Credential Scrubber: Keeping Secrets Out of Audit Logs and Error Records",
  "summary": "This module provides `scrub_params()` and `scrub_command()` — two functions that mask credential-like data before it enters audit logs, system logger fallbacks, and dangerous-command records. It combines an explicit secret field list from `credentials.py` with heuristic field-name patterns to catch secrets regardless of how they are named.",
  "concepts": [
    "credential scrubbing",
    "scrub_params",
    "scrub_command",
    "SECRET_FIELDS",
    "audit log security",
    "field-name heuristics",
    "inline credential masking",
    "recursive dict scrubbing",
    "syslog protection",
    "secret field detection"
  ],
  "categories": [
    "security",
    "observability",
    "agent runtime"
  ],
  "source_docs": [
    "6d9749cdf3663440"
  ],
  "backlinks": null,
  "word_count": 441,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Scrubbing Is Separate from Redaction

The codebase has both `redact.py` (output-level, user-facing) and `scrub.py` (internal, audit-facing). The distinction matters: `redact.py` protects end users from seeing secrets in responses; `scrub.py` protects the audit log and system logger from persisting secrets in operational records.

This separation was motivated by two specific audit log issues (#890 and #893): tool parameters containing API keys were being written verbatim to `audit.jsonl`, and when the audit writer itself failed, the fallback `logger.critical()` call was echoing those same params to syslog. `scrub.py` was added to intercept at both points.

## Secret Field Detection: Two Layers

`scrub_params()` uses two complementary strategies to identify secret fields:

**Layer 1 — Explicit list**: `SECRET_FIELDS` imported from `pocketpaw.credentials` contains the canonical set of field names that always hold secrets (`api_key`, `token`, `secret`, etc.). This list is maintained alongside the credential model, so additions to the credential schema automatically propagate to scrubbing.

**Layer 2 — Heuristic patterns**: `_SECRET_NAME_PATTERNS` catches field names that look like secrets even if not in the explicit list:

```python
_SECRET_NAME_PATTERNS = (
    re.compile(r"(?i).*api[_-]?key$"),
    re.compile(r"(?i).*token$"),
    re.compile(r"(?i).*secret$"),
    re.compile(r"(?i).*password$"),
    re.compile(r"(?i)^authorization$"),
)
```

This dual approach catches both known fields and dynamically named parameters that plugins or third-party tools might introduce.

## scrub_params: Recursive Dict Masking

`scrub_params()` accepts `Any` — dict, list, primitive — and returns a deep copy with secret-named fields replaced by `"***"`. Handling lists and nested dicts is essential because tool params frequently contain nested structures (e.g., `{"headers": {"Authorization": "Bearer TOKEN"}}`). A shallow scrubber would miss nested secrets.

## scrub_command: Inline Credential Masking

`scrub_command()` handles a different threat surface: shell commands passed as strings to the dangerous-command audit log. Commands like `curl -H 'Authorization: Bearer sk-ant-...' https://api.example.com` embed credentials inline. The function applies regex patterns against credential-looking substrings while preserving surrounding text so operators can still understand what command was attempted.

## The `scrub_event_dict` Export

The `AuditLogger.log()` method calls `scrub_event_dict()` (which wraps `scrub_params()`) on every audit event before writing. This ensures the scrubbing is not optional or caller-dependent — it is baked into the audit pipeline.

## Known Gaps

- **Heuristic false positives**: A field named `customer_email_token_id` would match the `.*token$` pattern and be masked even if it contains a non-sensitive identifier. There is no whitelist mechanism to override heuristic matches.
- **String values only**: The scrubber masks values associated with secret-named keys. A string value that *is* a raw API key stored under a non-secret-looking key name (e.g., `payload`) would not be caught by `scrub_params` — only `scrub_command`'s inline patterns would help there.
- **No scrubbing in non-audit logs**: Other `logger.*` calls throughout the codebase that pass tool params directly to standard Python logging are not automatically scrubbed.