# src/pocketpaw/paw_bar/concierge_fields.py — the owner's guided concierge fields.
#
# An owner shapes the v2 concierge through six fields instead of a free prompt
# (PRD decision 6): a name, a tone, the languages it answers in, a few lines about
# the business, topics to avoid, and what it offers when it doesn't know. This
# module holds their shapes, caps and validators;
# ``pocketpaw_ee.paw_bar.concierge_prompt`` renders them. It also validates the
# two visitor-facing texts the frame shows: the AI disclosure line and the
# privacy policy link.
#
# It lives beside ``appearance.py`` for the same reason that does: the Site
# document stores these values, and the owner settings DTOs validate them, so
# both import one definition. A cap exceeded is a 422, never a silent truncation,
# because the owner should see what the concierge will actually be told.
#
# Owner text is untrusted (Global Constraint 5). Validation here only normalizes
# it (one line, no control characters, capped); the renderer quotes it into fixed
# sentences. Nothing in this module makes owner text safe to concatenate.

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, model_validator

# ---------------------------------------------------------------------------
# Caps (owner UX doc: name 40, about 600, 10 topics of 80, contact 120)
# ---------------------------------------------------------------------------

NAME_MAX_CHARS = 40
ABOUT_MAX_CHARS = 600
TOPIC_MAX_CHARS = 80
TOPICS_MAX = 10
CONTACT_MAX_CHARS = 120
# The bar's AI disclosure line: one short sentence under the composer.
DISCLOSURE_MAX_CHARS = 140
PRIVACY_URL_MAX_CHARS = 500
# Not in the UX doc; a concierge that "speaks" more than ten languages is a
# configuration mistake, and every code lands in the prompt.
LANGUAGES_MAX = 10

ConciergeTone = Literal["friendly", "professional", "concise", "playful"]
EscalationMode = Literal["handoff", "email", "none"]

# Structural BCP-47: language[-Script][-REGION][-variant]*. It checks shape, not
# registry membership, which is enough to keep anything but a code out.
_BCP47_RE = re.compile(
    r"^(?P<lang>[A-Za-z]{2,3})"
    r"(?:-(?P<script>[A-Za-z]{4}))?"
    r"(?:-(?P<region>[A-Za-z]{2}|[0-9]{3}))?"
    r"(?P<variants>(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*)$"
)
# Loose on purpose: the UX doc has no "that isn't an email" copy, so this refuses
# only what plainly is not an address.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# The privacy link becomes an ``href`` in the frame: https with a host, and
# nothing that could close the attribute or the tag.
_PRIVACY_URL_RE = re.compile(r"^https://[^\s/?#\"'`<>]+[^\s\"'`<>]*$", re.IGNORECASE)


def _strip_controls(text: str, *, keep_newlines: bool) -> str:
    """Drop control and format characters (Unicode Cc/Cf: NUL, bidi overrides,
    zero-width joiners). Whitespace survives as a plain space, or as a newline
    when ``keep_newlines``."""
    out: list[str] = []
    for ch in text:
        if ch == "\n" and keep_newlines:
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf"):
            out.append(ch)
    return "".join(out)


def one_line(text: str) -> str:
    """``text`` as one clean line: controls dropped, whitespace runs collapsed."""
    return " ".join(_strip_controls(text or "", keep_newlines=False).split())


def _capped(text: str, cap: int, what: str) -> str:
    if len(text) > cap:
        raise ValueError(f"{what} must be at most {cap} characters")
    return text


def clean_name(value: str) -> str:
    return _capped(one_line(value), NAME_MAX_CHARS, "concierge_name")


def clean_about(value: str) -> str:
    """Paragraph breaks are kept for the owner's editor; every other whitespace run
    becomes one space. The renderer folds it to one line anyway."""
    text = _strip_controls(value or "", keep_newlines=True)
    text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _capped(text, ABOUT_MAX_CHARS, "concierge_about")


def normalize_language(code: str) -> str | None:
    """A BCP-47 code in canonical case (``en``, ``zh-Hant-TW``), or None."""
    m = _BCP47_RE.match((code or "").strip())
    if m is None:
        return None
    parts = [m.group("lang").lower()]
    if m.group("script"):
        parts.append(m.group("script").title())
    if m.group("region"):
        parts.append(m.group("region").upper())
    parts.extend(v.lower() for v in m.group("variants").split("-") if v)
    return "-".join(parts)


