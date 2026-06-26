"""Base types for the provider adapter pattern.

Updated: 2026-06-26 (feat/mcg-3-pool-route-model) — added ``route_model``, the
  single writer that maps a per-agent ``model`` string onto the correct
  ``Settings`` field for ANY registered agent backend, driven by
  ``_BACKEND_MODEL_ATTR`` (plus ``_BACKEND_MODEL_ATTR_ALIASES`` for backends
  that read another backend's field, e.g. ``langchain_react`` reads
  ``deep_agents_model``). Replaces the brittle ``"claude" in backend`` substring
  routing in ``AgentPool._build`` that silently dropped the per-agent model for
  ``codex_cli``/``opencode``/``deep_agents``/``copilot_sdk``/``langchain_react``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pocketpaw.config import Settings


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved connection config for any LLM provider."""

    provider: str  # "anthropic", "ollama", "litellm", etc.
    model: str  # resolved model name
    api_key: str | None = None  # None for ollama
    base_url: str | None = None  # None for native anthropic/openai
    max_tokens: int = 0  # 0 = use provider default
    extra: dict[str, str] = field(default_factory=dict)


# -- Default models per provider (used as last-resort fallback) --
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.2",
    "openai": "gpt-5.2",
    "openai_compatible": "",
    "openrouter": "",
    "gemini": "gemini-3-pro-preview",
    "litellm": "",
}

# -- Maps backend name -> settings attribute prefix for model/provider --
_BACKEND_MODEL_ATTR: dict[str, str] = {
    "claude_agent_sdk": "claude_sdk_model",
    "openai_agents": "openai_agents_model",
    "google_adk": "google_adk_model",
    "codex_cli": "codex_cli_model",
    "copilot_sdk": "copilot_sdk_model",
    "opencode": "opencode_model",
    "deep_agents": "deep_agents_model",
}

# -- Backends that read another backend's model field instead of their own --
# ``langchain_react`` subclasses ``DeepAgentsBackend`` and reads
# ``settings.deep_agents_model`` directly (it has no ``langchain_react_model``
# field), so it shares deep_agents' slot. Kept SEPARATE from
# ``_BACKEND_MODEL_ATTR`` so that map stays a 1:1 backend->own-field registry;
# ``route_model`` consults this alias map only when a backend isn't a primary
# key. ``resolve_model``'s backend lookup is unaffected (langchain_react resolves
# via the deep_agents path in the backend itself, not through this table).
_BACKEND_MODEL_ATTR_ALIASES: dict[str, str] = {
    "langchain_react": "deep_agents_model",
}

# -- Maps provider name -> settings attribute for provider-level model --
_PROVIDER_MODEL_ATTR: dict[str, str] = {
    "anthropic": "anthropic_model",
    "ollama": "ollama_model",
    "openai": "openai_model",
    "openai_compatible": "openai_compatible_model",
    "openrouter": "openrouter_model",
    "gemini": "gemini_model",
    "litellm": "litellm_model",
}


def resolve_model(settings: Settings, backend: str, provider: str) -> str:
    """Resolve model name with standard fallback chain.

    Priority:
    1. Backend-specific model (e.g. settings.claude_sdk_model)
    2. Provider-specific model (e.g. settings.anthropic_model)
    3. Provider default (e.g. "claude-sonnet-4-6")
    """
    # 1. Backend-specific
    backend_attr = _BACKEND_MODEL_ATTR.get(backend)
    if backend_attr:
        val = getattr(settings, backend_attr, "")
        if val:
            return val

    # 2. Provider-specific
    provider_attr = _PROVIDER_MODEL_ATTR.get(provider)
    if provider_attr:
        val = getattr(settings, provider_attr, "")
        if val:
            return val

    # 3. Provider default
    return PROVIDER_DEFAULT_MODELS.get(provider, "")


def route_model(settings: Settings, backend: str, model: str) -> bool:
    """Write a per-agent ``model`` onto the ``Settings`` field that ``backend``
    actually reads, for EVERY registered agent backend.

    This is the inverse of ``resolve_model``'s backend-specific lookup: given a
    model string chosen for one agent, it sets the single backend-model attribute
    so the backend instantiated from these settings picks it up. The field is
    chosen from ``_BACKEND_MODEL_ATTR`` (the 1:1 backend->own-field registry),
    falling back to ``_BACKEND_MODEL_ATTR_ALIASES`` for backends that read a
    sibling's field (``langchain_react`` -> ``deep_agents_model``).

    The model string is written VERBATIM. Backends whose field carries a
    composite format keep it intact — ``deep_agents``/``langchain_react`` expect
    ``provider:model`` and ``opencode`` expects ``provider/model``; this helper
    does NOT split or reformat, so those round-trip exactly as supplied by the
    upstream picker.

    Catalog/existence validation of the model is NOT done here — that lives
    upstream in the picker / EE catalog layer (MCG-1/4); OSS cannot import EE.
    This is the field-routing seam only.

    Args:
        settings: the per-agent ``Settings`` clone to mutate in place.
        backend: the registered backend name (e.g. ``"codex_cli"``).
        model: the per-agent model string. Empty/blank is a no-op — the backend
            then falls through to its own default via ``resolve_model``.

    Returns:
        ``True`` if a Settings field was written; ``False`` if the model was
        empty or the backend has no known model field (unknown backend — the
        caller leaves defaults untouched, matching legacy fall-through).
    """
    if not model:
        return False
    attr = _BACKEND_MODEL_ATTR.get(backend) or _BACKEND_MODEL_ATTR_ALIASES.get(backend)
    if not attr:
        return False
    setattr(settings, attr, model)
    return True


@runtime_checkable
class ProviderAdapter(Protocol):
    """Interface every provider adapter implements."""

    name: str

    def resolve_config(self, settings: Settings, backend: str) -> ProviderConfig:
        """Resolve settings into connection config for a given backend."""
        ...

    def build_env_dict(self, config: ProviderConfig) -> dict[str, str]:
        """Build env vars for subprocess-based backends (Claude SDK, Codex)."""
        ...

    def build_openai_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        """Build an AsyncOpenAI client."""
        ...

    def build_anthropic_client(self, config: ProviderConfig, **kwargs: Any) -> Any:
        """Build an AsyncAnthropic client."""
        ...

    def format_error(self, config: ProviderConfig, error: Exception, stderr: str = "") -> str:
        """Provider-specific error formatting."""
        ...
