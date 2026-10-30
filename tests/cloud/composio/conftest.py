import pytest

from pocketpaw.config import get_settings


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """Give every test a clean ``get_settings()`` cache.

    ``composio.service`` reads config through the ``lru_cache``d accessor
    rather than ``Settings.load()`` — the latter re-parses the whole
    pydantic-settings model at ~115 ms a call, which made it the single
    largest cost in assembling a chat turn (measured 2026-08-04).

    The cache is process-global, so without this a test that sets
    ``composio_api_key`` leaks that value into every test that runs after it.
    These tests previously got isolation for free from ``Settings.load()``'s
    freshness; they now ask for it explicitly. Production invalidates the same
    way — every config-write path already calls ``get_settings.cache_clear()``.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
