"""Gemini (Google AI) provider adapter."""

from __future__ import annotations

from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiAdapter:
    name = "gemini"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        return ProviderConfig(
            provider=self.name,
            model=resolve_model(settings, backend, self.name),
            api_key=settings.google_api_key,
            base_url=GEMINI_BASE_URL,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        env: dict[str, str] = {"ANTHROPIC_BASE_URL": GEMINI_BASE_URL}
        env["ANTHROPIC_API_KEY"] = config.api_key or "not-needed"
        return env

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=GEMINI_BASE_URL,
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            base_url=GEMINI_BASE_URL,
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        full = f"{error}\n{stderr}".lower()
        if "api key" in full or "auth" in full or "401" in full:
            return (
                "Google API key is invalid or missing.\n\n"
                "Get a key at [AI Studio](https://aistudio.google.com/apikey), "
                "then add it in **Settings > API Keys > Google API Key**."
            )
        if "not found" in full or "not exist" in full:
            return (
                f"Model '{config.model}' is not available via Gemini.\n\n"
                "Check the model name in **Settings > General > Gemini Model**."
            )
        if stderr.strip():
            return f"Gemini API error:\n\n{stderr.strip()}"
        return f"Gemini API error: {error}\n\nCheck your Google API key and model in Settings."
