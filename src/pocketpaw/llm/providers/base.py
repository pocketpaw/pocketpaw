"""Base types for the provider adapter pattern."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pocketpaw.config import Settings


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved connection config for any LLM provider."""

    provider: str  # "anthropic", "ollama", "litellm", etc.
    model: str  # resolved model name
    api_key: str | None = None  # None for ollama
    base_url: str | None = None  # None for native anthropic/openai
    max_tokens: int = 0  # 0 = use provider default
    extra: dict[str, str] = field(default_factory=dict)


# -- Default models per provider (used as last-resort fallback) --
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.2",
    "openai": "gpt-5.2",
    "openai_compatible": "",
    "openrouter": "",
    "gemini": "gemini-3-pro-preview",
    "litellm": "",
}

# -- Maps backend name -> settings attribute prefix for model/provider --
_BACKEND_MODEL_ATTR: dict[str, str] = {
    "claude_agent_sdk": "claude_sdk_model",
    "openai_agents": "openai_agents_model",
    "google_adk": "google_adk_model",
    "codex_cli": "codex_cli_model",
    "copilot_sdk": "copilot_sdk_model",
    "opencode": "opencode_model",
}

# -- Maps provider name -> settings attribute for provider-level model --
_PROVIDER_MODEL_ATTR: dict[str, str] = {
    "anthropic": "anthropic_model",
    "ollama": "ollama_model",
    "openai": "openai_model",
    "openai_compatible": "openai_compatible_model",
    "openrouter": "openrouter_model",
    "gemini": "gemini_model",
    "litellm": "litellm_model",
}


def resolve_model(settings: Settings, backend: str, provider: str) -> str:
    """Resolve model name with standard fallback chain.

    Priority:
    1. Backend-specific model (e.g. settings.claude_sdk_model)
    2. Provider-specific model (e.g. settings.anthropic_model)
    3. Provider default (e.g. "claude-sonnet-4-6")
    """
    # 1. Backend-specific
    backend_attr = _BACKEND_MODEL_ATTR.get(backend)
    if backend_attr:
        val = getattr(settings, backend_attr, "")
        if val:
            return val

    # 2. Provider-specific
    provider_attr = _PROVIDER_MODEL_ATTR.get(provider)
    if provider_attr:
        val = getattr(settings, provider_attr, "")
        if val:
            return val

    # 3. Provider default
    return PROVIDER_DEFAULT_MODELS.get(provider, "")


@runtime_checkable
class ProviderAdapter(Protocol):
    """Interface every provider adapter implements."""

    name: str

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        """Resolve settings into connection config for a given backend."""
        ...

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        """Build env vars for subprocess-based backends (Claude SDK, Codex)."""
        ...

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        """Build an AsyncOpenAI client."""
        ...

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        """Build an AsyncAnthropic client."""
        ...

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        """Provider-specific error formatting."""
        ...
