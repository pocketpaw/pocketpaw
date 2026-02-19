"""Backend Registry — lazy discovery and import of agent backends.

Backends are registered as ``(module_path, class_name)`` pairs and imported
on demand so missing optional dependencies don't crash startup.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw.agents.backend import AgentBackend, BackendInfo

logger = logging.getLogger(__name__)

# module path, class name
_BACKEND_REGISTRY: dict[str, tuple[str, str]] = {
    "claude_agent_sdk": ("pocketpaw.agents.claude_sdk", "ClaudeSDKBackend"),
    "openai_agents": ("pocketpaw.agents.openai_agents", "OpenAIAgentsBackend"),
    "google_adk": ("pocketpaw.agents.google_adk", "GoogleADKBackend"),
    "codex_cli": ("pocketpaw.agents.codex_cli", "CodexCLIBackend"),
    "opencode": ("pocketpaw.agents.opencode", "OpenCodeBackend"),
    "copilot_sdk": ("pocketpaw.agents.copilot_sdk", "CopilotSDKBackend"),
}

# Backends that were removed — map to fallback for graceful migration
_LEGACY_BACKENDS: dict[str, str] = {
    "pocketpaw_native": "claude_agent_sdk",
    "open_interpreter": "claude_agent_sdk",
    "claude_code": "claude_agent_sdk",
    "gemini_cli": "google_adk",
}


def list_backends() -> list[str]:
    """Return all registered backend names (installed or not)."""
    return list(_BACKEND_REGISTRY)


def get_backend_class(name: str) -> type[AgentBackend] | None:
    """Lazily import and return a backend class, or *None* if unavailable."""
    # Handle legacy backend names
    if name in _LEGACY_BACKENDS:
        fallback = _LEGACY_BACKENDS[name]
        logger.warning(
            "Backend '%s' has been removed — falling back to '%s'",
            name,
            fallback,
        )
        name = fallback

    entry = _BACKEND_REGISTRY.get(name)
    if entry is None:
        return None

    module_path, class_name = entry
    try:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)
    except (ImportError, AttributeError) as exc:
        logger.debug("Cannot load backend '%s': %s", name, exc)
        return None


def get_backend_info(name: str) -> BackendInfo | None:
    """Return static ``BackendInfo`` for *name* without instantiating."""
    cls = get_backend_class(name)
    if cls is None:
        return None
    try:
        return cls.info()
    except Exception:
        return None


def register_backend(name: str, module: str, cls: str) -> None:
    """Register an external backend (plugin support)."""
    _BACKEND_REGISTRY[name] = (module, cls)
