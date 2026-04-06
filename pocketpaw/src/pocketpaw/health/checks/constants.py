"""Shared constants for health check modules."""

from __future__ import annotations

# Providers that do NOT require an Anthropic API key.
NON_ANTHROPIC_PROVIDERS = ("ollama", "openai_compatible", "gemini", "litellm", "openrouter")

# Providers that do NOT require an OpenAI API key.
NON_OPENAI_PROVIDERS = ("ollama", "openai_compatible", "litellm", "openrouter")
