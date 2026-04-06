"""
Channel-aware message formatting.
Created: 2026-02-10

Converts standard Markdown to channel-native format and provides
LLM system-prompt hints per channel.
"""

from __future__ import annotations

import re

from pocketpaw.bus.events import Channel

# ---------------------------------------------------------------------------
# LLM system-prompt hints — injected into the system prompt per channel.
# These tell the LLM how to format its output natively for each channel,
# avoiding post-hoc regex conversion and entity-parse errors.
# Empty string → channel supports standard Markdown, no hint needed.
# ---------------------------------------------------------------------------
CHANNEL_FORMAT_HINTS: dict[Channel, str] = {
    Channel.WEBSOCKET: "",
    Channel.DISCORD: (
        "Format: Discord Markdown.\n"
        "- Bold: **text**, Italic: *text* or _text_, Strikethrough: ~~text~~\n"
        "- Code: `inline` or ```language\\nblock```. Headings: # ## ###\n"
        "- Links auto-embed; use <url> to suppress preview.\n"
        "- Lists: - or 1. work. Use \\n between paragraphs.\n"
        "- Max message length: 2000 chars. Split long responses into multiple paragraphs.\n"
        "- Avoid bare underscores in non-italic text (e.g. write `variable_name` in backticks)."
    ),
    Channel.MATRIX: "",
    Channel.WHATSAPP: (
        "Format: WhatsApp.\n"
        "- Bold: *text*, Italic: _text_, Strikethrough: ~text~, Code: ```code```\n"
        "- NO headings, NO [links](url), NO numbered lists, NO inline `code`.\n"
        "- Use blank lines between paragraphs for readability.\n"
        "- Use - for bullet lists (no nested lists).\n"
        "- Keep responses concise — mobile screens are small.\n"
        "- Escape formatting chars with \\ if they appear in normal text."
    ),
    Channel.SLACK: (
        "Format: Slack mrkdwn.\n"
        "- Bold: *text*, Italic: _text_, Strike: ~text~, Code: `inline` or ```block```\n"
        "- Links: <url|display text>. Do NOT use [text](url).\n"
        "- NO headings (#). Use *bold on its own line* as a section header.\n"
        "- Lists: use - or * (bullet) or 1. (numbered). Indent with spaces for nesting.\n"
        "- Blank lines between sections for readability.\n"
        "- Blockquotes: > text\n"
        "- Avoid bare underscores outside _italic_ — they break mrkdwn parsing."
    ),
    Channel.SIGNAL: (
        "Format: Plain text only.\n"
        "- NO formatting marks of any kind — no *, _, `, ~, #, []().\n"
        "- Use line breaks and blank lines to create visual structure.\n"
        "- Use CAPS or dashes for emphasis instead of bold/italic.\n"
        "- Use indentation (spaces) for lists and hierarchy.\n"
        "- Keep responses concise and scannable."
    ),
    Channel.TELEGRAM: (
        "Format: Telegram Markdown.\n"
        "- Bold: *text*, Italic: _text_, Code: `inline` or ```block```\n"
        "- Links: [display text](url)\n"
        "- NO headings (#) — use *bold* on its own line as a header.\n"
        "- Lists: use - for bullets. Numbered lists work as plain text (1. 2. 3.).\n"
        "- Use \\n between paragraphs for readability.\n"
        "- IMPORTANT: Escape _ inside words with \\_ (e.g. variable\\_name) or wrap in "
        "`backticks` — unmatched underscores cause parse errors.\n"
        "- Avoid nested or adjacent formatting (e.g. *_bold italic_* breaks).\n"
        "- Keep messages under 4096 chars. Split longer responses."
    ),
    Channel.TEAMS: (
        "Format: Microsoft Teams Markdown.\n"
        "- Bold: **text**, Italic: _text_, Code: `inline` or ```block```\n"
        "- Links: [text](url). Headings: # ## ### work.\n"
        "- Lists: - or 1. with blank line before the list.\n"
        "- Tables: | col | col | with header separator.\n"
        "- Use \\n between paragraphs. Blank lines matter for block elements.\n"
        "- Avoid bare underscores outside italic context."
    ),
    Channel.GOOGLE_CHAT: (
        "Format: Google Chat.\n"
        "- Bold: *text*, Italic: _text_, Strikethrough: ~text~, Code: `inline`\n"
        "- NO headings, NO [links](url) — URLs auto-link. NO code blocks.\n"
        "- Use blank lines between paragraphs.\n"
        "- Use - for bullet lists. Keep it simple and flat.\n"
        "- Avoid bare underscores in non-italic text."
    ),
    Channel.CLI: "",
    Channel.WEBHOOK: "",
    Channel.SYSTEM: "",
}

# Channels that support standard Markdown and need no conversion
_PASSTHROUGH_CHANNELS = frozenset(
    {
        Channel.WEBSOCKET,
        Channel.DISCORD,
        Channel.MATRIX,
        Channel.CLI,
        Channel.WEBHOOK,
        Channel.SYSTEM,
    }
)

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")


