"""Centralized LLM client abstraction.

Consolidates provider detection, client creation, env var construction,
and error formatting. Delegates provider-specific logic to adapter classes
in ``pocketpaw.llm.providers``.

Also provides ``resolve_backend_env()`` which pushes the correct
environment variables (API keys, base URLs) for whichever backend is
currently active, so switching backends doesn't require manually
reconfiguring env vars.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMClient:
    """Immutable descriptor for a resolved LLM provider configuration.

    Created via ``resolve_llm_client()`` -- not intended for direct construction.
    """

    provider: str  # "anthropic" | "ollama" | "openai" | "openai_compatible" | "gemini" | "litellm"
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

    @property
    def is_litellm(self) -> bool:
        return self.provider == "litellm"

    @property
    def is_openrouter(self) -> bool:
        """True when the endpoint is OpenRouter's Anthropic-compatible skin."""
        from urllib.parse import urlparse

        try:
            host = urlparse(self.openai_compatible_base_url).hostname or ""
            return host == "openrouter.ai" or host.endswith(".openrouter.ai")
        except Exception:
            return False

    # -- factory methods (delegate to provider adapters) --

    def _get_adapter(self):
        """Get the provider adapter for this client's provider."""
        from pocketpaw.llm.providers import get_adapter

        provider_name = self.provider
        # OpenRouter is resolved as openai_compatible in LLMClient but
        # has its own adapter; detect by URL.
        if self.is_openrouter:
            provider_name = "openrouter"
        return get_adapter(provider_name)

    def _to_provider_config(self):
        """Convert this LLMClient to a ProviderConfig for adapter calls."""
        from pocketpaw.llm.providers.base import ProviderConfig

        return ProviderConfig(
            provider=self.provider,
            model=self.model,
            api_key=self.api_key,
            base_url=(
                (self.ollama_host or self.openai_compatible_base_url)
                if self.is_ollama
                else (self.openai_compatible_base_url or None)
            ),
        )

    def create_openai_client(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        """Create an AsyncOpenAI client for OpenAI-compatible endpoints."""
        adapter = self._get_adapter()
        config = self._to_provider_config()
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return adapter.build_openai_client(config, **kwargs)

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
        if self.provider == "openai":
            raise ValueError(
                "Cannot create an Anthropic client for the OpenAI provider. "
                "Use the OpenAI SDK instead."
            )
        adapter = self._get_adapter()
        config = self._to_provider_config()
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return adapter.build_anthropic_client(config, **kwargs)

    def to_sdk_env(self) -> dict[str, str]:
        """Build env-var dict for the Claude Agent SDK subprocess."""
        adapter = self._get_adapter()
        config = self._to_provider_config()
        return adapter.build_env_dict(config)

    def format_api_error(self, error: Exception, *, stderr: str = "") -> str:
        """Return a user-friendly error message for an API failure."""
        adapter = self._get_adapter()
        config = self._to_provider_config()
        return adapter.format_error(config, error, stderr=stderr)


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
        anthropic (if key set) -> openai (if key set) -> ollama (fallback).
    """
    from pocketpaw.llm.providers import get_adapter

    provider = force_provider or settings.llm_provider

    if provider == "auto":
        if settings.anthropic_api_key:
            provider = "anthropic"
        elif settings.openai_api_key:
            provider = "openai"
        elif settings.google_api_key:
            provider = "gemini"
        elif settings.openrouter_api_key:
            provider = "openrouter"
        elif settings.openai_compatible_base_url and settings.openai_compatible_api_key:
            provider = "openai_compatible"
        elif settings.litellm_api_key:
            provider = "litellm"
        else:
            provider = "ollama"

    # OpenAI provider doesn't have an adapter (not an LLM proxy target),
    # handle directly.
    if provider == "openai":
        return LLMClient(
            provider="openai",
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            ollama_host=settings.ollama_host,
        )

    # OpenRouter resolves as openai_compatible in LLMClient for backward
    # compat, but uses the OpenRouter adapter for config resolution.
    if provider == "openrouter":
        adapter = get_adapter("openrouter")
        config = adapter.resolve_config(settings, backend="")
        return LLMClient(
            provider="openai_compatible",
            model=config.model,
            api_key=config.api_key,
            ollama_host=settings.ollama_host,
            openai_compatible_base_url=config.base_url or "",
        )

    # All other providers use their adapter.
    # Fall back to "anthropic" for unknown/invalid provider values
    # (e.g. MagicMock objects in tests, stale config strings).
    try:
        adapter = get_adapter(provider)
    except KeyError:
        logger.warning("Unknown provider %r, falling back to anthropic", provider)
        adapter = get_adapter("anthropic")
    config = adapter.resolve_config(settings, backend="")
    return LLMClient(
        provider=config.provider,
        model=config.model,
        api_key=config.api_key,
        ollama_host=settings.ollama_host,
        openai_compatible_base_url=config.base_url or "",
    )


def resolve_backend_env(settings: Settings, *, force: bool = False) -> None:
    """Push the correct environment variables for the active backend.

    This solves the problem where switching backends (e.g. from
    claude_agent_sdk to openai_agents) requires manually setting
    different env vars. Instead, PocketPaw's unified POCKETPAW_*
    settings are resolved here and pushed to the env vars each
    backend's SDK expects.

    Called at startup (from ``__main__``) and again whenever the user
    saves settings or API keys via the dashboard.

    Parameters
    ----------
    settings:
        The application settings.
    force:
        When True, overwrite existing env vars. Used for runtime
        updates (e.g. user saves a new API key via the dashboard).
        When False (default/startup), existing env vars are preserved
        so user-provided env vars take precedence.
    """

    def _set(key: str, value: str) -> None:
        if force or not os.environ.get(key):
            os.environ[key] = value

    # -- Anthropic --
    if settings.anthropic_api_key:
        _set("ANTHROPIC_API_KEY", settings.anthropic_api_key)

    # -- Claude Code OAuth (Max/Pro subscription in Docker/headless) --
    if settings.claude_code_oauth_token:
        _set("CLAUDE_CODE_OAUTH_TOKEN", settings.claude_code_oauth_token)

    # -- OpenAI --
    if settings.openai_api_key:
        _set("OPENAI_API_KEY", settings.openai_api_key)

    # -- Google --
    if settings.google_api_key:
        _set("GOOGLE_API_KEY", settings.google_api_key)

    # -- OpenRouter --
    if settings.openrouter_api_key:
        _set("OPENROUTER_API_KEY", settings.openrouter_api_key)

    # -- LiteLLM --
    if settings.litellm_api_key:
        _set("LITELLM_API_KEY", settings.litellm_api_key)

    # -- Ollama --
    if settings.ollama_host != "http://localhost:11434":
        if force:
            os.environ["OLLAMA_HOST"] = settings.ollama_host
        else:
            os.environ.setdefault("OLLAMA_HOST", settings.ollama_host)

    logger.debug(
        "Backend env resolved for %s (ANTHROPIC=%s, OPENAI=%s, GOOGLE=%s, LITELLM=%s)",
        settings.agent_backend,
        "set" if os.environ.get("ANTHROPIC_API_KEY") else "unset",
        "set" if os.environ.get("OPENAI_API_KEY") else "unset",
        "set" if os.environ.get("GOOGLE_API_KEY") else "unset",
        "set" if os.environ.get("LITELLM_API_KEY") else "unset",
    )
