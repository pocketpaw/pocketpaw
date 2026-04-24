---
{
  "title": "Sarvam AI Integration Tests — Translation, TTS, STT, and OCR",
  "summary": "This comprehensive test file covers PocketPaw's integration with Sarvam AI's multilingual APIs: text translation, text-to-speech (TTS), speech-to-text (STT), and vision OCR. Tests validate API key gating, SDK error propagation, audio encoding, file-based I/O, provider routing, policy group membership, config field defaults, and tool registration.",
  "concepts": [
    "Sarvam AI",
    "TranslateTool",
    "TextToSpeechTool",
    "SpeechToTextTool",
    "OCR",
    "multilingual",
    "TTS provider",
    "STT provider",
    "base64 audio",
    "httpx",
    "sarvamai SDK",
    "policy group",
    "tool registration"
  ],
  "categories": [
    "testing",
    "integrations",
    "multilingual",
    "voice and audio",
    "test"
  ],
  "source_docs": [
    "62232e67cdebc3f8"
  ],
  "backlinks": null,
  "word_count": 634,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_sarvam.py` was created 2026-02-16 and covers four Sarvam AI–backed tools: `TranslateTool`, `TextToSpeechTool`, `SpeechToTextTool`, and a Vision OCR tool. Sarvam AI specializes in Indian-language models; this integration brings multilingual translation, native TTS voices, and audio transcription to PocketPaw agents. All external calls are mocked.

## Shared Test Infrastructure

`_mock_settings(**overrides)` builds a fully configured settings mock with Sarvam-specific defaults: API key, TTS model/speaker/language, STT model, and provider selections for TTS, STT, and OCR. Individual tests override specific fields (e.g., `sarvam_api_key=None` to test missing-key behavior, `tmp_path` for `file_jail_path`).

`_fake_to_thread(func, /, *args, **kwargs)` is a drop-in for `asyncio.to_thread` that calls the synchronous function directly, avoiding thread pool overhead in tests and making assertions on call arguments easier.

## TranslateTool Tests

- **Tool definition** — `name == "translate"`, `trust_level == "standard"`, required params are `text` and `target_language`.
- **Missing API key** — returns an error string mentioning `SARVAM_API_KEY`, not an exception. This is important because agents need to surface actionable configuration guidance, not stack traces.
- **Empty/whitespace text** — returns an error; prevents sending blank payloads to the Sarvam API.
- **Happy path** — mocks `sarvamai.SarvamAI` and asserts the translated Hindi text appears in the result along with the target language code.
- **Formal mode** — asserts `"formal"` appears in the result when `mode="formal"` is passed.
- **SDK errors** — both generic `RuntimeError` and simulated HTTP 429 cause the tool to return an `"error"` string rather than raising.

## TextToSpeechTool (Sarvam TTS)

TTS output is audio bytes encoded as base64 in the Sarvam SDK's `audios` list. Tests:

- **Missing API key** — error returned without calling the SDK.
- **Happy path** — mocks the SDK response with base64-encoded audio, asserts `"Audio generated"`, `"300 bytes"`, and a `<!-- media:` tag appear. The media tag is the mechanism by which the dashboard renders audio inline.
- **Bytes response** — verifies the tool handles the base64 decode path correctly when `audios` contains a base64 string.
- **Custom speaker** — verifies call arguments are forwarded to `text_to_speech.convert`.
- **SDK not installed** — `ImportError` during `to_thread` results in an error string, not a crash.
- **Unknown TTS provider** — if `tts_provider` is set to an unrecognized value, the tool returns an error mentioning `"sarvam"`.

## SpeechToTextTool (Sarvam STT)

STT uses a direct HTTP POST via `httpx.AsyncClient` rather than the Sarvam SDK, because the SDK's STT interface was not available at integration time. Tests:

- **Missing API key** — error returned.
- **File not found** — audio file path does not exist; returns `"not found"` error.
- **Happy path** — mocks `httpx.AsyncClient.post` with a 200 response containing `{"transcript": "यह एक टेस्ट है"}` and asserts the Hindi transcript appears in the result.
- **Transliteration mode** — `mode="translit"` causes romanized output; asserts the mode label appears in the result metadata.
- **HTTP error** — non-200 response is surfaced as an error.
- **No speech detected** — API returns empty transcript; handled gracefully.
- **Unknown provider** — returns an error.

## Vision OCR Tests

OCR uses file upload (PDF and image formats). Tests cover:

- Missing API key, file not found, unsupported format, happy path, PDF support.
- OpenAI-backed OCR rejects PDF (a known provider limitation).
- SDK not installed and unknown provider error paths.

## Policy, Config, and Registration Tests

- **`TestSarvamPolicy`** — asserts `translate` belongs to the `voice` policy group and that the `translate` tool is correctly allowed/denied based on the active policy.
- **`TestSarvamConfig`** — asserts default config values are set and that TTS provider descriptions include `"sarvam"`.
- **`TestSarvamRegistration`** — verifies `TranslateTool` appears in lazy imports and is importable, confirming it will be discoverable by the tool registry.

## Known Gaps

No `TODO` or `FIXME` markers. The `test_pdf_support` and `test_openai_rejects_pdf` tests document a deliberate provider asymmetry that is not currently resolved — OCR provider selection for PDFs may need explicit routing logic.
