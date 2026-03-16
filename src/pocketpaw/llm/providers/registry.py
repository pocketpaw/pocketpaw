"""Provider adapter registry."""

from __future__ import annotations

from pocketpaw.llm.providers.anthropic import AnthropicAdapter
from pocketpaw.llm.providers.base import ProviderAdapter, ProviderConfig, resolve_model
from pocketpaw.llm.providers.gemini import GeminiAdapter
from pocketpaw.llm.providers.litellm import LiteLLMAdapter
from pocketpaw.llm.providers.ollama import OllamaAdapter
from pocketpaw.llm.providers.openai_compat import OpenAICompatibleAdapter
from pocketpaw.llm.providers.openrouter import OpenRouterAdapter

_ADAPTER_REGISTRY: dict[str, ProviderAdapter] = {
    "anthropic": AnthropicAdapter(),
    "ollama": OllamaAdapter(),
    "openai_compatible": OpenAICompatibleAdapter(),
    "openrouter": OpenRouterAdapter(),
    "gemini": GeminiAdapter(),
    "litellm": LiteLLMAdapter(),
}


def get_adapter(provider: str) -> ProviderAdapter:
    """Get adapter instance by provider name.

    Raises ``KeyError`` for unknown providers.
    """
    try:
        return _ADAPTER_REGISTRY[provider]
    except KeyError:
        available = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise KeyError(f"Unknown provider '{provider}'. Available: {available}") from None


__all__ = [
    "ProviderAdapter",
    "ProviderConfig",
    "get_adapter",
    "resolve_model",
]
