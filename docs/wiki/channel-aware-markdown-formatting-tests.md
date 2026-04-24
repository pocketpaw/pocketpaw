---
{
  "title": "Channel-Aware Markdown Formatting Tests",
  "summary": "This test module validates `convert_markdown`, PocketPaw's channel-aware formatter that translates LLM-generated markdown into the native formatting of each messaging platform. Each channel has its own rendering rules — WhatsApp uses `*bold*`, Slack uses `\u003curl|label\u003e` hyperlinks, Signal strips all formatting — and the tests prevent these channel-specific transformations from regressing.",
  "concepts": [
    "convert_markdown",
    "CHANNEL_FORMAT_HINTS",
    "WhatsApp formatting",
    "Slack formatting",
    "Telegram formatting",
    "Signal formatting",
    "code block protection",
    "Channel enum",
    "passthrough channels",
    "message bus",
    "LLM output formatting"
  ],
  "categories": [
    "testing",
    "message formatting",
    "channel adapters",
    "test"
  ],
  "source_docs": [
    "c740a1c384f46854"
  ],
  "backlinks": null,
  "word_count": 477,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Channel-Aware Formatting Exists

LLMs generate markdown by default. Sending `**bold**` to WhatsApp renders as literal asterisks; to Slack it renders as nothing (Slack expects `*bold*`). Left unformatted, every platform either shows raw syntax characters or drops the formatting silently, degrading user experience. `convert_markdown` translates once, at the bus boundary, so agent code never needs to know which channel it is responding to.

## CHANNEL_FORMAT_HINTS Validation

`CHANNEL_FORMAT_HINTS` is a dict mapping each `Channel` enum value to a hint string that can be injected into the system prompt to tell the LLM to generate format-friendly output. Three invariants are tested:

- Every hint must be a `str` (no `None` values that would crash string interpolation).
- Passthrough channels (`Channel.MATRIX`) must have an empty hint — no conversion happens, so no hint is needed.
- Non-passthrough channels (WEBSOCKET, WHATSAPP, SLACK, SIGNAL, TELEGRAM, DISCORD) must have non-empty hints.

## Passthrough Channels

Discord, Matrix, CLI, SYSTEM, and WEBSOCKET return text unchanged. This is tested with `@pytest.mark.parametrize` over the full list. Empty strings return early for all channels.

## Per-Channel Conversion Rules

### WhatsApp
| Input | Expected |
|-------|----------|
| `**hello**` | `*hello*` |
| `## Section Title` | `*Section Title*` |
| `[Google](https://google.com)` | `Google (https://google.com)` |
| `~~removed~~` | `~removed~` |

Code blocks are preserved verbatim — WhatsApp renders monospace code blocks natively.

### Slack
Similar to WhatsApp for bold and strikethrough, but links use Slack's native `<url|label>` format rather than text+URL.

### Telegram
Links are kept in Markdown format (Telegram's Bot API accepts `[text](url)`). Strikethrough is stripped rather than converted because Telegram's strikethrough syntax (`~text~`) conflicts with WhatsApp's.

### Signal
Signal has no rich text support. All formatting is stripped: bold markers removed, headings uppercased, links shown as `text (url)`, code fence backticks removed (code content preserved).

### Teams
Teams accepts standard Markdown, so text passes through unchanged.

### Google Chat
Bold converts like WhatsApp (`*bold*`); links become `text (url)` like Signal.

## Code Block Protection

A `@pytest.mark.parametrize` test runs over WHATSAPP, SLACK, TELEGRAM, and GOOGLE_CHAT and asserts that content inside a fenced code block is not modified. This prevents the formatter from corrupting code samples that happen to contain markdown-like syntax:

```
```
**not bold** [not a link](url)
```
```

After conversion, `**not bold**` must still appear inside the block.

## Realistic Output Test

`test_llm_response_whatsapp` assembles a full LLM response — heading, bold step labels, a bash code block, a link, and strikethrough — and verifies each transformed element in one pass. `test_plain_text_unchanged` confirms that plain text (no markdown) passes through all channels unmodified, which is important because over-eager regexes could corrupt ordinary prose.

## Known Gaps

No `TODO` or `FIXME` markers. Tests do not cover nested formatting (e.g., bold inside a link label), which could produce platform-specific artifacts. The Matrix channel is listed as passthrough but has no explicit test for its hint being empty.