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
# fal spells its credential ``<key-id>:<secret>``. Checked at the edge for the
# same reason the Anthropic prefix is: a paste that is obviously not a fal key
# should fail here, where the user can see the field, rather than one picture
# later inside a generation they paid for.
_MIN_IMAGE_KEY_LEN = 16


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


class ByokImageKeyRequest(BaseModel):
    """Set or replace this workspace's fal.ai key (illustrations).

    A SEPARATE route from ``ByokSetRequest`` on purpose: ``PUT /byok/key`` takes
    the whole LLM credential or nothing, so folding the image key in would mean
    re-pasting an Anthropic key to change an illustrator. The two credentials
    are independent and their write paths are too.
    """

    api_key: str = Field(min_length=_MIN_IMAGE_KEY_LEN, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _looks_like_a_fal_key(cls, v: str) -> str:
        v = v.strip()
        if any(c.isspace() for c in v):
            raise ValueError("the key contains whitespace — paste the key alone")
        head, sep, secret = v.partition(":")
        if not sep or not head or not secret:
            raise ValueError("a fal.ai key looks like '<key-id>:<secret>'")
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

    # The illustration credential (2026-09-11). Display-only, like the pair
    # above, and independent of it: a workspace may have either, both, or
    # neither. There is no ``image_verified_at`` because fal cannot be asked
    # whether a key is good without generating an image — ``image_last_error``
    # is stamped by the first refused generation instead.
    image_configured: bool = False
    image_last4: str | None = None
    image_key_hint: str | None = None
    image_last_error: str | None = None
