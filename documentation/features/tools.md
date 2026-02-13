# Built-in Tools

PocketPaw includes a suite of built-in tools beyond the core file/shell/browser tools. Each tool integrates with the [Tool Policy](tool-policy.md) system for access control.

## Web Search

Multi-provider web search with normalized result format.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `web_search_provider` | string | `"tavily"` | Provider: `tavily`, `brave`, or `parallel` |
| `tavily_api_key` | string | `null` | Tavily search API key |
| `brave_search_api_key` | string | `null` | Brave Search API key |
| `parallel_api_key` | string | `null` | Parallel AI API key |

Returns up to 10 results per query, each with title, URL, and content snippet.

**Tool policy group:** `group:search`

---

## URL Extract

Extract clean text/markdown from web pages.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `url_extract_provider` | string | `"auto"` | Provider: `auto`, `parallel`, or `local` |
| `parallel_api_key` | string | `null` | Parallel AI API key |

- **parallel** — Uses Parallel AI for high-quality extraction
- **local** — Uses `html2text` (optional dep under `[extract]` extra)
- **auto** — Tries parallel first, falls back to local

Accepts single or multiple URLs. Results capped at 50,000 characters per extraction.

**Tool policy group:** `group:search`

---

## Research

Multi-step research pipeline that chains Web Search → URL Extract → LLM summarization.

### Depth Levels

| Level | Sources | Description |
|-------|---------|-------------|
| `quick` | 3 | Fast overview from top results |
| `standard` | 5 | Balanced depth and speed |
| `deep` | 10 | Thorough research with many sources |

### Pipeline

1. **Search** — Queries web search for relevant pages
2. **Extract** — Fetches and extracts content from top URLs
3. **Summarize** — LLM synthesizes findings into a coherent answer with citations

Optionally saves research results to memory for future reference.

**Tool policy group:** `group:research`

---

## Image Generation

Generate images via Google Gemini, saved locally.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `google_api_key` | string | `null` | Google API key (for Gemini) |
| `image_model` | string | `"gemini-2.0-flash-exp"` | Google image generation model |

**Optional dependency:** `pip install pocketpaw[image]` (installs `google-genai`)

Images are saved as PNG to `~/.pocketclaw/generated/`. Supports aspect ratios: 1:1, 16:9, 9:16.

**Tool policy group:** `group:media`

---

## Voice / Text-to-Speech

Convert text to speech via OpenAI TTS or ElevenLabs.

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `tts_provider` | string | `"openai"` | Provider: `openai` or `elevenlabs` |
| `tts_voice` | string | `"alloy"` | Voice name |
| `openai_api_key` | string | `null` | OpenAI API key (also used for TTS) |
| `elevenlabs_api_key` | string | `null` | ElevenLabs API key |

### OpenAI Voices

`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`

Audio saved as MP3 to `~/.pocketclaw/generated/audio/`.

**Tool policy group:** `group:voice`

---

## Task Delegation

Delegate complex sub-tasks to Claude Code CLI as a subprocess.

**Requires:** Claude Code CLI installed globally (`npm install -g @anthropic-ai/claude-code`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `timeout` | 300s | Max execution time (capped at 600s) |

The delegation system:
1. Checks if Claude Code CLI is available on PATH
2. Spawns a subprocess with `claude --print --output-format json`
3. Parses JSON output; falls back to raw text if parsing fails
4. Returns the result to the calling agent

**Trust level:** Critical (this tool can execute arbitrary code via the subprocess)

**Tool policy group:** `group:delegation`

---

## Skill Generation

Create reusable agent skills at runtime. Skills are stored as `SKILL.md` files with YAML frontmatter.

### Naming Rules

Skill names must match: `^[a-z][a-z0-9_-]{0,64}$`

### Storage

Skills are saved to `~/.pocketclaw/skills/{skill_name}/SKILL.md` and automatically reloaded into the skill loader.

### SKILL.md Format

```markdown
---
name: my-skill
description: A brief description
trigger: keyword or phrase
---

# Skill instructions here

When triggered, do X, Y, Z...
```

**Tool policy group:** `group:skills`

---

## Tool Policy Groups Summary

| Group | Tools |
|-------|-------|
| `group:search` | `web_search`, `url_extract` |
| `group:media` | `image_generate` |
| `group:voice` | `text_to_speech` |
| `group:research` | `research` |
| `group:delegation` | `delegate_to_claude_code` |
| `group:skills` | `create_skill` |
| `group:gmail` | `search_gmail`, `read_email`, `send_email`, `gmail_list_labels`, `gmail_create_label`, `gmail_modify`, `gmail_trash`, `gmail_batch_modify` |
| `group:calendar` | `calendar_list`, `calendar_create`, `calendar_prep` |

See [Tool Policy](tool-policy.md) for how to use these groups in allow/deny lists.

## Implementation

| File | Description |
|------|-------------|
| `tools/builtin/web_search.py` | `WebSearchTool` — Tavily/Brave/Parallel search |
| `tools/builtin/url_extract.py` | `UrlExtractTool` — URL content extraction |
| `tools/builtin/research.py` | `ResearchTool` — Multi-step research pipeline |
| `tools/builtin/image_gen.py` | `ImageGenerateTool` — Google Gemini image gen |
| `tools/builtin/voice.py` | `TextToSpeechTool` — OpenAI/ElevenLabs TTS |
| `tools/builtin/delegate.py` | `DelegateToClaudeCodeTool` — Subprocess delegation |
| `tools/builtin/skill_gen.py` | `CreateSkillTool` — Runtime skill creation |
| `agents/delegation.py` | `ExternalAgentDelegate` — Delegation subprocess manager |

## Tests

- `tests/test_web_search.py`
- `tests/test_url_extract.py`
- `tests/test_research.py`
- `tests/test_image_gen.py`
- `tests/test_voice.py`
- `tests/test_delegation.py`
- `tests/test_skill_gen.py`
