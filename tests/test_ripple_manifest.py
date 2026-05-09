# backend/tests/test_ripple_manifest.py
"""Tests for ee.ripple.manifest — fetcher, cache, formatter, fallback."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


VALID_MANIFEST = {
    "schema": "ripple.manifest/v1",
    "version": "0.2.0",
    "generatedAt": "2026-05-02T00:00:00.000Z",
    "spec": {
        "uiField": "ui",
        "stateField": "state",
        "version": "1.0",
        "aliasesNotAllowed": ["root", "tree", "view", "body", "content"],
        "description": "A Ripple spec wraps the renderable tree under `ui`.",
        "example": {
            "version": "1.0",
            "state": {"draft": "", "items": []},
            "ui": {"type": "flex", "props": {}, "children": []},
        },
    },
    "widgets": [
        {
            "type": "metric",
            "category": "display",
            "description": "KPI tile.",
            "props": {
                "label": {"type": "string", "required": False, "description": "Label."},
                "value": {"type": "string | number", "required": True, "description": "Value."},
            },
            "example": {"type": "metric", "props": {"label": "MRR", "value": "$48k"}},
        }
    ],
}


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    from ee.ripple import manifest as m

    m._cache.clear()
    yield
    m._cache.clear()


async def test_fetch_success(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is not None
    assert result["schema"] == "ripple.manifest/v1"
    assert len(result["widgets"]) == 1


async def test_cache_hit_avoids_second_fetch(monkeypatch):
    from ee.ripple import manifest as m

    calls = {"n": 0}

    async def fake_get(self, url, timeout):
        calls["n"] += 1
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert calls["n"] == 1


async def test_cache_expiry_triggers_refetch(monkeypatch):
    from ee.ripple import manifest as m

    calls = {"n": 0}

    async def fake_get(self, url, timeout):
        calls["n"] += 1
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await m.get_manifest("https://example/manifest.json", ttl_seconds=0)
    # ttl=0 means every call is expired — second call refetches
    await m.get_manifest("https://example/manifest.json", ttl_seconds=0)
    assert calls["n"] == 2


async def test_fetch_timeout_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_fetch_4xx_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(404, text="not found", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_malformed_json_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(200, text="not json", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_schema_mismatch_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    bad = {"schema": "ripple.manifest/v2", "version": "1", "widgets": []}

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=bad, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


async def test_missing_widgets_field_returns_none(monkeypatch):
    from ee.ripple import manifest as m

    bad = {"schema": "ripple.manifest/v1", "version": "1"}

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=bad, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await m.get_manifest("https://example/manifest.json", ttl_seconds=60)
    assert result is None


def test_format_for_prompt_renders_widgets():
    from ee.ripple import manifest as m

    block = m.format_for_prompt(VALID_MANIFEST)
    assert "<ripple-widget-reference>" in block
    assert "</ripple-widget-reference>" in block
    assert "metric" in block
    assert "KPI tile." in block


def test_format_for_prompt_empty_widgets_returns_empty_string():
    from ee.ripple import manifest as m

    empty = {"schema": "ripple.manifest/v1", "version": "1", "widgets": []}
    assert m.format_for_prompt(empty) == ""


async def test_get_widget_spec_handler_returns_matched_entries(monkeypatch):
    """The get_widget_spec MCP tool returns a formatted reference for the
    requested types, sourced from the manifest."""
    from ee.ripple import manifest as m
    from pocketpaw.agents.sdk_mcp_pocket import _get_widget_spec_handler

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    m._cache.clear()

    result = await _get_widget_spec_handler({"types": ["metric"]})
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert "metric" in text
    assert "KPI tile." in text
    assert "**Props:**" in text


async def test_get_widget_spec_handler_errors_on_empty_types():
    from pocketpaw.agents.sdk_mcp_pocket import _get_widget_spec_handler

    result = await _get_widget_spec_handler({"types": []})
    assert result["is_error"] is True
    assert "non-empty" in result["content"][0]["text"]


async def test_get_widget_spec_handler_errors_on_unknown_only(monkeypatch):
    from ee.ripple import manifest as m
    from pocketpaw.agents.sdk_mcp_pocket import _get_widget_spec_handler

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    m._cache.clear()

    result = await _get_widget_spec_handler({"types": ["nonexistent-widget"]})
    assert result["is_error"] is True
    assert "Unknown types" in result["content"][0]["text"]


async def test_get_widget_spec_handler_partial_match_includes_warning(monkeypatch):
    from ee.ripple import manifest as m
    from pocketpaw.agents.sdk_mcp_pocket import _get_widget_spec_handler

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=VALID_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    m._cache.clear()

    result = await _get_widget_spec_handler({"types": ["metric", "not-a-real-widget"]})
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert "metric" in text
    assert "unknown types skipped" in text
    assert "not-a-real-widget" in text


async def test_format_for_prompt_renders_envelope_before_widgets():
    """The envelope contract must lead the prompt so the agent anchors
    `ui` before reading per-widget examples. If the catalog comes first,
    an LLM scanning examples might infer the wrong top-level shape."""
    from ee.ripple import manifest as m

    out = m.format_for_prompt(VALID_MANIFEST)
    assert "Spec envelope" in out
    assert "Widget catalog" in out
    # Envelope leads — its header appears before the catalog header.
    assert out.index("Spec envelope") < out.index("Widget catalog")
    # The aliases the agent invents are explicitly disallowed.
    for alias in ("root", "tree", "view", "body", "content"):
        assert f"`{alias}`" in out, f"alias {alias!r} not flagged"
    # The example shows the envelope wrapping a UI tree — not a bare node.
    assert '"ui"' in out
    assert '"state"' in out


async def test_format_for_prompt_handles_legacy_manifest_without_envelope():
    """Older manifests (pre-spec-envelope) must still render — the
    envelope section is skipped, the widget catalog still appears."""
    from ee.ripple import manifest as m

    legacy = {**VALID_MANIFEST}
    del legacy["spec"]
    out = m.format_for_prompt(legacy)
    assert out  # not empty
    assert "Spec envelope" not in out
    # Widget catalog still renders.
    assert "metric" in out


async def test_get_widget_spec_handler_errors_when_manifest_unavailable(monkeypatch):
    from ee.ripple import manifest as m
    from pocketpaw.agents.sdk_mcp_pocket import _get_widget_spec_handler

    async def fake_get(self, url, timeout):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    m._cache.clear()

    result = await _get_widget_spec_handler({"types": ["metric"]})
    assert result["is_error"] is True
    assert "unavailable" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# validate_against_manifest — pre-persist drift detector / aliaser.
# Covers the production drift seen on 2026-05-04: timeline.events[].description
# rendered as empty rows. (feed widget was removed in favor of timeline; the
# title→text drift case it formerly tested is gone with it.)
# ---------------------------------------------------------------------------


VALIDATE_MANIFEST = {
    "schema": "ripple.manifest/v1",
    "version": "0.2.0",
    "widgets": [
        {
            "type": "timeline",
            "category": "research",
            "props": {
                "events": {"type": "Array<{ date; title; detail?; ... }>", "required": True},
                "maxItems": {"type": "number", "required": False},
            },
        },
        {
            "type": "stat",
            "category": "display",
            "props": {
                "label": {"type": "string", "required": True},
                "value": {"type": "string | number", "required": True},
            },
        },
    ],
}


def test_validate_returns_empty_for_non_dict_spec():
    from ee.ripple import manifest as m

    assert m.validate_against_manifest(None, VALIDATE_MANIFEST) == []
    assert m.validate_against_manifest("not a dict", VALIDATE_MANIFEST) == []  # type: ignore[arg-type]
    assert m.validate_against_manifest([], VALIDATE_MANIFEST) == []  # type: ignore[arg-type]


def test_validate_clean_spec_yields_no_issues():
    from ee.ripple import manifest as m

    spec = {
        "ui": {
            "type": "timeline",
            "props": {
                "events": [{"date": "Q1 2026", "title": "Launch", "detail": "Shipped X."}],
                "maxItems": 5,
            },
        }
    }
    assert m.validate_against_manifest(spec, VALIDATE_MANIFEST) == []


def test_validate_flags_unknown_top_level_props():
    from ee.ripple import manifest as m

    spec = {
        "ui": {
            "type": "stat",
            "props": {"label": "MRR", "value": "$48k", "deltaPercnt": 12},
        }
    }
    issues = m.validate_against_manifest(spec, VALIDATE_MANIFEST)
    assert len(issues) == 1
    assert issues[0]["type"] == "stat"
    assert issues[0]["unknown_props"] == ["deltaPercnt"]
    assert "label" in issues[0]["allowed_props"]


def test_validate_timeline_drift_warns_without_apply():
    """Production drift: timeline events emitted with `description` instead
    of the manifest's `detail`. apply_aliases=False just reports — the
    spec is NOT mutated."""
    from ee.ripple import manifest as m

    spec = {
        "ui": {
            "type": "timeline",
            "props": {
                "events": [
                    {"date": "Q1 2026", "title": "Launch", "description": "Shipped X."},
                    {"date": "Q2 2026", "title": "Series B", "description": "$50M raised."},
                ]
            },
        }
    }
    issues = m.validate_against_manifest(spec, VALIDATE_MANIFEST, apply_aliases=False)

    # Spec is untouched in warn-only mode.
    assert "description" in spec["ui"]["props"]["events"][0]
    assert "detail" not in spec["ui"]["props"]["events"][0]

    assert len(issues) == 1
    item_issues = issues[0]["item_issues"]
    assert len(item_issues) == 2
    assert all(it["from"] == "description" and it["to"] == "detail" for it in item_issues)
    assert all(it["applied"] is False for it in item_issues)


def test_validate_timeline_description_alias_rewrites():
    """timeline.events[].description -> .detail is a clean rename."""
    from ee.ripple import manifest as m

    spec = {
        "ui": {
            "type": "timeline",
            "props": {
                "events": [
                    {"date": "Q1 2026", "title": "Launch", "description": "Shipped X."},
                ]
            },
        }
    }
    m.validate_against_manifest(spec, VALIDATE_MANIFEST, apply_aliases=True)
    event = spec["ui"]["props"]["events"][0]
    assert event["detail"] == "Shipped X."
    assert "description" not in event


def test_validate_does_not_overwrite_existing_canonical_key():
    """If the agent already emitted the right key alongside the wrong one,
    keep the right one — never clobber correct data with the alias."""
    from ee.ripple import manifest as m

    spec = {
        "ui": {
            "type": "timeline",
            "props": {
                "events": [
                    {"date": "Q1", "title": "T", "detail": "kept", "description": "ignored"}
                ],
            },
        }
    }
    m.validate_against_manifest(spec, VALIDATE_MANIFEST, apply_aliases=True)
    event = spec["ui"]["props"]["events"][0]
    assert event["detail"] == "kept"
    # `description` stayed because `detail` was already there — surfaces as
    # drift in the issue list but the canonical value is preserved.
    assert event["description"] == "ignored"


def test_validate_walks_nested_children():
    """The drift node is buried inside a deep tree. Validator must
    recurse through `children`."""
    from ee.ripple import manifest as m

    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "card",
                    "children": [
                        {
                            "type": "timeline",
                            "props": {
                                "events": [{"date": "Q1", "title": "T", "description": "Buried"}]
                            },
                        }
                    ],
                }
            ],
        }
    }
    issues = m.validate_against_manifest(spec, VALIDATE_MANIFEST, apply_aliases=True)
    assert len(issues) == 1
    # Path locates the offending node.
    assert "children" in issues[0]["path"]
    assert issues[0]["type"] == "timeline"


def test_validate_accepts_bare_node_without_envelope():
    """Older specs / inline-Ripple specs sometimes pass a bare UI node
    without the {ui: ...} envelope. The walker should still descend."""
    from ee.ripple import manifest as m

    bare = {
        "type": "timeline",
        "props": {"events": [{"date": "Q1", "title": "T", "description": "no-envelope"}]},
    }
    issues = m.validate_against_manifest(bare, VALIDATE_MANIFEST, apply_aliases=True)
    assert len(issues) == 1
    assert bare["props"]["events"][0]["detail"] == "no-envelope"


def test_validate_unknown_widget_type_is_ignored():
    """A node whose `type` isn't in the manifest (custom widget, or stale
    catalog) shouldn't crash validation — we only validate what we know."""
    from ee.ripple import manifest as m

    spec = {"ui": {"type": "custom-thing", "props": {"foo": "bar"}}}
    assert m.validate_against_manifest(spec, VALIDATE_MANIFEST) == []


