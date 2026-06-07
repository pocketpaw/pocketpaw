"""Feature flag resolution.

Reads OSS settings, but lets enterprise capability providers force-on
specific features when running in cloud mode. Providers are registered via
the ``pocketpaw.capabilities`` entry-point and discovered through the
extension registry — core never imports ``pocketpaw_ee`` directly.
"""

from __future__ import annotations

from pocketpaw.config import Settings


def _cloud_override(name: str) -> bool | None:
    """Return True/False if a capability provider forces *name*, else None."""
    from pocketpaw._registry import providers

    for provider in providers("pocketpaw.capabilities"):
        try:
            caps = provider.capabilities()
        except Exception:
            continue
        if name in caps:
            return bool(caps[name])
    return None


def chat_titles_enabled(settings: Settings) -> bool:
    override = _cloud_override("chat_titles_enabled")
    if override is not None:
        return override
    return settings.chat_title_generation_enabled
