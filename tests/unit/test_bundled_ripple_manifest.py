# tests/unit/test_bundled_ripple_manifest.py
"""The bundled Ripple widget catalog: packaged with core, pinned to the
@ripple-ui/svelte version Paw OS renders, read from disk with no network, and
the ``ripple_manifest_url`` override falling back to it when it fails or is
older. Core only (no pocketpaw_ee)."""

from __future__ import annotations

import importlib.resources

import httpx
import pytest

from pocketpaw.ripple import manifest as m

PURPOSE_BUILT = ["itinerary", "bill-split", "figure", "growth-projection", "model-viewer"]


@pytest.fixture(autouse=True)
def _clear_cache():
    m._cache.clear()
    yield
    m._cache.clear()


@pytest.fixture
def default_settings(monkeypatch):
    """Settings as shipped: no ripple_manifest_url override in the env."""
    import pocketpaw.config as config

    monkeypatch.delenv("POCKETPAW_RIPPLE_MANIFEST_URL", raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: config.Settings())


@pytest.fixture
def no_network(monkeypatch):
    async def refuse(self, url, *a, **k):
        raise AssertionError(f"network call to {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)


def _serve(monkeypatch, payload, calls=None):
    async def fake_get(self, url, timeout):
        if calls is not None:
            calls.append(url)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def test_the_manifest_ships_inside_the_core_package():
    packaged = importlib.resources.files("pocketpaw.ripple") / "manifest.json"
    assert packaged.is_file()
    assert m.BUNDLED_MANIFEST_PATH.is_file()


def test_bundled_version_matches_the_ripple_ui_paw_enterprise_renders():
    bundled = m.load_bundled_manifest()
    assert m.RIPPLE_UI_SVELTE_VERSION == "0.8.2"
    assert bundled["version"] == m.RIPPLE_UI_SVELTE_VERSION
    assert bundled["schema"] == "ripple.manifest/v1"


def test_the_default_setting_means_bundled():
    from pocketpaw.config import Settings

    assert Settings.model_fields["ripple_manifest_url"].default == ""


async def test_empty_url_serves_bundled_without_network(no_network):
    got = await m.get_manifest("", ttl_seconds=60)
    assert got is m.load_bundled_manifest()


async def test_chat_get_widget_spec_finds_purpose_built_widgets_offline(no_network, default_settings):
    from pocketpaw.agents import sdk_mcp_widgets

    result = await sdk_mcp_widgets._get_widget_spec_handler({"types": PURPOSE_BUILT})
    text = result["content"][0]["text"]
    assert result.get("is_error") is not True, text
    assert "Unknown types" not in text
    for widget in PURPOSE_BUILT:
        assert widget in text


async def test_builtin_widget_spec_tool_finds_them_offline(no_network, default_settings):
    from pocketpaw.tools.builtin import widget_spec

    text = await widget_spec.WidgetSpecTool().execute(types=PURPOSE_BUILT)
    assert "Unknown" not in text and "unavailable" not in text
    for widget in PURPOSE_BUILT:
        assert widget in text


async def test_a_newer_override_is_used_and_cached(monkeypatch):
    newer = {**m.load_bundled_manifest(), "version": "9.0.0", "widgets": []}
    calls: list[str] = []
    _serve(monkeypatch, newer, calls)
    first = await m.get_manifest("https://cdn.example/manifest.json", ttl_seconds=60)
    second = await m.get_manifest("https://cdn.example/manifest.json", ttl_seconds=60)
    assert first["version"] == "9.0.0" and second is first
    assert calls == ["https://cdn.example/manifest.json"]


async def test_a_failing_override_falls_back_to_bundled_once_per_ttl(monkeypatch, caplog):
    calls: list[str] = []

    async def down(self, url, timeout):
        calls.append(url)
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.AsyncClient, "get", down)
    got = await m.get_manifest("https://cdn.example/manifest.json", ttl_seconds=60)
    again = await m.get_manifest("https://cdn.example/manifest.json", ttl_seconds=60)
    assert got is m.load_bundled_manifest() and again is got
    assert len(calls) == 1
    assert "using the bundled catalog" in caplog.text


async def test_an_older_override_falls_back_to_bundled(monkeypatch, caplog):
    stale = {"schema": "ripple.manifest/v1", "version": "0.3.1", "widgets": []}
    _serve(monkeypatch, stale)
    got = await m.get_manifest("https://prod.example/manifest.json", ttl_seconds=60)
    assert got is m.load_bundled_manifest()
    assert "older than the bundled" in caplog.text
