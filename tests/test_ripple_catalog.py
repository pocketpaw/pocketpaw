# tests/test_ripple_catalog.py
# Created: 2026-05-22 (Increment 5) — OSS-core tests for the
# catalog-as-allowlist ingest gate (`validate_against_catalog`) and the
# `embed` URL/host policy in `pocketpaw.ripple.manifest`. No EE imports,
# so this file runs in the `Test (OSS-only)` CI scope.
"""Tests for the Ripple catalog allow-list gate + embed URL/host policy."""

from __future__ import annotations

from pocketpaw.ripple.manifest import (
    DEFAULT_EMBED_ALLOWED_HOSTS,
    allowed_types_from_manifest,
    check_embed_node,
    check_embed_nodes_in_spec,
    check_embed_url,
    find_embed_nodes,
    validate_against_catalog,
)

# A representative widget allow-list (the widget-manifest type list).
ALLOWED = ["flex", "card", "stat", "chart", "table", "embed", "model-viewer"]


# ---------------------------------------------------------------------------
# validate_against_catalog
# ---------------------------------------------------------------------------


def test_catalog_returns_empty_for_non_dict_spec() -> None:
    assert validate_against_catalog(None, ALLOWED) == []
    assert validate_against_catalog("not a dict", ALLOWED) == []  # type: ignore[arg-type]
    assert validate_against_catalog([], ALLOWED) == []  # type: ignore[arg-type]


def test_catalog_valid_spec_passes() -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "stat", "props": {"label": "Revenue", "value": 42}},
                {"type": "chart", "props": {"type": "bar", "data": []}},
            ],
        }
    }
    assert validate_against_catalog(spec, ALLOWED) == []


def test_catalog_allows_control_flow_types() -> None:
    """`if` and `each` are spec grammar — always allowed even though they
    are not in the widget allow-list."""
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "each", "items": "{state.rows}", "children": []},
                {"type": "if", "condition": "{state.ok}", "children": []},
            ],
        }
    }
    assert validate_against_catalog(spec, ALLOWED) == []


def test_catalog_flags_unknown_type() -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [{"type": "revenue-card", "props": {}}],
        }
    }
    issues = validate_against_catalog(spec, ALLOWED)
    assert len(issues) == 1
    assert issues[0]["path"] == "ui.children[0]"
    assert issues[0]["type"] == "revenue-card"


def test_catalog_suggests_nearest_match() -> None:
    """An unknown type close to a catalog widget gets a suggestion."""
    spec = {"ui": {"type": "carde", "children": []}}
    issues = validate_against_catalog(spec, ALLOWED)
    assert len(issues) == 1
    assert issues[0]["type"] == "carde"
    assert issues[0]["suggestion"] == "card"


def test_catalog_suggestion_is_none_when_nothing_close() -> None:
    spec = {"ui": {"type": "zzzzzzzz", "children": []}}
    issues = validate_against_catalog(spec, ALLOWED)
    assert len(issues) == 1
    assert issues[0]["suggestion"] is None


def test_catalog_suggestion_never_points_at_control_flow() -> None:
    """A mistyped widget name must not be 'corrected' to `if` / `each`."""
    spec = {"ui": {"type": "eachh", "children": []}}
    issues = validate_against_catalog(spec, ALLOWED)
    assert len(issues) == 1
    assert issues[0]["suggestion"] != "each"


def test_catalog_walks_nested_and_else_children() -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "if",
                    "condition": "{state.x}",
                    "children": [{"type": "bogus-a", "props": {}}],
                    "else_children": [{"type": "bogus-b", "props": {}}],
                }
            ],
        }
    }
    issues = validate_against_catalog(spec, ALLOWED)
    types = sorted(i["type"] for i in issues)
    assert types == ["bogus-a", "bogus-b"]


def test_catalog_accepts_bare_node_without_ui_envelope() -> None:
    bare = {"type": "made-up", "children": []}
    issues = validate_against_catalog(bare, ALLOWED)
    assert len(issues) == 1
    assert issues[0]["type"] == "made-up"


