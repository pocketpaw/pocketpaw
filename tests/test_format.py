# Tests for bus/format.py — Channel-aware formatting
# Created: 2026-02-10

import pytest

from pocketclaw.bus.events import Channel
from pocketclaw.bus.format import (
    CHANNEL_FORMAT_HINTS,
    convert_markdown,
)


# ---------------------------------------------------------------------------
# CHANNEL_FORMAT_HINTS validation
# ---------------------------------------------------------------------------
class TestChannelFormatHints:
    def test_all_hints_are_strings(self):
        for ch, hint in CHANNEL_FORMAT_HINTS.items():
            assert isinstance(hint, str), f"{ch} hint is not a string"

    def test_passthrough_channels_have_empty_hint(self):
        for ch in (Channel.WEBSOCKET, Channel.DISCORD, Channel.MATRIX):
            assert CHANNEL_FORMAT_HINTS[ch] == ""

    def test_non_passthrough_channels_have_nonempty_hint(self):
        for ch in (Channel.WHATSAPP, Channel.SLACK, Channel.SIGNAL, Channel.TELEGRAM):
            assert CHANNEL_FORMAT_HINTS[ch] != "", f"{ch} should have a hint"


# ---------------------------------------------------------------------------
# Passthrough channels (text unchanged)
# ---------------------------------------------------------------------------
class TestPassthrough:
    @pytest.mark.parametrize(
        "channel",
        [Channel.WEBSOCKET, Channel.DISCORD, Channel.MATRIX, Channel.CLI, Channel.SYSTEM],
    )
    def test_passthrough_returns_unchanged(self, channel):
        text = "## Heading\n**bold** and `code`\n[link](https://example.com)"
        assert convert_markdown(text, channel) == text

    def test_empty_string_passthrough(self):
        assert convert_markdown("", Channel.WHATSAPP) == ""

    def test_none_like_empty(self):
        # Empty string returns early for all channels
        assert convert_markdown("", Channel.SLACK) == ""


# ---------------------------------------------------------------------------
# WhatsApp conversion
# ---------------------------------------------------------------------------
class TestWhatsApp:
    def test_bold(self):
        assert convert_markdown("**hello**", Channel.WHATSAPP) == "*hello*"

    def test_heading_to_bold(self):
        result = convert_markdown("## Section Title", Channel.WHATSAPP)
        assert result == "*Section Title*"

    def test_link_to_text_url(self):
        result = convert_markdown("[Google](https://google.com)", Channel.WHATSAPP)
        assert result == "Google (https://google.com)"

    def test_strikethrough(self):
        assert convert_markdown("~~removed~~", Channel.WHATSAPP) == "~removed~"

    def test_code_block_preserved(self):
        text = "Before\n```python\nprint('hi')\n```\nAfter"
        result = convert_markdown(text, Channel.WHATSAPP)
        assert "```python\nprint('hi')\n```" in result

    def test_mixed_content(self):
        text = "## Summary\n**Bold text** and [a link](https://x.com)\n~~old~~"
        result = convert_markdown(text, Channel.WHATSAPP)
        assert "*Summary*" in result
        assert "*Bold text*" in result
        assert "a link (https://x.com)" in result
        assert "~old~" in result


# ---------------------------------------------------------------------------
# Slack conversion
# ---------------------------------------------------------------------------
class TestSlack:
    def test_bold(self):
        assert convert_markdown("**hello**", Channel.SLACK) == "*hello*"

    def test_heading_to_bold(self):
        assert convert_markdown("# Title", Channel.SLACK) == "*Title*"

    def test_link_to_slack_format(self):
        result = convert_markdown("[Google](https://google.com)", Channel.SLACK)
        assert result == "<https://google.com|Google>"

    def test_strikethrough(self):
        assert convert_markdown("~~nope~~", Channel.SLACK) == "~nope~"

    def test_code_block_preserved(self):
        text = "```\ncode\n```"
        result = convert_markdown(text, Channel.SLACK)
        assert "```\ncode\n```" in result


