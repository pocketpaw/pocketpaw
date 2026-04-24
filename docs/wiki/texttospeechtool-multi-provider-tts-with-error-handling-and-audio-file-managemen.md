---
{
  "title": "TextToSpeechTool: Multi-Provider TTS with Error Handling and Audio File Management",
  "summary": "The `TextToSpeechTool` converts text to speech using configurable providers (OpenAI TTS, ElevenLabs) and saves audio files to a managed directory. Tests cover the tool's definition (name, trust level, parameters), provider-specific error paths for missing API keys, unknown provider rejection, successful ElevenLabs synthesis, API error handling, the `synthesize_speech` helper, and the audio directory creation utility.",
  "concepts": [
    "TextToSpeechTool",
    "ElevenLabs",
    "OpenAI_TTS",
    "synthesize_speech",
    "audio_directory",
    "_get_audio_dir",
    "trust_level",
    "httpx",
    "API_key_validation",
    "TTS"
  ],
  "categories": [
    "tool-system",
    "audio",
    "testing",
    "test"
  ],
  "source_docs": [
    "2de7fd958a3d2943"
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

## Overview

`TextToSpeechTool` (tool name: `text_to_speech`) is a standard-trust tool that synthesizes speech from text using either OpenAI's TTS API or ElevenLabs, saving the output as an audio file in a managed subdirectory of PocketPaw's config directory.

## Tool Definition

The tool has:
- `name`: `"text_to_speech"`
- `trust_level`: `"standard"` (not requiring elevated permissions)
- Required parameter: `"text"` (the content to synthesize)
- Optional parameter: `"voice"` (provider-specific voice ID or name)

## Error Paths for Missing API Keys

When no API key is configured for the selected provider, the tool returns a descriptive error string rather than raising:

- OpenAI without `openai_api_key`: Returns an error mentioning `"OpenAI"`
- ElevenLabs without `elevenlabs_api_key`: Returns an error mentioning `"ElevenLabs"`
- Unknown provider: Returns `"Unknown TTS provider"` error

These checks happen before any network call, preventing confusing HTTP 401 errors from propagating to the agent.

## Audio Directory Management

`_get_audio_dir()` returns `<config_dir>/generated/audio/` and creates it if it does not exist. The test uses `monkeypatch` to redirect `get_config_dir` to a temp path, then verifies the directory was created at the expected path:

```python
def test_get_audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketpaw.tools.builtin.voice.get_config_dir", lambda: tmp_path)
    d = _get_audio_dir()
    assert d.exists()
    assert d == tmp_path / "generated" / "audio"
```

## ElevenLabs TTS: Success Path

The success test mocks `httpx.AsyncClient` to return a response with `content = b"fake_audio_data_elevenlabs"`. The test verifies the tool:
1. Calls the ElevenLabs API endpoint via POST
2. Writes the audio bytes to a file in the managed audio directory
3. Returns a success message or file path to the agent

The mock patches both `get_settings` (to inject the API key) and `_get_audio_dir` (to write to a temp path), ensuring no real filesystem or network calls occur.

## ElevenLabs API Error Handling

`test_elevenlabs_tts_api_error` verifies that when `raise_for_status()` raises an `httpx.HTTPStatusError`, the tool catches it and returns an error string. This prevents unhandled exceptions from crashing the agent during a TTS call if the ElevenLabs API is down or rate-limiting.

## `synthesize_speech` Helper

`synthesize_speech` is a standalone async function that wraps the tool's execute path, returning the audio file path on success and `None` on any error (including when `execute()` itself returns an error string). This helper is used by other parts of PocketPaw that need TTS without going through the tool registry.

```python
async def test_synthesize_speech_returns_none_on_error():
    # Verifies None is returned when execute() fails

async def test_synthesize_speech_checks_execute_error_result():
    # Verifies None is returned when execute() returns an error string
```

## Known Gaps

No TODOs. OpenAI TTS success path tests are not present — only the error path for missing API key is tested. A full integration test for the OpenAI provider would require mocking `httpx.AsyncClient` similar to the ElevenLabs tests.