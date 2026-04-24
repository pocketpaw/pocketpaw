---
{
  "title": "SpeechToTextTool: Multi-Provider Audio Transcription with Path Jail Enforcement",
  "summary": "`SpeechToTextTool` transcribes audio files to text via OpenAI Whisper, ElevenLabs, or Sarvam AI Saaras, with automatic provider selection from settings. A path-safety check (`is_safe_path`) prevents directory traversal attacks before any file is read, and transcripts are written to a versioned output directory.",
  "concepts": [
    "SpeechToTextTool",
    "OpenAI_Whisper",
    "ElevenLabs_STT",
    "Sarvam_Saaras",
    "is_safe_path",
    "file_jail",
    "provider_routing",
    "audio_transcription",
    "BCP47_language_codes",
    "transcript_persistence"
  ],
  "categories": [
    "tools",
    "speech-to-text",
    "media-integrations",
    "security"
  ],
  "source_docs": [
    "26eea07b204084c0"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`stt.py` (Phase 4 Media Integrations) implements the `speech_to_text` tool, which converts audio files to text. It supports three STT providers — OpenAI Whisper, ElevenLabs, and Sarvam AI Saaras — and routes to the correct one based on the configured provider in settings. Saaras is the standout for Indian language support, offering 23 languages and five output modes not available in Whisper or ElevenLabs.

## Path Jail Enforcement

```python
from pocketpaw.tools.fetch import is_safe_path

audio_path = Path(audio_file).expanduser().resolve()
jail = get_settings().file_jail_path.resolve()
if not is_safe_path(audio_path, jail):
    return self._error("Access denied: audio file is outside allowed directory")
```

The `is_safe_path` check (imported from `fetch.py`) uses `Path.is_relative_to()` on the fully resolved path, preventing `../` traversal tricks. This guard exists because the `audio_file` parameter is a user-supplied string — without it, an agent could be tricked (via prompt injection) into reading arbitrary files by framing them as audio inputs.

## Provider Routing

```python
provider = settings.stt_provider  # "openai", "elevenlabs", or "sarvam"
if provider == "openai":
    return await self._stt_openai(audio_path, language)
elif provider == "elevenlabs":
    return await self._stt_elevenlabs(audio_path, language)
elif provider == "sarvam":
    return await self._stt_sarvam(audio_path, language, mode)
```

Provider selection is settings-driven, not parameter-driven. This means the agent doesn't choose the provider; the deployment administrator does. The `language` and `mode` parameters are forwarded to whichever provider is active, though `mode` is only meaningful for Sarvam (Whisper and ElevenLabs ignore it).

## Sarvam Saaras Modes

The `mode` parameter for Sarvam supports five options: `transcribe` (default), `translate` (to English), `verbatim` (word-for-word without normalization), `translit` (romanized transliteration), and `codemix` (mixed-language output). This range covers multilingual use-cases common in Indian deployments where a speaker might mix Hindi and English in a single sentence.

## Transcript Persistence

```python
def _get_transcripts_dir() -> Path:
    d = get_config_dir() / "generated" / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

Transcripts are written to `~/.pocketpaw/generated/transcripts/` with a UUID filename, ensuring no collision between concurrent invocations. The directory is created on first use (`mkdir(parents=True, exist_ok=True)` is idempotent). Persisting transcripts to disk allows the user to retrieve them later even if the conversation is cleared.

## API Upload Pattern

All three providers use a multipart form upload pattern rather than base64 encoding. The `httpx` client sends the audio file as a binary stream, which is memory-efficient for large files (WAV files especially can be hundreds of megabytes). The `is_safe_path` check ensures the file being streamed is within the jail before the httpx client opens it.

## Known Gaps

- ElevenLabs STT is listed as a supported provider but the implementation (`_stt_elevenlabs`) was not fully shown — its availability in production is unclear.
- No audio file size limit is enforced before upload; large files could exhaust memory or exceed API limits silently.
- The `mode` parameter is silently ignored for non-Sarvam providers — there is no warning when a caller sets `mode="translate"` with provider `openai`.
- UUID-named transcript files have no index or manifest, making retrieval difficult without a directory listing.
