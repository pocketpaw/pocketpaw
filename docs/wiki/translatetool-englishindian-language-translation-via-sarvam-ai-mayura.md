---
{
  "title": "TranslateTool: English–Indian Language Translation via Sarvam AI Mayura",
  "summary": "`TranslateTool` integrates Sarvam AI's Mayura translation model to translate text between English and 22 Indian languages, with four register modes from formal to code-mixed. It enforces a 2,000-character input cap and requires a Sarvam API key from settings.",
  "concepts": [
    "TranslateTool",
    "Sarvam_AI",
    "Mayura_model",
    "BCP47_codes",
    "code_mixed",
    "Indian_languages",
    "translation_modes",
    "auto_detection",
    "api_key_requirement",
    "BaseTool"
  ],
  "categories": [
    "tools",
    "translation",
    "indian-languages",
    "media-integrations"
  ],
  "source_docs": [
    "a83e3695b255ace6"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`translate.py` (created 2026-02-16) adds Indian language translation as a first-class capability in PocketPaw. This is a deliberate product decision: the Sarvam AI integration targets a deployment context (Indian enterprise and consumer users) where English-only agents are insufficient. The tool uses Sarvam's Mayura model, which specializes in Indic languages rather than the broader multilingual models from Google or DeepL.

## Language Coverage

The tool supports 22 Indian languages plus English, enumerated in the description string:
Hindi, Bengali, Tamil, Telugu, Marathi, Gujarati, Kannada, Malayalam, Odia, Punjabi, Assamese, Urdu, Nepali, Sanskrit, Sindhi, Kashmiri, Konkani, Dogri, Bodo, Maithili, Manipuri, Santali. Languages are identified by BCP-47 codes (e.g., `hi-IN`, `ta-IN`, `bn-IN`) rather than ISO 639-1, because BCP-47 includes the region subtag needed to disambiguate language variants (e.g., `en-IN` for Indian English vs `en-US`).

## Register Modes

```python
"mode": {"description": "formal | modern-colloquial | classic-colloquial | code-mixed"}
```

Four translation registers cover distinct use-cases:
- **formal**: Government documents, business correspondence
- **modern-colloquial**: Everyday conversational text, chat messages
- **classic-colloquial**: Traditional literary register
- **code-mixed**: Text that alternates between Hindi (or another Indic language) and English within sentences — extremely common in WhatsApp and social media contexts

The code-mixed mode is why a general-purpose translator wouldn't work here: detecting and preserving English segments within a Hindi sentence requires a model trained on Hinglish and similar mixed-language data.

## Input Cap

The `text` parameter is described as "max 2000 chars." This cap exists because Sarvam's API has its own token limits, and long texts that exceed those limits silently fail or return truncated translations. Documenting the 2,000-character limit in the schema gives the LLM the information it needs to chunk longer texts before calling the tool.

## Auto-Detection

```python
source_language: str = "auto"
```

The source language defaults to `auto`, which delegates language detection to Sarvam's model. Auto-detection is convenient but less reliable for short texts (a two-word phrase could plausibly belong to several languages). For production deployments with known source languages, callers should specify explicitly.

## API Key Requirement

Unlike the Reddit tools, translation requires a Sarvam API key:

```python
settings = get_settings()
api_key = settings.sarvam_api_key
if not api_key:
    return self._error("Sarvam API key not configured.")
```

The early-exit pattern surfaces a clear configuration error rather than a confusing HTTP 401. This is important for first-time setup: the agent can tell the user exactly what's missing.

## Known Gaps

- No character-count enforcement in the tool itself — the 2,000-char limit is advisory (in the description) but not validated before the API call.
- Translation back from all 22 languages to English is supported, but inter-Indian-language pairs (e.g., Tamil to Hindi) depend on whether Sarvam's Mayura model supports them — this is not documented in the tool.
- No fallback provider if Sarvam is unavailable (compare to WebSearchTool which supports three providers).
