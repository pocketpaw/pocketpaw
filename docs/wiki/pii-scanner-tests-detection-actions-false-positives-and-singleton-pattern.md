---
{
  "title": "PII Scanner Tests: Detection, Actions, False Positives, and Singleton Pattern",
  "summary": "PocketPaw's PII scanner (`pocketpaw.security.pii`) detects sensitive personal information in text before it reaches logs, LLM context, or external storage. These tests validate detection of SSNs, emails, phones, credit cards, IP addresses, and dates of birth, verify that safe text generates no false positives, and confirm the singleton pattern for the default scanner instance.",
  "concepts": [
    "PII detection",
    "SSN",
    "email detection",
    "phone detection",
    "credit card",
    "IP address",
    "date of birth",
    "false positives",
    "mask action",
    "hash action",
    "singleton pattern",
    "security scanner"
  ],
  "categories": [
    "testing",
    "security",
    "PII protection",
    "test"
  ],
  "source_docs": [
    "dc38164798d9841c"
  ],
  "backlinks": null,
  "word_count": 492,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw processes messages from real users. Without a PII scanner, a user sending their phone number in a chat message could have it logged, stored in soul memory, or forwarded to an LLM provider. The scanner is a mandatory preprocessing step that intercepts this data before it leaves the local process.

## Detection Categories

Each detection class tests a specific PII type:

- **SSN** (`TestSSNDetection`): dashed format `123-45-6789`, tested both in isolation and surrounded by text to confirm the scanner preserves non-PII context.
- **Email** (`TestEmailDetection`): standard format and `+` tag addresses (e.g., `user+tag@example.com`), which are common in real addresses and must not confuse the pattern.
- **Phone** (`TestPhoneDetection`): US format with parentheses `(555) 123-4567` and dashes `555-123-4567`.
- **Credit Card** (`TestCreditCardDetection`): Visa and Mastercard number patterns.
- **IP Address** (`TestIPAddressDetection`): IPv4 dotted-quad format.
- **Date of Birth** (`TestDateOfBirthDetection`): dates preceded by context keywords like "DOB" or "born on" — bare dates without context keywords must NOT match, preventing false positives on regular date mentions.

## False Positive Prevention

`TestPhoneIPOverlap` is the most security-critical test class. Phone patterns and IP address patterns share digit-dot structure. Without word boundary guards:

- `192.168.1.1` could be misdetected as a phone number.
- `555-123-4567` could be misdetected as an IP address.
- A date like `12-05-2026` could trigger the phone pattern.

```python
def test_ip_address_not_detected_as_phone(scanner):
    results = scanner.scan("server at 192.168.1.1")
    assert not any(r.type == "PHONE" for r in results)
```

`TestSafeContent` ensures that normal messages, empty strings, and `None`-like inputs produce zero detections — preventing the scanner from blocking legitimate traffic.

## Actions: log, mask, hash

`TestActions` verifies the three response modes:

- **log**: preserves original text, attaches detection metadata for audit purposes.
- **mask**: replaces detected PII with a type placeholder like `[EMAIL]`.
- **hash**: replaces PII with a partial hash — allows de-duplication without exposing the value.
- **per-type override**: different PII types can use different actions in the same scan configuration.

The partial hash (not full SHA) is deliberate — a full hash would still allow identification if the PII value space is small (e.g., all possible SSNs).

## Multiple PII in One Message

`TestMultiplePII` verifies that a single message containing both an email and a phone number is fully processed — both are detected and all are replaced when masking is applied. A scanner that stops after the first hit would leave remaining PII exposed.

## Singleton Pattern

`TestSingleton` verifies the module-level default scanner:

- Successive calls to the factory return the same instance (memory efficiency for high-throughput agents).
- A `reset()` call creates a new instance (test isolation, config changes).

## Known Gaps

- IPv6 addresses are not tested.
- Non-US phone formats (international `+44`, `+1-800`) are not covered.
- No test for PII embedded in JSON structures — the scanner may not detect `{"ssn": "123-45-6789"}`.
- No test for Unicode homoglyph substitution in PII values (e.g., using Cyrillic `о` instead of Latin `o` in an email to bypass detection).