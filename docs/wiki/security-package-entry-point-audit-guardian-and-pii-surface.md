---
{
  "title": "Security Package Entry Point: Audit, Guardian, and PII Surface",
  "summary": "The pocketpaw.security package re-exports three security subsystems through its __init__.py: AuditLogger for structured event logging with severity levels, GuardianAgent for content policy enforcement, and PIIScanner for detecting and acting on personally identifiable information in agent inputs and outputs.",
  "concepts": [
    "AuditLogger",
    "AuditEvent",
    "AuditSeverity",
    "GuardianAgent",
    "PIIScanner",
    "PIIType",
    "PIIAction",
    "PIIScanResult",
    "security package",
    "PII detection",
    "policy enforcement",
    "trust level"
  ],
  "categories": [
    "security",
    "audit",
    "pii"
  ],
  "source_docs": [
    "e1e059680047f274"
  ],
  "backlinks": null,
  "word_count": 407,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`pocketpaw/security/__init__.py` consolidates PocketPaw's three-layer security surface into a single importable namespace. The package is the entry point for any code that needs audit logging, content policy enforcement, or PII detection, following PocketPaw's convention of keeping security concerns in a dedicated explicit module rather than scattering them across feature code.

## Exported Symbols

```python
from pocketpaw.security.audit import AuditEvent, AuditLogger, AuditSeverity, get_audit_logger
from pocketpaw.security.guardian import GuardianAgent, get_guardian
from pocketpaw.security.pii import PIIAction, PIIScanner, PIIScanResult, PIIType, get_pii_scanner
```

### audit — Structured Security Event Logging

`AuditLogger` records security-relevant events as structured `AuditEvent` objects with `AuditSeverity` levels. This is distinct from Python's standard `logging` module: audit events are persisted separately so they can be reviewed independently of application logs. Typical events include tool invocation with elevated trust, credential access attempts, and policy violations.

`AuditSeverity` provides tiered severity (INFO, WARNING, ERROR, CRITICAL) that maps to both human review priorities and alerting thresholds.

### guardian — Policy Enforcement Agent

`GuardianAgent` is PocketPaw's content policy enforcement layer. It sits in the request path to enforce configurable trust boundaries — for example, blocking tools from being invoked by channels with insufficient trust level, or preventing agents from accessing files outside their designated working directories. `get_guardian()` returns a process singleton.

The guardian pattern exists because security rules scattered across individual tool implementations are hard to audit and easy to bypass. Centralizing enforcement means policy changes propagate instantly to all tools.

### pii — PII Detection and Remediation

`PIIScanner` detects personally identifiable information in text using pattern matching and NLP heuristics. `PIIType` enumerates the categories it detects (email, phone, SSN, credit card, etc.). `PIIScanResult` contains the detected spans and their types.

`PIIAction` defines what to do when PII is detected: `ALLOW`, `REDACT`, `BLOCK`, or `LOG`. This allows different policies for different contexts: agent-internal memory might allow PII storage, but outbound channel messages might redact it automatically.

## Why a Flat Re-Export?

The flat `__init__.py` re-export means callers write:

```python
from pocketpaw.security import get_pii_scanner, get_audit_logger
```

rather than importing from the individual submodules. This makes imports stable against future internal reorganization.

## Known Gaps

- **No injection point documented**: The `__init__.py` does not document where `GuardianAgent` and `PIIScanner` are injected into the agent request path. New developers would need to trace call sites to understand where security checks happen.
- **All symbols eagerly imported**: Any import error in `audit.py`, `guardian.py`, or `pii.py` will prevent the entire `pocketpaw.security` namespace from loading — including the other two working subsystems.