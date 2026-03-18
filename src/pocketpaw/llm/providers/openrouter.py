"""OpenRouter provider adapter."""

from __future__ import annotations

from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter:
    name = "openrouter"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        model = resolve_model(settings, backend, self.name)
        if not model:
            model = settings.openai_compatible_model
        api_key = settings.openrouter_api_key or settings.openai_compatible_api_key
        return ProviderConfig(
            provider=self.name,
            model=model,
            api_key=api_key,
            base_url=OPENROUTER_BASE_URL,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        # OpenRouter's Anthropic-compatible skin uses /api (not /api/v1)
        # and authenticates via ANTHROPIC_AUTH_TOKEN, not ANTHROPIC_API_KEY.
        base_url = (config.base_url or OPENROUTER_BASE_URL).rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        env: dict[str, str] = {
            "ANTHROPIC_BASE_URL": base_url,
            # Must be empty string, not omitted. OpenRouter authenticates
            # via ANTHROPIC_AUTH_TOKEN; if ANTHROPIC_API_KEY is set to a
            # real value, the SDK sends it as Bearer and OpenRouter rejects it.
            "ANTHROPIC_API_KEY": "",
        }
        if config.api_key:
            env["ANTHROPIC_AUTH_TOKEN"] = config.api_key
        return env

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=config.base_url or OPENROUTER_BASE_URL,
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        # For Anthropic client, strip /v1 to hit /api
        base_url = (config.base_url or OPENROUTER_BASE_URL).rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        return AsyncAnthropic(
            base_url=base_url,
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        full = f"{error}\n{stderr}".lower()
        url = config.base_url or OPENROUTER_BASE_URL
        if "auth" in full or "api key" in full:
            return (
                f"Authentication failed at `{url}`.\n\n"
                "Check your OpenRouter API key in "
                "**Settings > API Keys > OpenRouter**."
            )
        if stderr.strip():
            return f"OpenRouter error:\n\n{stderr.strip()}"
        return f"OpenRouter error: {error}\n\nEndpoint: `{url}`"