def _extract_code_blocks(text: str) -> tuple[str, list[str]]:
    """Replace code blocks with placeholders and return (text, blocks)."""
    blocks: list[str] = []

    def _replace(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"\x00CODE{len(blocks) - 1}\x00"

    return _CODE_BLOCK_RE.sub(_replace, text), blocks


def _restore_code_blocks(text: str, blocks: list[str]) -> str:
    """Restore placeholders to original code blocks."""
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00CODE{i}\x00", block)
    return text


# ---------------------------------------------------------------------------
# Per-channel converters
# ---------------------------------------------------------------------------
def _to_whatsapp(text: str) -> str:
    """Convert standard Markdown to WhatsApp format."""
    text, blocks = _extract_code_blocks(text)
    # Headings → bold line
    text = _HEADING_RE.sub(lambda m: f"*{m.group(2)}*", text)
    # Links → plain text (WhatsApp auto-links URLs)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    # Bold **x** → *x*
    text = _BOLD_RE.sub(r"*\1*", text)
    # Strikethrough ~~x~~ → ~x~
    text = _STRIKETHROUGH_RE.sub(r"~\1~", text)
    return _restore_code_blocks(text, blocks)


def _to_slack(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn."""
    text, blocks = _extract_code_blocks(text)
    # Headings → bold line
    text = _HEADING_RE.sub(lambda m: f"*{m.group(2)}*", text)
    # Links [text](url) → <url|text>
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    # Bold **x** → *x*
    text = _BOLD_RE.sub(r"*\1*", text)
    # Strikethrough ~~x~~ → ~x~
    text = _STRIKETHROUGH_RE.sub(r"~\1~", text)
    return _restore_code_blocks(text, blocks)


def _to_telegram(text: str) -> str:
    """Convert standard Markdown to Telegram Markdown."""
    text, blocks = _extract_code_blocks(text)
    # Headings → bold line
    text = _HEADING_RE.sub(lambda m: f"*{m.group(2)}*", text)
    # Bold **x** → *x*
    text = _BOLD_RE.sub(r"*\1*", text)
    # Strikethrough ~~x~~ → (not supported, strip)
    text = _STRIKETHROUGH_RE.sub(r"\1", text)
    # Links stay as [text](url) — Telegram supports them
    return _restore_code_blocks(text, blocks)


def _to_signal(text: str) -> str:
    """Convert standard Markdown to plain text for Signal."""
    text, blocks = _extract_code_blocks(text)
    # Headings → plain text with caps-style separator
    text = _HEADING_RE.sub(lambda m: m.group(2).upper(), text)
    # Links → text (url)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    # Strip bold
    text = _BOLD_RE.sub(r"\1", text)
    # Strip italic
    text = _ITALIC_RE.sub(r"\1", text)
    # Strip strikethrough
    text = _STRIKETHROUGH_RE.sub(r"\1", text)
    # Strip remaining code block markers from restored blocks
    restored = _restore_code_blocks(text, blocks)
    restored = re.sub(r"```\w*\n?", "", restored)
    return restored


def _to_teams(text: str) -> str:
    """Convert standard Markdown to Teams format.

    Teams supports standard Markdown, but we ensure compatibility.
    """
    # Teams handles standard MD well — minimal conversion
    return text


def _to_gchat(text: str) -> str:
    """Convert standard Markdown to Google Chat format."""
    text, blocks = _extract_code_blocks(text)
    # Headings → bold line
    text = _HEADING_RE.sub(lambda m: f"*{m.group(2)}*", text)
    # Links → plain text (Google Chat basic format)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    # Bold **x** → *x*
    text = _BOLD_RE.sub(r"*\1*", text)
    # Strikethrough ~~x~~ → ~x~
    text = _STRIKETHROUGH_RE.sub(r"~\1~", text)
    return _restore_code_blocks(text, blocks)


def _strip_markdown(text: str) -> str:
    """Fallback: strip all Markdown formatting."""
    text, blocks = _extract_code_blocks(text)
    text = _HEADING_RE.sub(lambda m: m.group(2), text)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    text = _STRIKETHROUGH_RE.sub(r"\1", text)
    restored = _restore_code_blocks(text, blocks)
    restored = re.sub(r"```\w*\n?", "", restored)
    return restored


# Dispatcher
_CONVERTERS: dict[Channel, callable] = {
    Channel.WHATSAPP: _to_whatsapp,
    Channel.SLACK: _to_slack,
    Channel.TELEGRAM: _to_telegram,
    Channel.SIGNAL: _to_signal,
    Channel.TEAMS: _to_teams,
    Channel.GOOGLE_CHAT: _to_gchat,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def convert_markdown(text: str, channel: Channel) -> str:
    """Convert standard Markdown to channel-native format.

    For channels that support standard Markdown (WebSocket, Discord, Matrix),
    returns text unchanged. For others, applies channel-specific conversion.

    Args:
        text: Standard Markdown text from the LLM.
        channel: Target channel.

    Returns:
        Text formatted for the target channel.
    """
    if not text or channel in _PASSTHROUGH_CHANNELS:
        return text

    converter = _CONVERTERS.get(channel, _strip_markdown)
    return converter(text)
