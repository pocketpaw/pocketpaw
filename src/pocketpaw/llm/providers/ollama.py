"""Ollama provider adapter."""

from __future__ import annotations

from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model


class OllamaAdapter:
    name = "ollama"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        return ProviderConfig(
            provider=self.name,
            model=resolve_model(settings, backend, self.name),
            api_key=None,
            base_url=settings.ollama_host,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        return {
            "ANTHROPIC_BASE_URL": config.base_url or "http://localhost:11434",
            "ANTHROPIC_API_KEY": "ollama",
        }

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        host = config.base_url or "http://localhost:11434"
        return AsyncOpenAI(
            base_url=f"{host.rstrip('/')}/v1",
            api_key="ollama",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            base_url=config.base_url or "http://localhost:11434",
            api_key="ollama",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        full = f"{error}\n{stderr}".lower()
        host = config.base_url or "http://localhost:11434"
        if "not_found" in str(error) or "not found" in full:
            return (
                f"Model '{config.model}' not found in Ollama.\n\n"
                "Run `ollama list` to see available models, "
                "then set the correct model in "
                "**Settings > General > Ollama Model**."
            )
        if "connection" in full or "refused" in full:
            return (
                f"Cannot connect to Ollama at `{host}`.\n\n"
                "Make sure Ollama is running: `ollama serve`"
            )
        return f"Ollama error: {error}\n\nCheck that Ollama is running and accessible at `{host}`."