def test_catalog_allowed_types_from_manifest() -> None:
    manifest = {
        "widgets": [
            {"type": "stat"},
            {"type": "chart"},
            {"type": "stat"},  # duplicate
            {"type": ""},  # ignored
            {"no_type": 1},  # ignored
        ]
    }
    assert allowed_types_from_manifest(manifest) == ["chart", "stat"]


# ---------------------------------------------------------------------------
# Embed URL / host policy
# ---------------------------------------------------------------------------


def test_embed_url_https_allowlisted_host_passes() -> None:
    assert check_embed_url("https://codepen.io/team/pen/abc", DEFAULT_EMBED_ALLOWED_HOSTS) is None


def test_embed_url_subdomain_of_allowlisted_host_passes() -> None:
    assert check_embed_url("https://cdn.codepen.io/x", DEFAULT_EMBED_ALLOWED_HOSTS) is None


def test_embed_url_http_scheme_rejected() -> None:
    reason = check_embed_url("http://codepen.io/x", DEFAULT_EMBED_ALLOWED_HOSTS)
    assert reason is not None
    assert "https" in reason


def test_embed_url_non_allowlisted_host_rejected() -> None:
    reason = check_embed_url("https://evil.example.com/x", DEFAULT_EMBED_ALLOWED_HOSTS)
    assert reason is not None
    assert "allow-list" in reason


def test_embed_url_empty_rejected() -> None:
    assert check_embed_url("", DEFAULT_EMBED_ALLOWED_HOSTS) is not None
    assert check_embed_url(None, DEFAULT_EMBED_ALLOWED_HOSTS) is not None


def test_embed_url_wildcard_allowlist_permits_any_public_host() -> None:
    assert check_embed_url("https://anything.example.com/x", ["*"]) is None


def test_embed_url_loopback_hard_blocked_even_with_wildcard_allowlist() -> None:
    """The internal-host hard block must survive a `["*"]` allow-list."""
    for url in (
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://10.0.0.5/x",
        "https://192.168.1.1/x",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
    ):
        reason = check_embed_url(url, ["*"])
        assert reason is not None, f"{url} must be blocked even with ['*']"
        assert "block" in reason.lower()


def test_embed_node_url_mode_checked() -> None:
    node = {"type": "embed", "props": {"mode": "url", "url": "http://codepen.io/x"}}
    assert check_embed_node(node, DEFAULT_EMBED_ALLOWED_HOSTS) is not None


def test_embed_node_srcdoc_mode_not_url_checked() -> None:
    """A `srcdoc` embed carries no URL boundary — it is not checked."""
    node = {"type": "embed", "props": {"mode": "srcdoc", "srcdoc": "<canvas></canvas>"}}
    assert check_embed_node(node, DEFAULT_EMBED_ALLOWED_HOSTS) is None


def test_embed_node_non_embed_passes() -> None:
    assert check_embed_node({"type": "card"}, DEFAULT_EMBED_ALLOWED_HOSTS) is None


def test_find_embed_nodes_walks_tree() -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "embed", "props": {"mode": "url", "url": "https://codepen.io/a"}},
                {
                    "type": "if",
                    "condition": "{state.x}",
                    "else_children": [
                        {"type": "embed", "props": {"mode": "srcdoc", "srcdoc": "<p>x</p>"}}
                    ],
                },
            ],
        }
    }
    assert len(find_embed_nodes(spec)) == 2


def test_check_embed_nodes_in_spec_flags_violations() -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {"type": "embed", "props": {"mode": "url", "url": "https://codepen.io/ok"}},
                {"type": "embed", "props": {"mode": "url", "url": "http://evil.test/x"}},
            ],
        }
    }
    issues = check_embed_nodes_in_spec(spec, DEFAULT_EMBED_ALLOWED_HOSTS)
    assert len(issues) == 1
    assert issues[0]["path"] == "ui.children[1]"
    assert issues[0]["url"] == "http://evil.test/x"


def test_check_embed_nodes_in_spec_clean_spec_passes() -> None:
    spec = {
        "ui": {
            "type": "embed",
            "props": {"mode": "url", "url": "https://www.figma.com/file/abc"},
        }
    }
    assert check_embed_nodes_in_spec(spec, DEFAULT_EMBED_ALLOWED_HOSTS) == []