def clean_languages(value: list[str]) -> list[str]:
    """Normalized, de-duplicated codes in the owner's order (the first is the
    fallback). Empty is refused: the concierge has to answer in something."""
    out: list[str] = []
    for code in value:
        norm = normalize_language(code)
        if norm is None:
            raise ValueError(f"{code!r} is not a BCP-47 language code")
        if norm not in out:
            out.append(norm)
    if not out:
        raise ValueError("pick at least one language")
    if len(out) > LANGUAGES_MAX:
        raise ValueError(f"at most {LANGUAGES_MAX} languages")
    return out


def clean_avoid_topics(value: list[str]) -> list[str]:
    """Blank entries dropped, duplicates (ignoring case) dropped, each capped.
    ``[]`` is allowed: it clears the list."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        topic = _capped(one_line(raw), TOPIC_MAX_CHARS, "each concierge_avoid_topics entry")
        if topic and topic.casefold() not in seen:
            seen.add(topic.casefold())
            out.append(topic)
    if len(out) > TOPICS_MAX:
        raise ValueError(f"at most {TOPICS_MAX} topics to avoid")
    return out


def clean_disclosure(value: str) -> str:
    """The AI disclosure line. "" keeps the bar's own wording, so the line can be
    reworded but never removed (EU AI Act Art. 50)."""
    return _capped(one_line(value), DISCLOSURE_MAX_CHARS, "concierge_disclosure")


def clean_privacy_url(value: str) -> str:
    """Empty, or an https:// URL with a host. Anything else is refused, never
    fixed: http would be a mixed-content link on the owner's https page."""
    url = (value or "").strip()
    if not url:
        return ""
    _capped(url, PRIVACY_URL_MAX_CHARS, "concierge_privacy_url")
    if not _PRIVACY_URL_RE.match(url):
        raise ValueError("concierge_privacy_url must be an https:// link")
    return url


ConciergeName = Annotated[str, AfterValidator(clean_name)]
ConciergeAbout = Annotated[str, AfterValidator(clean_about)]
ConciergeLanguages = Annotated[list[str], AfterValidator(clean_languages)]
ConciergeAvoidTopics = Annotated[list[str], AfterValidator(clean_avoid_topics)]
ConciergeDisclosure = Annotated[str, AfterValidator(clean_disclosure)]
ConciergePrivacyUrl = Annotated[str, AfterValidator(clean_privacy_url)]


class ConciergeEscalation(BaseModel):
    """What the concierge offers when the answer isn't in its knowledge.

    ``handoff`` offers a person from the team; ``email`` shares ``contact``;
    ``none`` just says it doesn't know. Sent and stored whole. ``contact`` is kept
    for every mode (the owner's editor round-trips it) but only rendered for
    ``email``, where it is required."""

    mode: EscalationMode = "handoff"
    contact: str = ""

    @model_validator(mode="after")
    def _check(self) -> ConciergeEscalation:
        self.contact = _capped(one_line(self.contact), CONTACT_MAX_CHARS, "escalation contact")
        if self.mode == "email" and not _EMAIL_RE.match(self.contact):
            raise ValueError("escalation mode 'email' needs a contact email address")
        return self


__all__ = [
    "ABOUT_MAX_CHARS",
    "CONTACT_MAX_CHARS",
    "ConciergeAbout",
    "ConciergeAvoidTopics",
    "ConciergeDisclosure",
    "ConciergeEscalation",
    "ConciergeLanguages",
    "ConciergeName",
    "ConciergePrivacyUrl",
    "ConciergeTone",
    "DISCLOSURE_MAX_CHARS",
    "EscalationMode",
    "LANGUAGES_MAX",
    "NAME_MAX_CHARS",
    "PRIVACY_URL_MAX_CHARS",
    "TOPICS_MAX",
    "TOPIC_MAX_CHARS",
    "clean_about",
    "clean_avoid_topics",
    "clean_disclosure",
    "clean_languages",
    "clean_name",
    "clean_privacy_url",
    "normalize_language",
    "one_line",
]