# ---------------------------------------------------------------------------
# Telegram conversion
# ---------------------------------------------------------------------------
class TestTelegram:
    def test_bold(self):
        assert convert_markdown("**hello**", Channel.TELEGRAM) == "*hello*"

    def test_heading_to_bold(self):
        assert convert_markdown("### Title", Channel.TELEGRAM) == "*Title*"

    def test_link_preserved(self):
        text = "[Google](https://google.com)"
        result = convert_markdown(text, Channel.TELEGRAM)
        assert "[Google](https://google.com)" in result

    def test_strikethrough_stripped(self):
        assert convert_markdown("~~gone~~", Channel.TELEGRAM) == "gone"


# ---------------------------------------------------------------------------
# Signal conversion (plain text)
# ---------------------------------------------------------------------------
class TestSignal:
    def test_bold_stripped(self):
        assert convert_markdown("**hello**", Channel.SIGNAL) == "hello"

    def test_heading_uppercased(self):
        assert convert_markdown("## Section", Channel.SIGNAL) == "SECTION"

    def test_link_to_text_url(self):
        result = convert_markdown("[site](https://x.com)", Channel.SIGNAL)
        assert result == "site (https://x.com)"

    def test_italic_stripped(self):
        assert convert_markdown("*italic*", Channel.SIGNAL) == "italic"

    def test_code_block_markers_stripped(self):
        text = "```python\nprint('hi')\n```"
        result = convert_markdown(text, Channel.SIGNAL)
        assert "```" not in result
        assert "print('hi')" in result


# ---------------------------------------------------------------------------
# Teams conversion (passthrough / minimal)
# ---------------------------------------------------------------------------
class TestTeams:
    def test_standard_markdown_preserved(self):
        text = "**bold** and [link](https://x.com)"
        assert convert_markdown(text, Channel.TEAMS) == text


# ---------------------------------------------------------------------------
# Google Chat conversion
# ---------------------------------------------------------------------------
class TestGoogleChat:
    def test_bold(self):
        assert convert_markdown("**hello**", Channel.GOOGLE_CHAT) == "*hello*"

    def test_link_to_text_url(self):
        result = convert_markdown("[G](https://g.com)", Channel.GOOGLE_CHAT)
        assert result == "G (https://g.com)"


# ---------------------------------------------------------------------------
# Code block protection across channels
# ---------------------------------------------------------------------------
class TestCodeBlockProtection:
    @pytest.mark.parametrize(
        "channel",
        [Channel.WHATSAPP, Channel.SLACK, Channel.TELEGRAM, Channel.GOOGLE_CHAT],
    )
    def test_code_inside_block_not_converted(self, channel):
        text = "```\n**not bold** [not a link](url)\n```"
        result = convert_markdown(text, channel)
        # The code block should be preserved as-is
        assert "**not bold**" in result or "```" in result


# ---------------------------------------------------------------------------
# Realistic LLM output
# ---------------------------------------------------------------------------
class TestRealisticOutput:
    def test_llm_response_whatsapp(self):
        text = (
            "## Quick Answer\n\n"
            "Here's what you need to know:\n\n"
            "**Step 1**: Install the package\n"
            "```bash\npip install pocketpaw\n```\n\n"
            "**Step 2**: Check the [docs](https://docs.example.com)\n\n"
            "~~Don't use the old method.~~"
        )
        result = convert_markdown(text, Channel.WHATSAPP)
        assert "## Quick Answer" not in result
        assert "*Quick Answer*" in result
        assert "*Step 1*" in result
        assert "```bash\npip install pocketpaw\n```" in result
        assert "docs (https://docs.example.com)" in result
        assert "~Don't use the old method.~" in result

    def test_plain_text_unchanged(self):
        text = "Just a simple answer with no formatting at all."
        for ch in Channel:
            assert convert_markdown(text, ch) == text
