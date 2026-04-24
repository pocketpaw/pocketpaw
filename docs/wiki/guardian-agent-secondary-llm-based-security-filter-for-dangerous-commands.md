---
{
  "title": "Guardian Agent: Secondary LLM-Based Security Filter for Dangerous Commands",
  "summary": "The Guardian Agent provides a second-opinion AI security layer that evaluates commands and actions against a SAFE/DANGEROUS classification before execution. It sits above the deterministic regex rails, handling ambiguous cases where pattern matching alone cannot determine intent.",
  "concepts": [
    "Guardian Agent",
    "AI security filter",
    "secondary LLM check",
    "SAFE/DANGEROUS classification",
    "dangerous command detection",
    "circular import",
    "deferred initialization",
    "audit integration",
    "defense in depth",
    "agent security"
  ],
  "categories": [
    "security",
    "agent runtime",
    "ai safety"
  ],
  "source_docs": [
    "c17b489324086b0c"
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

## Why a Second LLM Layer?

Deterministic security rules have a fundamental limitation: they match what they know. A regex that blocks `rm -rf /` will not catch `find / -delete` or a creatively obfuscated variant. The Guardian Agent addresses this by delegating final judgment on ambiguous commands to a secondary LLM call with a strict security-focused system prompt.

This two-layer design is deliberate: the regex rails (`rails.py`) provide fast, zero-cost blocking for clearly dangerous patterns. The Guardian handles the gray zone — commands that are syntactically unusual, contextually suspicious, or novel enough to evade static patterns.

## Architecture

`GuardianAgent` integrates with the audit system and the compiled dangerous patterns from `rails.py`. When invoked, it formats the candidate command and relevant context into a prompt that instructs the LLM to respond with exactly one word: `SAFE` or `DANGEROUS`. The binary classification eliminates ambiguous LLM responses that might be exploited via prompt injection.

The `_ensure_client()` async method handles lazy initialization of the LLM client. This deferred setup prevents import-time failures when the LLM provider credentials are not yet configured — a common scenario during initial setup or testing.

## Circular Import Defense

The module includes an explicit comment explaining why `pocketpaw.config` is not imported at module level:

> Deferred import — `pocketpaw.config` imports `validate_external_url` from `pocketpaw.security.url_validators`, and `pocketpaw.security.__init__` eagerly imports this module. Importing `get_settings` at module load time creates a circular import during `config.py` initialization.

This is a real, documented engineering constraint — the deferred import is not laziness but a deliberate solution to a circular dependency formed by the security package's init chain.

## Input Truncation

The module defines a maximum character limit for content passed to the Guardian. This guards against two failure modes: (1) extremely long commands exhausting LLM context windows and producing unreliable classifications, and (2) adversarial inputs designed to overwhelm or distract the security-focused prompt with irrelevant content.

## Integration with AuditLogger

Every Guardian evaluation — both SAFE and DANGEROUS verdicts — is logged to the audit trail via `get_audit_logger()`. This creates an evidence chain: if a command is later found to have caused harm despite a SAFE verdict, the audit log captures that the Guardian evaluated it and what it saw.

## Singleton Access

`get_guardian()` returns a module-level singleton, ensuring only one Guardian instance exists per process. This matters because the underlying LLM client may maintain connection pools or rate-limit state.

## Known Gaps

- **LLM availability dependency**: If the configured LLM provider is unreachable, the Guardian cannot evaluate commands. The current behavior on client failure needs a clearly documented fallback policy (fail-open vs. fail-closed).
- **Classification latency**: Each Guardian call adds an LLM round-trip to the critical path of command execution. There is no caching of prior classifications for identical or structurally similar commands.
- **No adversarial prompt testing**: The Guardian's own system prompt could itself be a target for injection. There is no documented red-team evaluation of the classification prompt's robustness.