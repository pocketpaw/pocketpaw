"""Composio Connect Link tests — inline Ripple shape + auth-error detection."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud.composio import connect_link
from pocketpaw_ee.cloud.composio.connect_link import (
    as_inline_ripple,
    detect_connect_link,
    maybe_emit_connect_link,
)
from pocketpaw_ee.cloud.ripple_normalizer import normalize_ripple_spec

from pocketpaw.config import Settings

# ---------------------------------------------------------------------------
# as_inline_ripple — spec shape
# ---------------------------------------------------------------------------


def test_as_inline_ripple_has_canonical_envelope() -> None:
    spec = as_inline_ripple("https://composio.dev/auth/abc", "gmail")
    assert spec["version"] == "1.0"
    assert spec["ui"]["type"] == "flex"
    assert "children" in spec["ui"]


def test_as_inline_ripple_button_targets_blank_tab() -> None:
    spec = as_inline_ripple("https://composio.dev/auth/abc", "gmail")
    buttons = [c for c in spec["ui"]["children"] if c["type"] == "button"]
    assert len(buttons) == 1
    actions = buttons[0]["actions"]
    assert actions[0]["type"] == "open_url"
    assert actions[0]["url"] == "https://composio.dev/auth/abc"
    assert actions[0]["target"] == "_blank"


def test_as_inline_ripple_default_label_includes_toolkit() -> None:
    spec = as_inline_ripple("https://composio.dev/auth/abc", "slack")
    buttons = [c for c in spec["ui"]["children"] if c["type"] == "button"]
    assert buttons[0]["props"]["label"] == "Connect Slack"


def test_as_inline_ripple_respects_custom_label() -> None:
    spec = as_inline_ripple(
        "https://composio.dev/auth/abc", "slack", action_label="Authorize Slack workspace"
    )
    buttons = [c for c in spec["ui"]["children"] if c["type"] == "button"]
    assert buttons[0]["props"]["label"] == "Authorize Slack workspace"


def test_as_inline_ripple_passes_ripple_normalizer() -> None:
    """The normalizer accepts ``{version, ui}`` shape without dropping
    our button + actions. This is the guarantee the chat layer relies on."""
    spec = as_inline_ripple("https://composio.dev/auth/abc", "gmail")
    normalized = normalize_ripple_spec(spec)
    assert normalized is not None
    assert normalized.get("version") == "1.0"
    assert isinstance(normalized.get("ui"), dict)
    # Button + open_url action should survive normalization.
    children = normalized["ui"].get("children", [])
    buttons = [c for c in children if isinstance(c, dict) and c.get("type") == "button"]
    assert buttons
    assert buttons[0]["actions"][0]["type"] == "open_url"


def test_as_inline_ripple_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match="url"):
        as_inline_ripple("", "gmail")


def test_as_inline_ripple_rejects_empty_toolkit() -> None:
    with pytest.raises(ValueError, match="toolkit"):
        as_inline_ripple("https://x", "")


# ---------------------------------------------------------------------------
# detect_connect_link — heuristic auth-response detection
# ---------------------------------------------------------------------------


def test_detect_explicit_connect_url_key() -> None:
    payload = {
        "ok": False,
        "connect_url": "https://composio.dev/auth/abc",
        "toolkit": "gmail",
    }
    result = detect_connect_link(payload)
    assert result == ("https://composio.dev/auth/abc", "gmail")


def test_detect_nested_connection_block() -> None:
    payload = {
        "ok": False,
        "needs_connection": True,
        "connection": {"auth_url": "https://composio.dev/auth/xyz"},
        "app": "slack",
    }
    result = detect_connect_link(payload)
    assert result is not None
    url, toolkit = result
    assert url == "https://composio.dev/auth/xyz"
    assert toolkit == "slack"


def test_detect_returns_none_for_random_url_without_auth_flag() -> None:
    """A URL elsewhere in the payload must NOT trigger detection — too
    much false-positive risk. Only auth-named keys or explicit flags count."""
    payload = {"ok": True, "data": [{"id": "1", "url": "https://example.com/article"}]}
    assert detect_connect_link(payload) is None


def test_detect_returns_none_for_non_dict() -> None:
    assert detect_connect_link("a string") is None
    assert detect_connect_link(None) is None
    assert detect_connect_link(["list"]) is None


def test_detect_returns_none_when_url_missing() -> None:
    payload = {"needs_connection": True, "toolkit": "gmail"}
    assert detect_connect_link(payload) is None


def test_detect_truthy_string_flag() -> None:
    payload = {
        "requires_auth": "true",
        "authorization_url": "https://composio.dev/auth/abc",
    }
    result = detect_connect_link(payload)
    assert result is not None
    assert result[0] == "https://composio.dev/auth/abc"


# ---------------------------------------------------------------------------
# maybe_emit_connect_link — SSE push + feature flag
# ---------------------------------------------------------------------------


def _enabled_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "composio_api_key": "ck_test",
        "composio_enterprise_id": "ent_acme",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_emit_skipped_when_feature_flag_disabled() -> None:
    s = _enabled_settings(composio_connect_link_inline=False)
    payload = {"connect_url": "https://x", "toolkit": "gmail"}
    with patch("pocketpaw_ee.cloud.chat.agent_service.push_sse_event") as push:
        emitted = maybe_emit_connect_link(payload, s)
    assert emitted is False
    push.assert_not_called()


def test_emit_skipped_when_no_connect_link_detected() -> None:
    s = _enabled_settings()
    with patch("pocketpaw_ee.cloud.chat.agent_service.push_sse_event") as push:
        emitted = maybe_emit_connect_link({"ok": True, "data": []}, s)
    assert emitted is False
    push.assert_not_called()


def test_emit_pushes_ripple_inline_event() -> None:
    s = _enabled_settings()
    payload = {
        "ok": False,
        "needs_connection": True,
        "connect_url": "https://composio.dev/auth/abc",
        "toolkit": "gmail",
    }
    with patch("pocketpaw_ee.cloud.chat.agent_service.push_sse_event") as push:
        emitted = maybe_emit_connect_link(payload, s)
    assert emitted is True
    push.assert_called_once()
    event_name, data = push.call_args[0]
    assert event_name == "ripple_inline"
    assert data["source"] == "composio"
    assert data["toolkit"] == "gmail"
    assert data["spec"]["version"] == "1.0"
    # And the embedded spec is the same one as_inline_ripple would build.
    actions = data["spec"]["ui"]["children"][-1]["actions"]
    assert actions[0]["url"] == "https://composio.dev/auth/abc"


def test_emit_uses_fallback_toolkit_label_when_unknown() -> None:
    s = _enabled_settings()
    payload = {
        "needs_connection": True,
        "connect_url": "https://composio.dev/auth/abc",
        # no toolkit / app field — emitter substitutes a generic label
    }
    with patch("pocketpaw_ee.cloud.chat.agent_service.push_sse_event") as push:
        emitted = maybe_emit_connect_link(payload, s)
    assert emitted is True
    _name, data = push.call_args[0]
    assert data["toolkit"] == "this integration"


# Note on architecture: in v2 of the wiring, Composio talks to the
# parent agent via its own hosted MCP server (see
# ``ee/pocketpaw_ee/cloud/composio/mcp.py``). Connect Link responses arrive at the
# agent as MCP tool results, not through our in-process handler — so
# there's no integration test here for an ``_execute_handler``.
# ``as_inline_ripple`` + ``detect_connect_link`` remain as utility
# functions for any downstream consumer (e.g. an agent-router hook
# that intercepts MCP tool results to render inline UI).


# Keep import alive even though only referenced indirectly via patches.
_ = connect_link
_ = asyncio
