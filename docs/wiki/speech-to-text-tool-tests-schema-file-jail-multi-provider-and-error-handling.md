---
{
  "title": "Speech-to-Text Tool Tests: Schema, File Jail, Multi-Provider, and Error Handling",
  "summary": "Tests for PocketPaw's `SpeechToTextTool`, covering schema validation, file path jailing for security, size limits, successful transcription with both OpenAI Whisper and ElevenLabs providers, language parameter forwarding, and graceful handling of missing API keys and provider errors.",
  "concepts": [
    "SpeechToTextTool",
    "file jail",
    "OpenAI Whisper",
    "ElevenLabs",
    "trust_level",
    "audio transcription",
    "size limit",
    "path traversal prevention",
    "language parameter"
  ],
  "categories": [
    "testing",
    "built-in tools",
    "security",
    "test"
  ],
  "source_docs": [
    "ae528cb95db077c4"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's `SpeechToTextTool` converts audio files to text by delegating to a configurable STT provider (OpenAI Whisper or ElevenLabs). Because the tool accepts a file path from the LLM, it must apply strict security controls before touching the filesystem. This test file validates both the happy-path transcription flows and the security boundaries.

## Schema Validation

`TestSpeechToTextToolSchema` verifies the tool's name (`speech_to_text`), trust level (`standard`), required parameters (`audio_file`, `language`), and description. Schema correctness is a prerequisite for the LLM to invoke the tool correctly — a missing required field or wrong parameter name breaks the invocation before any business logic runs.

## File Jail

`test_stt_file_jail_rejects_outside_path` is the most important security test. The tool is given a path outside the permitted jail directory and must reject it. Without this guard, a compromised or hallucinating LLM could read arbitrary files from the host filesystem by disguising them as audio file paths. The jail enforces that the tool can only transcribe files within a designated directory.

## File Not Found and Size Limit

`test_stt_file_not_found` verifies the tool returns a clear error when the specified file does not exist. `test_stt_file_too_large` confirms that files exceeding the size limit are rejected before any bytes are sent to the API — protecting against both accidental large-file uploads and prompt-injection attacks via large audio files.

## Successful Transcription (Whisper)

`test_stt_success` and `test_stt_with_language` cover the Whisper happy path. The language test confirms the optional `language` parameter is forwarded to the provider, enabling better transcription accuracy for non-English audio.

`test_stt_empty_transcript` handles the edge case where the provider returns an empty string — not an error, but a signal that the audio was silent or unintelligible. The tool should return a structured "no speech detected" result rather than an empty success.

## Provider Error

`test_stt_api_error` verifies that provider-side failures (network errors, rate limits, invalid audio format) are caught and returned as structured error messages rather than unhandled exceptions.

## ElevenLabs Provider

Four ElevenLabs tests mirror the Whisper coverage:

- `test_elevenlabs_stt_success`: Basic transcription works.
- `test_elevenlabs_stt_with_language`: Language parameter is forwarded.
- `test_elevenlabs_stt_no_api_key`: Missing API key produces a clear error (not a crash).
- `test_elevenlabs_stt_api_error`: API failures are handled gracefully.

The parallel coverage for both providers ensures that switching the default STT provider does not introduce regressions in the error-handling paths.

## Known Gaps

The test suite does not cover streaming transcription (real-time audio input), which may be a future feature. No `TODO` annotations are visible in the AST.

```python
# File jail test pattern
async def test_stt_file_jail_rejects_outside_path(tmp_path):
    """Files outside the jail directory must be rejected."""
    # Attempts to transcribe /etc/passwd or equivalent
    result = await tool.run({"audio_file": "/etc/passwd"})
    assert "error" in result or "denied" in result.lower()
```