# ---------------------------------------------------------------------------
# agent_context._validate_ripple_spec — the wiring on the write path.
# ---------------------------------------------------------------------------


async def test_agent_context_validator_mutates_spec_in_place(monkeypatch):
    """The wiring inside create_pocket_for_agent / update_pocket_for_agent
    runs with apply_aliases=True and rewrites known drift before the
    pocket reaches the service layer."""
    from ee.cloud.pockets import agent_context
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        return httpx.Response(200, json=VALIDATE_MANIFEST, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    m._cache.clear()

    spec = {
        "ui": {
            "type": "timeline",
            "props": {"events": [{"date": "Q1", "title": "T", "description": "Drifted"}]},
        }
    }
    await agent_context._validate_ripple_spec(spec)
    assert spec["ui"]["props"]["events"][0]["detail"] == "Drifted"
    assert "description" not in spec["ui"]["props"]["events"][0]


async def test_agent_context_validator_skips_when_manifest_unavailable(monkeypatch):
    """Manifest fetch failure is non-fatal — the spec passes through
    untouched and the write proceeds."""
    from ee.cloud.pockets import agent_context
    from ee.ripple import manifest as m

    async def fake_get(self, url, timeout):
        raise httpx.TimeoutException("network down")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    m._cache.clear()

    spec = {
        "ui": {
            "type": "timeline",
            "props": {"events": [{"date": "Q1", "title": "T", "description": "kept-as-is"}]},
        }
    }
    await agent_context._validate_ripple_spec(spec)
    # Spec unchanged when validator can't reach the manifest.
    assert spec["ui"]["props"]["events"][0] == {
        "date": "Q1",
        "title": "T",
        "description": "kept-as-is",
    }


async def test_agent_context_validator_handles_none_spec(monkeypatch):
    """``ripple_spec`` is optional on update — None / non-dict shouldn't
    crash the pre-persist hook."""
    from ee.cloud.pockets import agent_context

    # No manifest fetch at all when input is non-dict — guard returns early.
    await agent_context._validate_ripple_spec(None)
    await agent_context._validate_ripple_spec("not-a-dict")  # type: ignore[arg-type]
