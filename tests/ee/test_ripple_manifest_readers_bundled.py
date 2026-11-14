# tests/ee/test_ripple_manifest_readers_bundled.py
"""Every Ripple manifest reader (chat agent, pocket tools, catalog gates, chat
card quality log, Paw Bar) resolves to the one bundled catalog with no network
call when no ``ripple_manifest_url`` override is set."""

from __future__ import annotations

import httpx
import pytest

from pocketpaw.ripple import manifest as m


@pytest.fixture(autouse=True)
def _offline_default(monkeypatch):
    import pocketpaw.config as config

    async def refuse(self, url, *a, **k):
        raise AssertionError(f"network call to {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)
    monkeypatch.delenv("POCKETPAW_RIPPLE_MANIFEST_URL", raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: config.Settings())
    m._cache.clear()
    yield
    m._cache.clear()


async def test_all_readers_resolve_to_the_bundled_catalog(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import pockets
    from pocketpaw_ee.agent.pocket_specialist import tools as specialist_tools
    from pocketpaw_ee.cloud import ripple_validator
    from pocketpaw_ee.cloud.pockets import agent_context, service
    from pocketpaw_ee.paw_bar import card_spec

    from pocketpaw.config import get_settings

    bundled = m.load_bundled_manifest()
    assert bundled["version"] == m.RIPPLE_UI_SVELTE_VERSION

    # Paw Bar's RIPPLE_PROFILE and the pocket MCP seam hold the same object.
    assert card_spec.RIPPLE_MANIFEST is bundled
    assert card_spec.RIPPLE_MANIFEST_PATH == m.BUNDLED_MANIFEST_PATH
    assert await pockets._get_manifest_for_validation() is bundled

    # The pocket specialist binds get_manifest at import; same helper, same catalog.
    s = get_settings()
    got = await specialist_tools._get_manifest(
        s.ripple_manifest_url, ttl_seconds=s.ripple_manifest_ttl_seconds
    )
    assert got is bundled

    # Catalog gates read the bundled allow-list and required props.
    assert await service._catalog_allowed_types() == m.allowed_types_from_manifest(bundled)
    assert await service._catalog_required_props() == m.required_props_from_manifest(bundled)

    # The two validators see the manifest: an undeclared prop is flagged.
    seen: list[dict] = []
    real = m.validate_against_manifest

    def spy(spec, manifest, **kw):
        seen.append(manifest)
        return real(spec, manifest, **kw)

    monkeypatch.setattr("pocketpaw.ripple.manifest.validate_against_manifest", spy)
    monkeypatch.setattr(ripple_validator, "validate_against_manifest", spy)
    spec = {"version": "1.0", "ui": {"type": "text", "props": {"text": "hi", "bogus": 1}}}
    await agent_context._validate_ripple_spec(spec)
    await ripple_validator.log_chat_spec_quality(spec)
    assert len(seen) == 2 and all(x is bundled for x in seen)
