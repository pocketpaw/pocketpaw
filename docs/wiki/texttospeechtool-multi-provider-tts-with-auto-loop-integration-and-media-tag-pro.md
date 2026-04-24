---
{
  "title": "TextToSpeechTool: Multi-Provider TTS with Auto-Loop Integration and Media Tag Protocol",
  "summary": "`TextToSpeechTool` converts text to audio via OpenAI TTS, ElevenLabs, or Sarvam AI Bulbul, saving output to `~/.pocketpaw/generated/audio/` with UUID filenames. A standalone `synthesize_speech()` helper and a `\u003c!-- media:path --\u003e` HTML comment protocol enable the agent loop to auto-play TTS responses without parsing tool output.",
  "concepts": [
    "TextToSpeechTool",
    "synthesize_speech",
    "media_tag_protocol",
    "OpenAI_TTS",
    "ElevenLabs",
    "Sarvam_Bulbul",
    "provider_routing",
    "UUID_filenames",
    "auto_TTS",
    "BaseTool"
  ],
  "categories": [
    "tools",
    "text-to-speech",
    "media-integrations",
    "audio"
  ],
  "source_docs": [
    "bfa6a497e97f3416"
  ],
  "backlinks": null,
  "word_count": 532,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`voice.py` (Phase 2 Integration Ecosystem) implements `text_to_speech`, a tool that converts text to speech audio files. The design goes beyond a simple API wrapper — it includes an auto-TTS integration protocol (`synthesize_speech` + media tags) that lets the agent loop attach voice to any response, not just explicit tool calls.

## Three-Provider Support

```python
if provider == "openai":
    return await self._tts_openai(text, voice)
elif provider == "elevenlabs":
    return await self._tts_elevenlabs(text, voice)
elif provider == "sarvam":
    return await self._tts_sarvam(text, voice)
```

**OpenAI TTS** (`tts-1` model) offers six voices: alloy, echo, fable, onyx, nova, shimmer. It's the default for English deployments.

**ElevenLabs** provides voice cloning and a larger library of natural-sounding voices identified by ID strings.

**Sarvam AI Bulbul** covers 11 Indian languages with 39 voices (named: Shubh, Kriti, Amol, Amartya, Diya, etc.). Bulbul is the only option for high-quality Indian language TTS — OpenAI's voices don't handle Indic scripts natively.

Provider selection is settings-driven, not parameter-driven — the same design philosophy as `SpeechToTextTool`.

## Media Tag Protocol

```python
match = re.search(r"<!-- media:([^>]+) -->", result)
if match:
    return match.group(1)
```

The tool embeds the output file path in an HTML comment (`<!-- media:/path/to/file.mp3 -->`) appended to the response text. This is a low-friction IPC protocol: the agent loop can scan any tool result for this pattern and trigger audio playback without knowing which tool produced it, and without a separate return channel. The `synthesize_speech()` standalone function uses exactly this extraction pattern.

## synthesize_speech() Standalone Helper

```python
async def synthesize_speech(text: str) -> str | None:
    tool = TextToSpeechTool()
    result = await tool.execute(text=text)
    if result.startswith("Error:"):
        logger.error("synthesize_speech failed: %s", result)
        return None
    match = re.search(r"<!-- media:([^>]+) -->", result)
    return match.group(1) if match else None
```

`synthesize_speech` is designed to be called by the agent loop for "auto-TTS" — automatically voicing agent responses when the user is in a voice session. It returns `None` on failure rather than raising, so the agent loop can continue silently if TTS is unavailable. The `Error:` prefix check relies on the `BaseTool._error()` convention — a stable string prefix that acts as a structured error signal.

## Audio Output Directory

```python
def _get_audio_dir() -> Path:
    d = get_config_dir() / "generated" / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

Audio files are saved to `~/.pocketpaw/generated/audio/` with UUID filenames, preventing collisions from concurrent TTS calls. The `mkdir(parents=True, exist_ok=True)` call is idempotent — safe to call on every invocation without checking existence first.

## Text Preprocessing

The `voice.py` import of `re` (regular expressions) suggests text preprocessing before API submission — likely stripping markdown formatting (bold, italics, code blocks) that would be read literally by TTS models ("asterisk asterisk bold word asterisk asterisk"). The exact preprocessing is in the `_tts_*` methods not fully shown.

## Known Gaps

- No text length enforcement before API submission; long texts may hit provider token limits silently.
- The `<!-- media: -->` tag is appended to the response text, which could interfere if the response is displayed directly in a non-HTML context.
- ElevenLabs voice IDs are opaque strings — there's no discovery mechanism to list available voices within the tool.
- No streaming TTS output — the entire audio file is generated before being returned, adding latency for long texts.
