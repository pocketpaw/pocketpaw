"""Social provider registry.

Created 2026-07-29 (AM-2/AM-3). One lookup point so the service never
branches on provider name, and so an unconfigured provider is reported as
absent rather than offered and then failing at the consent screen.
"""

from __future__ import annotations

from .base import SocialIdentity, SocialProvider
from .github import GitHubProvider
from .google import GoogleProvider

_PROVIDERS: dict[str, SocialProvider] = {
    "google": GoogleProvider(),
    "github": GitHubProvider(),
}


def get_provider(name: str) -> SocialProvider | None:
    """The adapter for ``name``, or None if unknown."""
    return _PROVIDERS.get((name or "").strip().lower())


def configured_providers() -> list[str]:
    """Names of providers with credentials present, in stable order.

    The frontend renders a button per entry, so a provider missing its
    credentials simply does not appear.
    """
    return [name for name, p in _PROVIDERS.items() if p.is_configured()]


__all__ = [
    "SocialIdentity",
    "SocialProvider",
    "configured_providers",
    "get_provider",
]
