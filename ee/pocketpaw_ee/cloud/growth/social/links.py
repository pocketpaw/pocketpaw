# ee/pocketpaw_ee/cloud/growth/social/links.py — pure helpers for Growth ›
# Social tracked links: short-code generation and validation, the link-preview
# bot filter (a bot fetch is not a click), and URL extraction from post text.
# No I/O; ``growth.social.service`` owns the SocialLink doc.

from __future__ import annotations

import re
import secrets
from urllib.parse import urlsplit

# 6 random bytes -> 8 url-safe base64 characters. The pattern also accepts 7 so
# a shorter code scheme stays valid; anything else is refused before a DB read.
CODE_BYTES = 6
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{7,8}$")

_BOT_MARKERS = (
    "twitterbot",
    "facebookexternalhit",
    "linkedinbot",
    "slackbot",
    "discordbot",
    "telegrambot",
    "whatsapp",
    "googlebot",
    "bingbot",
    "applebot",
    "bot",
    "crawler",
    "spider",
    "preview",
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""


def new_code() -> str:
    return secrets.token_urlsafe(CODE_BYTES)


def valid_code(code: str) -> bool:
    return bool(CODE_RE.fullmatch(code))


def is_preview_bot(user_agent: str | None) -> bool:
    """True for link-preview fetchers and crawlers; an empty UA counts as a bot."""
    ua = (user_agent or "").strip().lower()
    return not ua or any(marker in ua for marker in _BOT_MARKERS)


def is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. an unclosed IPv6 bracket
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def extract_urls(text: str) -> list[str]:
    """http(s) URLs with a host in ``text``, in order, without trailing punctuation."""
    found = (m.group(0).rstrip(_TRAILING) for m in _URL_RE.finditer(text))
    return [url for url in found if is_web_url(url)]


__all__ = ["CODE_RE", "extract_urls", "is_preview_bot", "is_web_url", "new_code", "valid_code"]
