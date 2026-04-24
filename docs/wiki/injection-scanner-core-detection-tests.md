---
{
  "title": "Injection Scanner Core Detection Tests",
  "summary": "This test suite validates `InjectionScanner`'s heuristic-based prompt injection detection across seven attack categories: instruction overrides, persona hijacks, delimiter attacks, data exfiltration commands, jailbreak attempts, tool abuse, and safe content. It also tests sanitization output wrapping and the singleton accessor pattern.",
  "concepts": [
    "InjectionScanner",
    "prompt injection",
    "ThreatLevel",
    "heuristic detection",
    "persona hijack",
    "delimiter attack",
    "data exfiltration",
    "jailbreak",
    "tool abuse",
    "sanitization",
    "deep scan",
    "singleton",
    "get_injection_scanner"
  ],
  "categories": [
    "testing",
    "security",
    "prompt injection",
    "agent safety",
    "attack detection",
    "test"
  ],
  "source_docs": [
    "512771ecc3b963bb"
  ],
  "backlinks": null,
  "word_count": 538,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Prompt injection is the primary attack surface for AI agents that process user-controlled text from external sources (email, web pages, documents). `InjectionScanner` is PocketPaw's defense layer — it classifies incoming text as `SAFE`, `SUSPICIOUS`, or `CRITICAL` using pattern matching, and optionally wraps flagged content in a sanitization envelope that tells the agent not to follow embedded instructions.

The test suite in `test_injection_scanner.py` catalogs the attack patterns the scanner must recognize and establishes a false-positive baseline for legitimate requests.

## Attack Category Coverage

### `TestInstructionOverrides`
Tests direct instruction manipulation phrases:
- "ignore previous instructions"
- "disregard prior context"
- "forget your training"
- "new instructions"
- "system: you are"

These are the most common prompt injection vectors found in the wild. An attacker embedding any of these in an email or document body attempts to override the agent's system prompt mid-conversation.

### `TestPersonaHijacks`
Tests identity replacement attempts:
- "you are now", "act as", "pretend to be", "roleplay"

Persona hijacks attempt to replace the agent's identity with a permissive alter-ego that will comply with requests the original agent would refuse.

### `TestDelimiterAttacks`
Tests structural injection via special tokens:
- System code blocks (``` ```system ``` ```)
- `<|im_start|>` tags (ChatML format)
- `[INST]` / `[/INST]` tags (Llama format)
- `<|sys|>` tags

These attacks exploit the model's knowledge of prompt formatting conventions. By injecting the structural markers that separate system, user, and assistant turns, an attacker attempts to insert synthetic system-level instructions.

### `TestDataExfil`
Tests data exfiltration commands:
- "send to webhook"
- "curl [url] api_key"

These patterns attempt to use the agent's tool access to transmit sensitive data to attacker-controlled endpoints.

### `TestJailbreak`
Tests capability unlocking phrases:
- "DAN mode", "developer mode", "bypass safety"

Jailbreak attempts frame the attack as enabling a special "unrestricted" mode that supposedly exists inside the model.

### `TestToolAbuse`
Tests command injection:
- `rm -rf`, "backdoor"

These patterns in user-controlled content might cause the agent to execute destructive system commands via Bash tools.

## Safe Content Baseline

`TestSafeContent` establishes that normal questions, code-related questions, empty strings, and typical coding requests are NOT flagged. False positives are as harmful as false negatives — an overly aggressive scanner would block legitimate requests and make the agent unusable.

## Sanitization Wrapping

`TestSanitization` verifies that flagged content is wrapped in a sanitization envelope (the exact format is implementation-defined but tests assert the original content is preserved inside the wrapper). Safe content must NOT be wrapped — unnecessary wrapping adds noise to the agent's context.

## Deep Scan and Singleton

- **`test_deep_scan_fallback_no_api_key`** — when no API key is available, `deep_scan()` falls back to the heuristic result rather than failing. This ensures the scanner degrades gracefully in keyless environments.
- **`test_deep_scan_safe_content_skips_llm`** — content that passes heuristic screening does not trigger an LLM API call. This prevents unnecessary API usage and latency for the common case.
- **`test_get_injection_scanner_singleton`** — `get_injection_scanner()` returns the same instance on repeated calls, consistent with the health engine singleton pattern.

## Known Gaps

No tests cover multi-language injection (attacks in non-English languages that map to the same semantic intent). No tests cover gradual injection across multiple messages. The `ThreatLevel` enum values are used in tests but the scoring thresholds between `SUSPICIOUS` and `CRITICAL` are not explicitly tested.