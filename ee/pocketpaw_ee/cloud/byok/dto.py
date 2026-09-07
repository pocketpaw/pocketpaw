# ee/pocketpaw_ee/cloud/byok/dto.py — the wire shapes for BYOK.
#
# Created 2026-08-28 (feat/other-hand-byok).
#
# There is deliberately NO response model carrying the key. ``ByokStatus`` is
# the ONLY thing this domain returns, and it is built from display-only columns
# (``last4`` / ``key_hint``) so answering a status call never decrypts anything.
# If a future field would require a decrypt to populate, that is the signal to
# not add the field.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# Anthropic keys start with this. Checked at the edge so an obviously-wrong
# paste (an OpenAI key, a session token, a whole curl command) fails before it
# reaches the provider — and before we spend a validation round trip on it.
_ANTHROPIC_PREFIX = "sk-ant-"
_MIN_KEY_LEN = 20


class ByokSetRequest(BaseModel):
    """Set or replace this workspace's provider key."""

    provider: str = Field(default="anthropic")
    api_key: str = Field(min_length=_MIN_KEY_LEN, max_length=512)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v != "anthropic":
            raise ValueError("only the 'anthropic' provider is supported today")
        return v

    @field_validator("api_key")
    @classmethod
    def _looks_like_a_key(cls, v: str) -> str:
        # Whitespace is the single most common paste artefact, and a stray
        # newline inside a header value is worth rejecting on its own merits.
        v = v.strip()
        if any(c.isspace() for c in v):
            raise ValueError("the key contains whitespace — paste the key alone")
        if not v.startswith(_ANTHROPIC_PREFIX):
            raise ValueError(f"an Anthropic API key starts with {_ANTHROPIC_PREFIX!r}")
        return v


class ByokStatus(BaseModel):
    """What the UI is allowed to know about the stored key.

    Never carries the key, and never a field that would need one to compute.
    """

    configured: bool
    provider: str | None = None
    last4: str | None = None
    key_hint: str | None = None
    last_verified_at: datetime | None = None
    last_error: str | None = None
