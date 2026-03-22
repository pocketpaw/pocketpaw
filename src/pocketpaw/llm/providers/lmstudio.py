from __future__ import annotations

from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model


class LmStudioAdapter:
    name = "lmstudio"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        host = (getattr(settings, "lmstudio_host", None) or "http://localhost:1234").rstrip("/")
        return ProviderConfig(
            provider=self.name,
            model=resolve_model(settings, backend, self.name),
            api_key=None,
            base_url=host,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        base = (config.base_url or "http://localhost:1234").rstrip("/")
        return {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_API_KEY": "lmstudio",
        }

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        base = (config.base_url or "http://localhost:1234").rstrip("/")
        return AsyncOpenAI(
            base_url=f"{base}/v1",
            api_key="lmstudio",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        base = config.base_url or "http://localhost:1234"
        return AsyncAnthropic(
            base_url=base.rstrip("/"),
            api_key="lmstudio",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        full = f"{error}\n{stderr}".lower()
        host = config.base_url or "http://localhost:1234"
        if "not_found" in str(error) or "not found" in full or "issue with the selected model" in full:
            return (
                f"Model '{config.model}' is not available in LM Studio.\n\n"
                "Load the model in LM Studio and pick it in **Settings → AI Model**, "
                "or type the exact model id from **Server → Available Models**."
            )
        if "connection" in full or "refused" in full:
            return (
                f"Cannot connect to LM Studio at `{host}`.\n\n"
                "Start the local server in LM Studio (Developer → Start Server) "
                "and ensure the port matches **LM Studio Host**."
            )
        return f"LM Studio error: {error}\n\nServer: `{host}`"
