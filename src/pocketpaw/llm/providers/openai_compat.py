"""OpenAI-compatible endpoint provider adapter."""

from __future__ import annotations

from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model


class OpenAICompatibleAdapter:
    name = "openai_compatible"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        return ProviderConfig(
            provider=self.name,
            model=resolve_model(settings, backend, self.name),
            api_key=settings.openai_compatible_api_key,
            base_url=settings.openai_compatible_base_url,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        env: dict[str, str] = {}
        if config.base_url:
            env["ANTHROPIC_BASE_URL"] = config.base_url
        env["ANTHROPIC_API_KEY"] = config.api_key or "not-needed"
        return env

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            base_url=config.base_url,
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        full = f"{error}\n{stderr}".lower()
        url = config.base_url or "(not set)"

        if "issue with the selected model" in full or (
            "model" in full and ("not exist" in full or "not found" in full)
        ):
            hint = stderr.strip() if stderr.strip() else str(error)
            return (
                f"Model '{config.model}' is not available at `{url}`.\n\n"
                f"{hint}\n\n"
                "Check that the model name matches what the endpoint expects, "
                "and that you have access to it."
            )
        if "connection" in full or "refused" in full:
            return (
                f"Cannot connect to endpoint at `{url}`.\n\n"
                "Make sure the server is running and the URL is correct."
            )
        if "auth" in full or "api key" in full:
            return (
                f"Authentication failed at `{url}`.\n\n"
                "Check your API key in **Settings > General > API Key**."
            )
        if stderr.strip():
            return f"OpenAI-compatible endpoint error:\n\n{stderr.strip()}"
        return f"OpenAI-compatible endpoint error: {error}\n\nEndpoint: `{url}`"
