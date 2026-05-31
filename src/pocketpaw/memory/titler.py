"""Chat title generation from the first user message.

Uses a Haiku-class Anthropic model to produce a short (≤6 word) title. The
caller is responsible for persisting the title and emitting the
``session_titled`` SystemEvent — this module only generates.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PROMPT = (
    "Write a concise chat title (max 6 words, Title Case, no quotes, no"
    " trailing punctuation) that captures the subject of this user message.\n\n"
    "Message:\n{message}\n\nTitle:"
)

_MAX_INPUT_CHARS = 2000
_MAX_TOKENS = 24


def fallback_title(first_message: str) -> str | None:
    """Derive a title from the first user message when Haiku is unavailable.

    Collapses whitespace and truncates to ~60 chars. Returns None when the
    message is empty so callers can skip the event altogether.
    """
    text = (first_message or "").strip()
    if not text:
        return None
    one_line = " ".join(text.split())
    if len(one_line) > 60:
        return one_line[:60].rstrip() + "…"
    return one_line


async def generate_title(
    first_message: str, *, model: str, api_key: str | None = None
) -> str | None:
    """Return a short title for ``first_message``.

    Prefers a Haiku-generated title; falls back to a trimmed first-message
    excerpt when the SDK / API key / network fails, so chats always get a
    non-default title. Returns None only for an empty first message.
    """
    text = (first_message or "").strip()
    if not text:
        return None
    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS]

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.info("anthropic SDK not installed; using fallback title")
        return fallback_title(first_message)

    if not api_key:
        logger.info("no Anthropic API key configured; using fallback title")
        return fallback_title(first_message)

    try:
        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": _PROMPT.format(message=text)}],
        )
    except Exception:
        logger.warning("Haiku title generation call failed; using fallback", exc_info=True)
        return fallback_title(first_message)

    try:
        raw = response.content[0].text
    except (AttributeError, IndexError):
        return fallback_title(first_message)

    title = raw.strip().strip('"').strip("'").rstrip(".").strip()
    if not title:
        return fallback_title(first_message)
    # Cap at a hard character budget in case the model ignores word-count.
    if len(title) > 80:
        title = title[:80].rstrip()
    return title
