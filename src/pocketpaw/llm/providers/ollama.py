"""Ollama provider adapter.

Change (2026-06-10): documented the supported local / bring-your-own-model
lane and corrected an earlier misread of the claude-harness path. There are
two Ollama code paths and they are NOT equivalent in RELIABILITY today:

- ``build_env_dict`` points the Claude Code CLI subprocess (the
  ``claude_agent_sdk`` backend) at the Ollama host via ``ANTHROPIC_BASE_URL``
  + ``ANTHROPIC_API_KEY="ollama"``. Ollama v0.14.0+ DOES expose an
  Anthropic-compatible ``POST /v1/messages`` endpoint (announced 2026-01-16),
  so the wire format is no longer the blocker. BUT this combination is not
  reliable yet: the Claude Code CLI also calls
  ``/v1/messages/count_tokens?beta=true`` and uses prompt-caching / beta
  features Ollama's shim does not implement, and Ollama's 404 handling for
  those degrades the server into timeouts/unresponsiveness (open upstream:
  ollama/ollama#13949). So we do NOT route the BYO/local lane through the
  claude harness — it's left as a power-user option, not the supported path.
- ``build_openai_client`` and the ``langchain_react`` / ``deep_agents``
  backends drive a genuine local model over Ollama's clean native API: the
  langchain backends build a ``ChatOllama`` via
  ``init_chat_model("ollama:<model>", base_url=<host>)`` which talks to
  ``/api/chat`` and never touches the count_tokens/beta surface. THIS is the
  supported local-only lane — set ``agent_backend=langchain_react`` (or
  ``deep_agents``) and ``llm_provider=ollama``. No cloud key is configured, so
  every request stays on the box. See docs/deployment/self-hosting.mdx.
"""

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
        # Points the Claude Code CLI subprocess at Ollama's Anthropic-compatible
        # /v1/messages endpoint (Ollama v0.14.0+). The wire format works, but
        # the claude_agent_sdk + Ollama combo is NOT the supported BYO/local
        # path: the CLI's count_tokens/beta-caching calls degrade Ollama today
        # (ollama/ollama#13949). For the reliable local-only lane, route through
        # the langchain_react / deep_agents backend (ChatOllama → native
        # /api/chat). See module docstring and docs/deployment/self-hosting.mdx.
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
