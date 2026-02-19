"""Centralized LLM client abstraction.

Consolidates provider detection, client creation, env var construction,
and error formatting that was previously duplicated across 7+ files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMClient:
    """Immutable descriptor for a resolved LLM provider configuration.

    Created via ``resolve_llm_client()`` — not intended for direct construction.
    """

    provider: str  # "anthropic" | "ollama" | "openai" | "openai_compatible" | "gemini"
    model: str  # resolved model name
    api_key: str | None  # API key (None for Ollama)
    ollama_host: str  # Ollama server URL (always populated from settings)
    openai_compatible_base_url: str = ""  # Base URL for OpenAI-compatible endpoints

    # -- convenience properties --

    @property
    def is_ollama(self) -> bool:
        return self.provider == "ollama"

    @property
    def is_anthropic(self) -> bool:
        return self.provider == "anthropic"

    @property
    def is_openai_compatible(self) -> bool:
        return self.provider == "openai_compatible"

    @property
    def is_gemini(self) -> bool:
        return self.provider == "gemini"

    # -- factory methods --

    def create_openai_client(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        """Create an AsyncOpenAI client for OpenAI-compatible endpoints."""
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=self.openai_compatible_base_url,
            api_key=self.api_key or "not-needed",
            timeout=timeout if timeout is not None else 120.0,
            max_retries=max_retries if max_retries is not None else 1,
        )

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

        if self.is_openai_compatible or self.is_gemini:
            return AsyncAnthropic(
                base_url=self.openai_compatible_base_url,
                api_key=self.api_key or "not-needed",
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
        if self.is_openai_compatible or self.is_gemini:
            env: dict[str, str] = {
                "ANTHROPIC_BASE_URL": self.openai_compatible_base_url,
            }
            if self.api_key:
                env["ANTHROPIC_API_KEY"] = self.api_key
            else:
                env["ANTHROPIC_API_KEY"] = "not-needed"
            return env
        # Anthropic / OpenAI — pass API key if available
        if self.api_key:
            return {"ANTHROPIC_API_KEY": self.api_key}
        return {}

    def format_api_error(self, error: Exception, *, stderr: str = "") -> str:
        """Return a user-friendly error message for an API failure.

        Args:
            error: The exception that occurred.
            stderr: Optional captured stderr from the CLI subprocess.
        """
        error_str = str(error)
        # Combine error string with stderr for pattern matching
        full_context = f"{error_str}\n{stderr}".lower()

        if self.is_ollama:
            if "not_found" in error_str or "not found" in full_context:
                return (
                    f"❌ Model '{self.model}' not found in Ollama.\n\n"
                    "Run `ollama list` to see available models, "
                    "then set the correct model in "
                    "**Settings → General → Ollama Model**."
                )
            if "connection" in full_context or "refused" in full_context:
                return (
                    f"❌ Cannot connect to Ollama at `{self.ollama_host}`.\n\n"
                    "Make sure Ollama is running: `ollama serve`"
                )
            return (
                f"❌ Ollama error: {error_str}\n\n"
                f"Check that Ollama is running and accessible at `{self.ollama_host}`."
            )

        if self.is_gemini:
            if "api key" in full_context or "auth" in full_context or "401" in full_context:
                return (
                    "❌ Google API key is invalid or missing.\n\n"
                    "Get a key at [AI Studio](https://aistudio.google.com/apikey), "
                    "then add it in **Settings → API Keys → Google API Key**."
                )
            if "not found" in full_context or "not exist" in full_context:
                return (
                    f"❌ Model '{self.model}' is not available via Gemini.\n\n"
                    "Check the model name in **Settings → General → Gemini Model**."
                )
            if stderr.strip():
                return f"❌ Gemini API error:\n\n{stderr.strip()}"
            return (
                f"❌ Gemini API error: {error_str}\n\n"
                "Check your Google API key and model in Settings."
            )

        if self.is_openai_compatible:
            # Model not found / not accessible
            if "issue with the selected model" in full_context or (
                "model" in full_context
                and ("not exist" in full_context or "not found" in full_context)
            ):
                hint = stderr.strip() if stderr.strip() else error_str
                return (
                    f"❌ Model '{self.model}' is not available at "
                    f"`{self.openai_compatible_base_url}`.\n\n"
                    f"{hint}\n\n"
                    "Check that the model name matches what the endpoint expects, "
                    "and that you have access to it."
                )
            # Connection errors
            if "connection" in full_context or "refused" in full_context:
                return (
                    f"❌ Cannot connect to endpoint at "
                    f"`{self.openai_compatible_base_url}`.\n\n"
                    "Make sure the server is running and the URL is correct."
                )
            # Authentication errors
            if "auth" in full_context or "api key" in full_context:
                return (
                    f"❌ Authentication failed at "
                    f"`{self.openai_compatible_base_url}`.\n\n"
                    "Check your API key in "
                    "**Settings → General → API Key**."
                )
            # Generic: surface stderr if available
            if stderr.strip():
                return f"❌ OpenAI-compatible endpoint error:\n\n{stderr.strip()}"
            return (
                f"❌ OpenAI-compatible endpoint error: {error_str}\n\n"
                f"Endpoint: `{self.openai_compatible_base_url}`"
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

    if provider == "openai_compatible":
        return LLMClient(
            provider="openai_compatible",
            model=settings.openai_compatible_model,
            api_key=settings.openai_compatible_api_key,
            ollama_host=settings.ollama_host,
            openai_compatible_base_url=settings.openai_compatible_base_url,
        )

    if provider == "gemini":
        return LLMClient(
            provider="gemini",
            model=settings.gemini_model,
            api_key=settings.google_api_key,
            ollama_host=settings.ollama_host,
            openai_compatible_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

    # Default: anthropic
    return LLMClient(
        provider="anthropic",
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        ollama_host=settings.ollama_host,
    )
