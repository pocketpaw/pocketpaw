"""Feature flag resolution.

Reads OSS settings, but allows ``ee.cloud.features`` to force-on specific
capabilities when running in cloud mode. ee.cloud is imported lazily so OSS
builds without the ee package continue to work.
"""

from __future__ import annotations

from pocketpaw.config import Settings


def _cloud_override(name: str) -> bool | None:
    """Return True/False if ee.cloud forces a feature on/off, else None."""
    try:
        from ee.cloud import features as cloud_features  # type: ignore[import-not-found]
    except ImportError:
        return None
    getter = getattr(cloud_features, name, None)
    if getter is None:
        return None
    try:
        return bool(getter())
    except Exception:
        return None


def chat_titles_enabled(settings: Settings) -> bool:
    override = _cloud_override("chat_titles_enabled")
    if override is not None:
        return override
    return settings.chat_title_generation_enabled
