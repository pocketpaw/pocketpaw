---
{
  "title": "PII Detection and Masking: Regex-Based Personal Data Protection Layer",
  "summary": "This module provides a configurable PII detection and masking system using pre-compiled regex patterns for common personal data types (emails, phone numbers, SSNs, etc.). It supports per-type action overrides — mask, redact, hash, or passthrough — and integrates as both a standalone scanner and a subsystem of the audit logger's PII filter.",
  "concepts": [
    "PII detection",
    "PIIType",
    "PIIAction",
    "PIIScanner",
    "PIIScanResult",
    "data masking",
    "regex patterns",
    "personal data protection",
    "GDPR",
    "audit integration",
    "singleton pattern",
    "test reset"
  ],
  "categories": [
    "security",
    "privacy",
    "agent runtime"
  ],
  "source_docs": [
    "0ca2827fd841e473"
  ],
  "backlinks": null,
  "word_count": 527,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why PII Protection in an Agent Runtime?

AI agents frequently handle sensitive user data: users paste emails, share phone numbers, or describe situations involving personal identifiers. This data flows through memory persistence, audit logs, and tool call parameters. Without explicit filtering, PII gets stored in places designed for operational data (logs, memories) where it is hard to purge and easy to leak.

`pii.py` was introduced as part of security hardening and follows the same pre-compiled regex pattern established by `injection_scanner.py`.

## PIIType: Recognized Data Categories

```python
class PIIType(StrEnum):
    # EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS, etc.
```

Each type represents a distinct regex pattern family. Enumerating types explicitly (rather than using a single catch-all pattern) serves two purposes: (1) different types warrant different actions — an email in a log might be masked, while an SSN should always be redacted; (2) callers can inspect which PII types were found and make context-sensitive decisions.

## PIIAction: Per-Type Response Policy

`PIIAction` defines what happens when PII is detected:

- **MASK**: Replace with a placeholder like `[EMAIL]` — preserves context while hiding the value
- **REDACT**: Replace with `***` — no hint of the original
- **HASH**: Replace with a deterministic hash — allows correlation without exposure
- **PASSTHROUGH**: Log the detection but leave the value intact — for cases where PII is expected and legitimate

The `PIIScanner.__init__` accepts a `default_action` and a `type_actions` override dict, enabling fine-grained policy. For example, the audit logger might mask emails but hash IP addresses for network analysis.

## Pre-Compiled Pattern Architecture

Patterns are compiled at class instantiation, not per-scan call. This is the same design as `injection_scanner.py` and for the same reason: regex compilation is expensive; scanning is on the hot path for every message processed.

## PIIScanResult: Rich Detection Report

`PIIScanResult` includes:
- `sanitized_text`: the text after applying actions to all matches
- A list of `PIIMatch` objects, each capturing the type, original value, action taken, and span
- `has_pii` property: quick boolean check for callers that just need a yes/no
- `pii_types_found` property: set of all detected types

This richness allows callers to log what was found, audit PII exposure patterns over time, or surface warnings to users.

## Singleton with Test Reset

`get_pii_scanner()` initializes the scanner from application settings on first call. `reset_pii_scanner()` clears the singleton for testing — without this, test suites that test different configurations would share state across test cases.

## Integration Points

The scanner integrates at two levels: `AuditLogger.enable_pii_filter()` wraps it around every audit event, and higher-level code can call `get_pii_scanner().scan(text)` before persisting to memory or returning responses.

## Known Gaps

- **Regex coverage is limited by known formats**: PII that does not match a recognized pattern (e.g., a non-standard national ID format) passes through undetected. The scanner has no ML-based fallback for unfamiliar PII structures.
- **No GDPR deletion integration**: Detected PII is masked in transit but there is no pipeline to retroactively find and purge PII from existing memory or log files (beyond the CLI `scan_memory_for_pii` report).
- **Hash action uses unkeyed hash**: If HASH mode uses a predictable hash function without a secret salt, determined attackers can reverse known values by brute force.