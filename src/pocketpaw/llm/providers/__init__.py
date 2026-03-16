"""LLM provider adapters.

Usage::

    from pocketpaw.llm.providers import get_adapter

    adapter = get_adapter("anthropic")
    config = adapter.resolve_config(settings, backend="claude_agent_sdk")
    env = adapter.build_env_dict(config)
"""

from pocketpaw.llm.providers.base import ProviderAdapter, ProviderConfig, resolve_model
from pocketpaw.llm.providers.registry import get_adapter

__all__ = [
    "ProviderAdapter",
    "ProviderConfig",
    "get_adapter",
    "resolve_model",
]
