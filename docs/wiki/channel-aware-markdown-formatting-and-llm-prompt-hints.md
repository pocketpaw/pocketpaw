---
{
  "title": "Channel-Aware Markdown Formatting and LLM Prompt Hints",
  "summary": "format.py provides two capabilities: a `convert_markdown()` function that translates standard Markdown output from the LLM into each channel's native formatting syntax, and a `CHANNEL_FORMAT_HINTS` dictionary that injects per-channel formatting instructions into the LLM system prompt so the model generates natively formatted text in the first place.",
  "concepts": [
    "convert_markdown",
    "CHANNEL_FORMAT_HINTS",
    "code block preservation",
    "placeholder sentinel",
    "Slack mrkdwn",
    "WhatsApp formatting",
    "Telegram Markdown",
    "passthrough channels",
    "ui-spec",
    "LLM system prompt hints",
    "regex substitution"
  ],
  "categories": [
    "bus",
    "formatting",
    "markdown",
    "channel-adapters"
  ],
  "source_docs": [
    "af617781d089063a"
  ],
  "backlinks": null,
  "word_count": 515,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Different messaging platforms use incompatible formatting syntaxes. WhatsApp uses `*bold*`, Slack uses `*bold*` but different link syntax, Telegram uses `*bold*` but has strict entity parsing rules, Signal renders no formatting at all, and Google Chat has yet another variant. format.py centralizes all of this complexity so channel adapters never contain inline regex logic.

## Two-Tier Strategy

The module uses a two-tier approach to formatting:

**Tier 1 — LLM hints**: `CHANNEL_FORMAT_HINTS` contains per-channel system prompt fragments injected upstream before the LLM generates its response. If the model follows these instructions, `convert_markdown()` receives already-correct text and makes few or no changes. This eliminates most post-hoc regex replacement errors.

**Tier 2 — Post-hoc conversion**: `convert_markdown()` acts as a safety net for cases where the LLM ignores the hint, generates standard Markdown, or the hint is not injected. It applies a chain of regex substitutions to transform the text to the correct format.

## Code Block Preservation

All converters begin by calling `_extract_code_blocks()`, which replaces fenced code blocks (` ``` ... ``` `) with `\x00CODE{n}\x00` placeholders before any other regex runs. This prevents formatting regexes from corrupting code content — for example, `_BOLD_RE` would turn `**kwargs` inside a code block into bold text. After all other substitutions complete, `_restore_code_blocks()` puts the original code blocks back.

The null-byte character (`\x00`) is chosen as the placeholder sentinel because it cannot appear in valid UTF-8 chat messages, making false matches impossible.

## Per-Channel Converters

- **WhatsApp**: `**bold**` → `*bold*`, `~~strike~~` → `~strike~`, `# Heading` → `*Heading*`, `[text](url)` → `text (url)` (WhatsApp auto-links raw URLs)
- **Slack**: Same bold/strike mapping, but links become `<url|text>` (Slack mrkdwn format). Headings become bold lines.
- **Telegram**: Bold and heading handling; strikethrough is stripped (Telegram Markdown v1 does not support it); links remain in `[text](url)` format since Telegram supports them.
- **Signal**: Full Markdown stripping — all bold, italic, heading, link, and strikethrough markers are removed, leaving plain text. Restored code blocks have their ` ``` ` fence markers stripped too.
- **Teams**: Effectively a passthrough — Teams renders standard Markdown natively.
- **Google Chat**: Same pattern as WhatsApp with `*bold*` and `~strike~`.

## Passthrough Channels

`_PASSTHROUGH_CHANNELS` is a frozen set containing channels that natively render standard Markdown (WebSocket/dashboard, Discord, Matrix, CLI, Webhook, System). `convert_markdown()` returns text unchanged for these channels, avoiding unnecessary processing.

## Channel Format Hints Detail

The `CHANNEL_FORMAT_HINTS` dictionary contains rich, multi-line strings for each channel. The WebSocket hint is the most complex: it describes a full `ui-spec` JSON DSL that allows the LLM to generate interactive dashboard widgets (charts, metric cards, grids, forms) rendered by the frontend. Channels with empty string hints (Matrix, CLI) accept standard Markdown with no special guidance.

## Known Gaps

- Telegram's `_to_telegram()` converter does not escape problematic underscores inside words (e.g., `variable_name` should become `` `variable_name` `` or `variable\_name`). The hint instructs the LLM to do this, but the post-hoc converter does not enforce it — partial entity parse failures can still occur.
- There is no converter for Discord, Matrix, Google Chat (partially covered), or A2A channels — these rely entirely on the LLM hint.