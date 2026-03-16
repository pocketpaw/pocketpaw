"""Anthropic provider adapter."""

from __future__ import annotations

from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model


class AnthropicAdapter:
    name = "anthropic"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        return ProviderConfig(
            provider=self.name,
            model=resolve_model(settings, backend, self.name),
            api_key=settings.anthropic_api_key,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        if config.api_key:
            return {"ANTHROPIC_API_KEY": config.api_key}
        return {}

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "AnthropicAdapter does not support OpenAI clients. "
            "Use build_anthropic_client() instead."
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            api_key=config.api_key,
            timeout=kwargs.get("timeout", 60.0),
            max_retries=kwargs.get("max_retries", 2),
        )

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        err = str(error).lower()
        if "api key" in err or "authentication" in err:
            return (
                "Anthropic API key not configured.\n\n"
                "Open **Settings > API Keys** in the sidebar to add your key."
            )
        return f"API Error: {error}"
