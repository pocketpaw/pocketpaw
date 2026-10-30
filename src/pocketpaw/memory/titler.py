"""Chat title generation from the first user message.

Uses a Haiku-class Anthropic model to produce a short (≤6 word) title. The
caller is responsible for persisting the title and emitting the
``session_titled`` SystemEvent — this module only generates.

Surface context is stripped before titling (2026-07-22). Clients may prepend a
machine-readable context block to the wire content — the home chat sends a
snapshot of the user's pinned widgets and recent activity so the agent can
answer "what's on my home page?" without being told. That block is for the
AGENT, never for the human: titling the raw payload produced session names like
"[Home Page Snapshot] Time Of Day: Afternoon" instead of what the user actually
typed. ``strip_context_preamble`` recovers the real message.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Marker a client places between its context block and the user's own words.
# Contract shared with the paw-enterprise client (see home-context.ts, which
# emits ``f"{ctx}\n\n[User message]\n{text}"``). Kept as a lone constant so the
# convention is greppable from both sides.
_USER_MESSAGE_MARKER = "\n[User message]\n"


def strip_context_preamble(message: str) -> str:
    """Return the user's own words from a possibly context-wrapped message.

    Clients wrap outgoing content as ``<context>\\n\\n[User message]\\n<text>``.
    Everything up to and including the marker is scaffolding for the agent, so
    the title should come from what follows it. Messages without the marker —
    every ordinary chat — are returned unchanged.

    The LAST marker wins: a user who quotes the marker inside their own message
    still gets their trailing text, and a title can never expose more of the
    preamble than the client intended.
    """
    if not message:
        return message
    index = message.rfind(_USER_MESSAGE_MARKER)
    if index == -1:
        return message
    return message[index + len(_USER_MESSAGE_MARKER) :]


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

    Strips any client context preamble first — this is the path that produced
    the visible bug (a 60-char excerpt of the home-page snapshot as the session
    name). Stripping is idempotent, so it is safe that ``generate_title`` has
    usually stripped already.
    """
    text = strip_context_preamble(first_message or "").strip()
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

    Any client context preamble is stripped up front, so BOTH paths title from
    what the user actually typed — the model never sees the snapshot block
    either, which kept it from titling chats after the surface instead of the
    subject.
    """
    first_message = strip_context_preamble(first_message or "")
    text = first_message.strip()
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
