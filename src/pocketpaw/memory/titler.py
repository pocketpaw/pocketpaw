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


async def generate_title(
    first_message: str, *, model: str, api_key: str | None = None
) -> str | None:
    """Return a short title for ``first_message`` or ``None`` on failure.

    Failures (missing SDK, missing key, API error) are logged at debug and
    swallowed — titling is best-effort and must never break the chat flow.
    """
    text = (first_message or "").strip()
    if not text:
        return None
    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS]

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.debug("anthropic SDK not installed; skipping title generation")
        return None

    try:
        client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        response = await client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": _PROMPT.format(message=text)}],
        )
    except Exception:
        logger.debug("title generation call failed", exc_info=True)
        return None

    try:
        raw = response.content[0].text
    except (AttributeError, IndexError):
        return None

    title = raw.strip().strip('"').strip("'").rstrip(".").strip()
    if not title:
        return None
    # Cap at a hard character budget in case the model ignores word-count.
    if len(title) > 80:
        title = title[:80].rstrip()
    return title
