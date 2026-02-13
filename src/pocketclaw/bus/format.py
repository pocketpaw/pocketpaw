"""
Channel-aware message formatting.
Created: 2026-02-10

Converts standard Markdown to channel-native format and provides
LLM system-prompt hints per channel.
"""

from __future__ import annotations

import re

from pocketclaw.bus.events import Channel

# ---------------------------------------------------------------------------
# LLM system-prompt hints (one sentence each)
# Empty string → channel supports standard Markdown, no hint needed.
# ---------------------------------------------------------------------------
CHANNEL_FORMAT_HINTS: dict[Channel, str] = {
    Channel.WEBSOCKET: "",
    Channel.DISCORD: "",
    Channel.MATRIX: "",
    Channel.WHATSAPP: (
        "Format: WhatsApp. Use *bold*, _italic_, ~strikethrough~, ```code```. "
        "No headings, no links, no numbered lists. Keep it simple."
    ),
    Channel.SLACK: (
        "Format: Slack mrkdwn. Use *bold*, _italic_, ~strike~, `code`, ```code```. "
        "Links: <url|text>. No headings — use *bold* on its own line."
    ),
    Channel.SIGNAL: (
        "Format: plain text. No formatting marks. Use line breaks and spacing for structure."
    ),
    Channel.TELEGRAM: (
        "Format: Telegram Markdown. Use *bold*, _italic_, `code`, ```code```. Links: [text](url)."
    ),
    Channel.TEAMS: (
        "Format: Microsoft Teams. Use **bold**, _italic_, `code`, ```code```. "
        "Links: [text](url). Headings work as standard Markdown."
    ),
    Channel.GOOGLE_CHAT: (
        "Format: Google Chat. Use *bold*, _italic_, ~strikethrough~, `code`. "
        "No headings, no links. Keep it simple."
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
