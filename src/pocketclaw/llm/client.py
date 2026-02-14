"""Centralized LLM client abstraction.

Consolidates provider detection, client creation, env var construction,
and error formatting that was previously duplicated across 7+ files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pocketclaw.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMClient:
    """Immutable descriptor for a resolved LLM provider configuration.

    Created via ``resolve_llm_client()`` — not intended for direct construction.
    """

    provider: str  # "anthropic" | "ollama" | "openai"
    model: str  # resolved model name
    api_key: str | None  # API key (None for Ollama)
    ollama_host: str  # Ollama server URL (always populated from settings)

    # -- convenience properties --

    @property
    def is_ollama(self) -> bool:
        return self.provider == "ollama"

    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"

    # -- factory methods --

    def create_anthropic_client(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        """Create an ``AsyncAnthropic`` client configured for this provider.

        Raises ``ValueError`` if the provider is ``openai`` (not supported
        by the Anthropic SDK).
        """
        from anthropic import AsyncAnthropic

        if self.provider == "openai":
            raise ValueError(
                "Cannot create an Anthropic client for the OpenAI provider. "
                "Use the OpenAI SDK instead."
            )

        if self.is_ollama:
            return AsyncAnthropic(
                base_url=self.ollama_host,
                api_key="ollama",
                timeout=timeout if timeout is not None else 120.0,
                max_retries=max_retries if max_retries is not None else 1,
            )

        # Anthropic
        return AsyncAnthropic(
            api_key=self.api_key,
            timeout=timeout if timeout is not None else 60.0,
            max_retries=max_retries if max_retries is not None else 2,
        )

    def to_sdk_env(self) -> dict[str, str]:
        """Build env-var dict for the Claude Agent SDK subprocess."""
        if self.is_ollama:
            return {
                "ANTHROPIC_BASE_URL": self.ollama_host,
                "ANTHROPIC_API_KEY": "ollama",
            }
        # Anthropic / OpenAI — pass API key if available
        if self.api_key:
            return {"ANTHROPIC_API_KEY": self.api_key}
        return {}

    def format_api_error(self, error: Exception) -> str:
        """Return a user-friendly error message for an API failure."""
        error_str = str(error)

        if self.is_ollama:
            if "not_found" in error_str or "not found" in error_str.lower():
                return (
                    f"❌ Model '{self.model}' not found in Ollama.\n\n"
                    "Run `ollama list` to see available models, "
                    "then set the correct model in "
                    "**Settings → General → Ollama Model**."
                )
            if "connection" in error_str.lower() or "refused" in error_str.lower():
                return (
                    f"❌ Cannot connect to Ollama at `{self.ollama_host}`.\n\n"
                    "Make sure Ollama is running: `ollama serve`"
                )
            return (
                f"❌ Ollama error: {error_str}\n\n"
                f"Check that Ollama is running and accessible at `{self.ollama_host}`."
            )

        # Anthropic / OpenAI
        if "api key" in error_str.lower() or "authentication" in error_str.lower():
            return (
                "❌ Anthropic API key not configured.\n\n"
                "Open **Settings → API Keys** in the sidebar to add your key."
            )
        return f"❌ API Error: {error_str}"


def resolve_llm_client(
    settings: Settings,
    *,
    force_provider: str | None = None,
) -> LLMClient:
    """Resolve settings into an ``LLMClient``.

    Parameters
    ----------
    settings:
        The application settings.
    force_provider:
        Override the configured ``llm_provider``.  Useful for security
        modules that must always use a cloud API (``"anthropic"``), or
        for the ``--check-ollama`` CLI that forces ``"ollama"``.

    Auto-resolution order (when ``llm_provider == "auto"``):
        anthropic (if key set) → openai (if key set) → ollama (fallback).
    """
    provider = force_provider or settings.llm_provider

    if provider == "auto":
        if settings.anthropic_api_key:
            provider = "anthropic"
        elif settings.openai_api_key:
            provider = "openai"
        else:
            provider = "ollama"

    if provider == "ollama":
        return LLMClient(
            provider="ollama",
            model=settings.ollama_model,
            api_key=None,
            ollama_host=settings.ollama_host,
        )

    if provider == "openai":
        return LLMClient(
            provider="openai",
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            ollama_host=settings.ollama_host,
        )

    # Default: anthropic
    return LLMClient(
        provider="anthropic",
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        ollama_host=settings.ollama_host,
    )
