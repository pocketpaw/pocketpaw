"""LiteLLM provider adapter.

Supports two modes:
- Proxy mode: routes through a LiteLLM proxy server
- Direct SDK mode: native LitellmModel/LiteLlm wrappers when available
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.config import Settings
from pocketpaw.llm.providers.base import ProviderConfig, resolve_model

logger = logging.getLogger(__name__)


class LiteLLMAdapter:
    name = "litellm"

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        return ProviderConfig(
            provider=self.name,
            model=resolve_model(settings, backend, self.name),
            api_key=settings.litellm_api_key,
            base_url=settings.litellm_api_base.rstrip("/"),
            max_tokens=settings.litellm_max_tokens,
        )

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        return {
            "ANTHROPIC_BASE_URL": config.base_url or "http://localhost:4000",
            "ANTHROPIC_API_KEY": config.api_key or "not-needed",
        }

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        base = (config.base_url or "http://localhost:4000").rstrip("/")
        return AsyncOpenAI(
            base_url=f"{base}/v1",
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 1),
        )

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            base_url=config.base_url or "http://localhost:4000",
            api_key=config.api_key or "not-needed",
            timeout=kwargs.get("timeout", 120.0),
            max_retries=kwargs.get("max_retries", 2),
        )

    def build_agents_model(self, config: ProviderConfig) -> Any:
        """Build a model for the OpenAI Agents SDK.

        - Proxy mode (base_url set): uses OpenAI-compat client pointing at the
          proxy, since the proxy handles model routing.
        - Direct SDK mode (no base_url): uses native LitellmModel which calls
          litellm.acompletion directly. Requires LiteLLM-prefixed model names
          (e.g. "anthropic/claude-sonnet-4-6").
        """
        # Proxy mode: route through the proxy as an OpenAI-compat endpoint
        if config.base_url:
            from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

            client = self.build_openai_client(config)
            return OpenAIChatCompletionsModel(model=config.model, openai_client=client)

        # Direct SDK mode: use native LitellmModel
        try:
            from agents.extensions.models.litellm_model import LitellmModel

            return LitellmModel(
                model=config.model,
                api_key=config.api_key,
            )
        except ImportError:
            logger.debug("LitellmModel not available, falling back to OpenAI-compat")
            from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

            client = self.build_openai_client(config)
            return OpenAIChatCompletionsModel(model=config.model, openai_client=client)

    def build_adk_model(self, config: ProviderConfig) -> Any:
        """Build a model for Google ADK.

        Tries ADK's native LiteLlm wrapper first, falls back to model string.
        """
        try:
            from google.adk.models.lite_llm import LiteLlm

            kwargs: dict[str, Any] = {"model": config.model}
            if config.api_key:
                kwargs["api_key"] = config.api_key
            if config.base_url:
                kwargs["base_url"] = config.base_url
            return LiteLlm(**kwargs)
        except ImportError:
            logger.warning("google.adk.models.lite_llm not available, falling back to model string")
            return config.model

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        full = f"{error}\n{stderr}".lower()
        url = config.base_url or "http://localhost:4000"
        if "connection" in full or "refused" in full:
            return (
                f"Cannot connect to LiteLLM proxy at `{url}`.\n\n"
                "Make sure the proxy is running: `litellm --config config.yaml`"
            )
        if "auth" in full or "api key" in full:
            return (
                f"Authentication failed with LiteLLM proxy at `{url}`.\n\n"
                "Check your LiteLLM API key in "
                "**Settings > General > LiteLLM API Key**."
            )
        if stderr.strip():
            return f"LiteLLM error:\n\n{stderr.strip()}"
        return f"LiteLLM error: {error}\n\nProxy: `{url}`"
