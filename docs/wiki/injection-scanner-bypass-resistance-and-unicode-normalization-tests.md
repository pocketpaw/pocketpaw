---
{
  "title": "Injection Scanner Bypass Resistance and Unicode Normalization Tests",
  "summary": "This test module verifies that `InjectionScanner` is resistant to obfuscation techniques that attempt to evade heuristic pattern matching: fullwidth Unicode characters, zero-width characters, homoglyphs, and byte-order marks. It also tests that the deep scan LLM path is only triggered when the heuristic flags content, and that the `_normalize` method handles all Unicode edge cases correctly.",
  "concepts": [
    "injection scanner bypass",
    "Unicode normalization",
    "NFKC",
    "zero-width characters",
    "fullwidth characters",
    "BOM",
    "deep scan",
    "heuristic fallback",
    "sanitization",
    "_normalize",
    "homoglyph",
    "threat detection"
  ],
  "categories": [
    "testing",
    "security",
    "prompt injection",
    "Unicode",
    "bypass resistance",
    "test"
  ],
  "source_docs": [
    "d77905425e0fcb2d"
  ],
  "backlinks": null,
  "word_count": 459,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

A naive injection scanner that only matches exact ASCII strings is easily defeated by Unicode substitution. An attacker who knows the scanner checks for `"ignore previous instructions"` can instead write it using fullwidth Unicode characters (`ｉｇｎｏｒｅ ｐｒｅｖｉｏｕｓ ｉｎｓｔｒｕｃｔｉｏｎｓ`) or insert zero-width joiners between letters to break naive pattern matches. These bypass tests ensure `InjectionScanner` normalizes input before matching.

## `TestUnicodeNormalization`

All four tests in this class pass non-ASCII representations of known attack phrases and assert that `InjectionScanner` still detects them:

- **`test_fullwidth_characters_normalized`** — fullwidth Unicode letters (U+FF01–U+FF60) are NFKC-normalized to their ASCII equivalents before pattern matching. Fullwidth "ignore previous instructions" must be detected.
- **`test_zero_width_chars_stripped`** — zero-width space (U+200B) inserted between letters (`i\u200bnore`) must be stripped before matching.
- **`test_zero_width_joiner_stripped`** — zero-width joiner (U+200D) has the same effect.
- **`test_bom_stripped`** — byte-order mark (U+FEFF) prepended to a payload must be stripped. BOMs can appear in text extracted from Word documents or PDFs.

## `TestStandardDetection`

Parametrized tests covering a broad matrix of known attack payloads and expected threat levels (`SUSPICIOUS` or `CRITICAL`), alongside a set of safe content examples that must return `SAFE`. This provides regression coverage as new patterns are added to the scanner.

## `TestSanitization`

Three tests verify sanitization output behavior:
- Delimiter attack strings (`<|im_start|>system`) must be stripped from sanitized output.
- `<|im_start|>` tags must be removed.
- `[INST]`/`[/INST]` tags must be removed.

Sanitization serves a dual purpose: it signals to the agent that the content was suspicious, and it removes the structurally dangerous tokens that could confuse the model's prompt parser.

## `TestDeepScanConditions`

- **`test_safe_content_skips_deep_scan`** — content that passes heuristic screening entirely skips the LLM deep scan call. Without this guard, every message processed by the agent would incur an extra API call, multiplying latency and cost.
- **`test_deep_scan_fallback_no_api_key`** — the async deep scan falls back to the heuristic result when no API key is configured. This allows the scanner to operate in offline or keyless mode without raising exceptions.

## `TestNormalizeMethod`

Unit tests for `_normalize()` as a standalone method, covering:
- Zero-width space (U+200B) stripped
- Zero-width non-joiner (U+200C) stripped
- Zero-width joiner (U+200D) stripped
- Word joiner (U+2060) stripped
- BOM (U+FEFF) stripped
- NFKC normalization of fullwidth characters to ASCII
- Normal ASCII text returned unchanged
- Empty string returned unchanged

Testing `_normalize` in isolation separates the normalization logic from the detection logic, making it easier to add new Unicode ranges without needing to construct full injection payloads.

## Known Gaps

No tests cover homoglyph substitution using Cyrillic or Greek characters that visually resemble Latin letters (e.g., Cyrillic `а` instead of Latin `a`). No tests cover RTL override characters (U+202E) that can visually reverse text direction. These are known bypass vectors for Unicode-aware scanners and represent an incomplete area of coverage.