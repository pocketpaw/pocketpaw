# ee/pocketpaw_ee/cloud/byok/dto.py — the wire shapes for BYOK.
#
# Created 2026-08-28 (feat/other-hand-byok).
#
# Updated 2026-09-09 (feat/byok-custom-gateway): a second provider,
# ``openai_compatible``, for any gateway that speaks the OpenAI wire
# (Experiential Labs, OpenRouter, LiteLLM, a local vLLM). It brings its own
# ``base_url`` and ``model``, and its key has no fixed prefix, so the
# ``sk-ant-`` check now applies to the anthropic provider only.
#
# ``base_url`` goes through ``validate_external_url_strict``, the same guard
# pocket backend URLs use: https only, and loopback / RFC1918 / link-local /
# CGNAT hosts blocked with no operator escape hatch. This field is a
# stranger's input that our server then sends requests to, which is the exact
# shape of an SSRF, so the permissive ``validate_external_url`` is the wrong
# one here even though it is what the Settings fields use.
#
# There is deliberately NO response model carrying the key. ``ByokStatus`` is
# the ONLY thing this domain returns, and it is built from display-only columns
# (``last4`` / ``key_hint``) so answering a status call never decrypts anything.
# If a future field would require a decrypt to populate, that is the signal to
# not add the field.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from pocketpaw.security.url_validators import validate_external_url_strict

# Anthropic keys start with this. Checked at the edge so an obviously-wrong
# paste (an OpenAI key, a session token, a whole curl command) fails before it
# reaches the provider — and before we spend a validation round trip on it.
_ANTHROPIC_PREFIX = "sk-ant-"
_MIN_KEY_LEN = 20

#: Providers a key row may name. Kept in step with
#: ``byok.service.SUPPORTED_PROVIDERS`` — this is the edge check, that is the
#: pipeline check, and they must not drift.
_PROVIDERS = frozenset({"anthropic", "openai_compatible"})


class ByokSetRequest(BaseModel):
    """Set or replace this workspace's provider key."""

    provider: str = Field(default="anthropic")
    api_key: str = Field(min_length=_MIN_KEY_LEN, max_length=512)
    #: Required for ``openai_compatible``, rejected for anthropic. Must carry
    #: the version path the gateway serves (``/v1``): the OpenAI client appends
    #: only ``/chat/completions``, so a bare host 404s every request.
    base_url: str | None = Field(default=None, max_length=512)
    #: Required for ``openai_compatible``, rejected for anthropic.
    model: str | None = Field(default=None, max_length=200)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in _PROVIDERS:
            raise ValueError(f"provider must be one of {sorted(_PROVIDERS)}")
        return v

    @field_validator("api_key")
    @classmethod
    def _no_whitespace(cls, v: str) -> str:
        # Whitespace is the single most common paste artefact, and a stray
        # newline inside a header value is worth rejecting on its own merits.
        v = v.strip()
        if any(c.isspace() for c in v):
            raise ValueError("the key contains whitespace — paste the key alone")
        return v

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            return None
        # ponytail: validated here and resolved again when the request is made,
        # so a DNS rebind between the two is not closed. The egress guard
        # (``assert_egress_allowed`` + ``PinnedTransport``) closes it and is the
        # upgrade path if a gateway URL ever becomes long-lived infrastructure
        # rather than a thing one guest typed.
        return validate_external_url_strict(v)

    @model_validator(mode="after")
    def _provider_shape(self) -> ByokSetRequest:
        """Each provider carries exactly the fields it can use.

        An anthropic key with a base URL would silently ignore the URL; a
        gateway key without one has nowhere to spend. Both are the kind of
        half-configured state that only shows up as a dead first turn.
        """
        if self.provider == "anthropic":
            if not self.api_key.startswith(_ANTHROPIC_PREFIX):
                raise ValueError(f"an Anthropic API key starts with {_ANTHROPIC_PREFIX!r}")
            if self.base_url or self.model:
                raise ValueError(
                    "base_url and model belong to the 'openai_compatible' provider, "
                    "not to 'anthropic'"
                )
        else:
            if not self.base_url:
                raise ValueError("a gateway key needs a base_url, e.g. https://host/v1")
            if not (self.model or "").strip():
                raise ValueError("a gateway key needs the model id the gateway serves")
        return self


class ByokStatus(BaseModel):
    """What the UI is allowed to know about the stored key.

    Never carries the key, and never a field that would need one to compute.
    """

    configured: bool
    provider: str | None = None
    # Non-secret by construction: a gateway's address and model id are public.
    # Neither needs a decrypt to read, so the "no field that needs plaintext"
    # rule above still holds.
    base_url: str | None = None
    model: str | None = None
    last4: str | None = None
    key_hint: str | None = None
    last_verified_at: datetime | None = None
    last_error: str | None = None
