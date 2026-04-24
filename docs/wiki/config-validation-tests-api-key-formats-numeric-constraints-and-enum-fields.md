---
{
  "title": "Config Validation Tests: API Key Formats, Numeric Constraints, and Enum Fields",
  "summary": "This test module covers three tiers of Settings validation: the `validate_api_key()` function that checks provider-specific key prefixes and warns on mismatches, Pydantic `gt`/`ge` constraints on numeric fields that prevent nonsensical configurations, and `Literal`-typed enum fields for provider selection. Together they ensure that misconfigured credentials and out-of-range settings fail loudly at startup rather than silently at runtime.",
  "concepts": [
    "validate_api_key",
    "Settings",
    "Pydantic",
    "ValidationError",
    "API key prefix",
    "Anthropic",
    "OpenAI",
    "Telegram",
    "numeric constraints",
    "Literal types",
    "enum validation",
    "compaction",
    "rate limiting"
  ],
  "categories": [
    "configuration",
    "testing",
    "validation",
    "security",
    "test"
  ],
  "source_docs": [
    "28351fe74cca9651"
  ],
  "backlinks": null,
  "word_count": 553,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's `Settings` class is the single source of truth for runtime configuration. Bad configuration — a pasted Anthropic key into an OpenAI field, a zero-value concurrency limit, or an unrecognized TTS provider — will cause silent failures deep in the call stack unless caught early. This test file validates that the `config.py` module catches these problems at construction time.

## API Key Format Validation (`TestValidateApiKey`)

`validate_api_key(field_name, value)` returns a `(is_valid: bool, warning: str)` tuple. It never blocks a save — it only warns — because operators may paste keys incrementally or use custom proxy keys that don't follow standard prefixes.

**Anthropic keys** must start with `sk-ant-`. The tests confirm that `sk-ant-api03-abc123` passes, while `sk-wrong-abc123` fails with a message containing both the expected format (`sk-ant-...`) and an actionable hint (`Double-check for typos or truncation`).

**OpenAI keys** accept both the legacy `sk-` prefix and the newer `sk-proj-` prefix (tested separately). The `test_anthropic_key_catches_openai_prefix` test is particularly important: it ensures that accidentally pasting an OpenAI key into the Anthropic field is caught, preventing cryptic authentication errors from the Anthropic SDK.

**Telegram tokens** follow the format `<bot_id>:<AAH...suffix>`. The tests cover missing colons, wrong character classes after the colon, and the correct format.

**Passthrough cases**: `None`, empty strings, whitespace-only values, and unknown field names all return `(True, "")`. This is intentional — optional fields start empty, and the validator should not force operators to fill in every credential before they can start the agent.

**Whitespace trimming**: The tests for leading/trailing whitespace confirm that keys are validated after stripping, preventing copy-paste-with-trailing-newline failures that are notoriously hard to debug.

**Return type contract**: `test_return_type_is_tuple` explicitly asserts the return type is a 2-tuple, protecting callers that destructure the result (`is_valid, warning = validate_api_key(...)`) from `AttributeError` if the function is ever accidentally changed to return a single value.

## Numeric Field Constraints (`TestNumericFieldConstraints`, issue #629)

Pydantic's `gt` (greater-than) and `ge` (greater-than-or-equal) field constraints make illegal values fail at `Settings()` construction with a `ValidationError`. The tested fields and their constraints are:

| Field | Constraint | Reason |
|---|---|---|
| `compaction_recent_window` | `gt=0` | Zero means no history window, breaking compaction |
| `compaction_char_budget` | `gt=0` | Zero budget makes the system prompt empty |
| `compaction_summary_chars` | `gt=0` | Zero produces empty summaries |
| `session_token_ttl_hours` | `gt=0` | Zero TTL expires tokens immediately, locking out all users |
| `api_rate_limit_per_key` | `gt=0` | Zero allows no requests at all |
| `media_max_file_size_mb` | `ge=0` | Zero is valid (disables uploads); negative is not |
| `max_concurrent_conversations` | `gt=0` | Zero would deadlock the semaphore |

Each field has three tests: zero rejected, negative rejected, positive accepted. This pattern ensures no fence-post errors where zero is accidentally accepted.

## Enum Field Validation (`TestEnumFieldValidation`, issue #638)

Fields like `whatsapp_mode`, `tts_provider`, and `stt_provider` use `Literal` types. Accepting arbitrary strings would cause `KeyError` or `AttributeError` deep in the provider dispatch logic. The tests verify:
- Valid values (`"personal"`, `"business"`, `"openai"`, `"elevenlabs"`, `"sarvam"`) are accepted.
- Invalid values and close typos (`"openai_"`, `"elevenlabs_tts"`) are rejected with `ValidationError`.
- Empty string is accepted for optional provider fields (means "not configured").

## Known Gaps

No TODO or FIXME markers are present. The `TestEnumFieldValidation` docstring notes it covers `whatsapp_mode`, `tts_provider`, and `stt_provider` — other `Literal`-typed fields (if any were added after issue #638) may not yet have corresponding tests.