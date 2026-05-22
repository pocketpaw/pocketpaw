# tests/cloud/test_ripple_validator.py
# Updated: 2026-05-22 (Increment 5) — added the catalog-as-allowlist gate
# tests: CatalogViolationError, validate_against_catalog_strict/_logged,
# the embed URL/host policy via the validator, and format_violations_for_agent.
"""Tests for ripple_validator — grammar warnings on AI-generated specs."""

from __future__ import annotations

import logging

import pytest

# EE-gated: skip cleanly in the `Test (OSS-only)` CI scope, which has no
# pocketpaw_ee on disk. (tests/cloud/conftest.py also gates the tree.)
pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.ripple_validator import (  # noqa: E402
    CatalogViolationError,
    ExpressionWarning,
    RippleSpecGrammarError,
    format_violations_for_agent,
    format_warnings_for_agent,
    validate_against_catalog_logged,
    validate_against_catalog_strict,
    validate_ripple_spec,
    validate_ripple_spec_strict,
)


def _codes(warnings: list[ExpressionWarning]) -> list[str]:
    return [w.code for w in warnings]


def test_empty_spec_has_no_warnings() -> None:
    assert validate_ripple_spec(None) == []
    assert validate_ripple_spec({}) == []


def test_supported_expressions_pass() -> None:
    spec = {
        "state": {"language_filter": "All", "sort_by": "stars", "all_repos": []},
        "ui": {
            "type": "table",
            "props": {
                "columns": [{"accessorKey": "name", "header": "Name"}],
                "rows": (
                    "{state.all_repos.where('language', state.language_filter)"
                    ".sortBy(state.sort_by, 'desc')}"
                ),
            },
        },
    }
    assert validate_ripple_spec(spec) == []


def test_array_object_literal_in_ternary_is_supported() -> None:
    # Regression for the Note-to-Teammate pocket — the array-of-objects
    # fallback in a ternary's else branch is supported by the resolver
    # (post-P1.A) and must not warn.
    spec = {
        "ui": {
            "type": "select",
            "props": {
                "options": (
                    "{state.team.length > 0 "
                    "? state.team "
                    ": [{value: 'placeholder', label: 'No teammates'}]}"
                )
            },
        }
    }
    assert validate_ripple_spec(spec) == []


def test_unknown_method_warns() -> None:
    spec = {
        "ui": {
            "type": "table",
            "props": {"rows": "{state.repos.flatMap(r => r.tags)}"},
        }
    }
    warnings = validate_ripple_spec(spec)
    codes = _codes(warnings)
    # Expect both an "unknown_method" warning for `.flatMap(...)` and a
    # "forbidden_syntax" warning for the arrow function inside it.
    assert "unknown_method" in codes
    assert "forbidden_syntax" in codes


def test_arrow_function_in_expression_warns() -> None:
    spec = {"ui": {"props": {"x": "{state.items.map(i => i.name)}"}}}
    warnings = validate_ripple_spec(spec)
    assert any(w.code == "forbidden_syntax" for w in warnings)


def test_unbalanced_brackets_warn() -> None:
    spec = {"ui": {"props": {"x": "{state.foo.where('a', 'b'}"}}}
    warnings = validate_ripple_spec(spec)
    assert any(w.code == "unbalanced_brackets" for w in warnings)


def test_state_initial_values_are_not_treated_as_expressions() -> None:
    # A literal sentence with `{}` inside `state` shouldn't trip the
    # validator — it's seed data, not an expression.
    spec = {"state": {"note": "Use the syntax {state.x} to read state"}}
    assert validate_ripple_spec(spec) == []


def test_warning_contains_field_path_and_expression() -> None:
    spec = {
        "ui": {
            "type": "flex",
            "children": [
                {
                    "type": "table",
                    "props": {"rows": "{state.repos.bogus()}"},
                }
            ],
        }
    }
    warnings = validate_ripple_spec(spec)
    assert len(warnings) == 1
    w = warnings[0]
    assert "rows" in w.path
    assert "bogus" in w.expression
    assert w.code == "unknown_method"


def test_strict_mode_raises_with_full_warnings_attached() -> None:
    spec = {"ui": {"props": {"x": "{state.items.map(i => i.name)}"}}}
    with pytest.raises(RippleSpecGrammarError) as ei:
        validate_ripple_spec_strict(spec)
    # The exception carries the structured warnings, not just a string.
    assert len(ei.value.warnings) >= 1
    assert any(w.code == "forbidden_syntax" for w in ei.value.warnings)


def test_strict_mode_passes_clean_spec() -> None:
    spec = {"ui": {"props": {"rows": "{state.repos.where('lang', 'TS').sortBy('stars', 'desc')}"}}}
    # Must not raise.
    validate_ripple_spec_strict(spec)


def test_format_warnings_for_agent_lists_each_finding() -> None:
    spec = {"ui": {"props": {"x": "{state.items.map(i => i.name)}"}}}
    warnings = validate_ripple_spec(spec)
    msg = format_warnings_for_agent(warnings)
    assert "unsupported expressions" in msg
    assert "state.items.map" in msg


def test_format_warnings_for_agent_empty_returns_empty_string() -> None:
    assert format_warnings_for_agent([]) == ""


def test_whitelisted_methods_do_not_warn() -> None:
    # Confirm every method in the resolver whitelist passes.
    methods = [
        "trim()",
        "toLowerCase()",
        "toUpperCase()",
        "includes('x')",
        "startsWith('x')",
        "endsWith('x')",
        "join(',')",
        "sum('value')",
        "count()",
        "first()",
        "last()",
        "reverse()",
        "where('k', 'v')",
        "whereIn('k', state.values)",
        "sortBy('k', 'desc')",
        "limit(5)",
        "toFixed(2)",
    ]
    for m in methods:
        spec = {"ui": {"props": {"x": f"{{state.collection.{m}}}"}}}
        assert validate_ripple_spec(spec) == [], f"method {m} should be allowed"


# ---------------------------------------------------------------------------
# Catalog-as-allowlist gate (Increment 5)
# ---------------------------------------------------------------------------

_ALLOWED = ["flex", "card", "stat", "chart", "embed"]
_EMBED_HOSTS = ["codepen.io", "www.figma.com"]


def test_catalog_strict_passes_a_valid_spec() -> None:
    spec = {"ui": {"type": "flex", "children": [{"type": "stat", "props": {}}]}}
    # Must not raise.
    validate_against_catalog_strict(spec, _ALLOWED, embed_allowed_hosts=_EMBED_HOSTS)


def test_catalog_strict_raises_on_unknown_type() -> None:
    spec = {"ui": {"type": "flex", "children": [{"type": "revenue-card", "props": {}}]}}
    with pytest.raises(CatalogViolationError) as exc:
        validate_against_catalog_strict(spec, _ALLOWED, embed_allowed_hosts=_EMBED_HOSTS)
    assert len(exc.value.violations) == 1
    assert exc.value.violations[0]["type"] == "revenue-card"


def test_catalog_strict_suggests_nearest_match() -> None:
    """A near-miss type name gets a corrective suggestion."""
    spec = {"ui": {"type": "carde", "children": []}}
    with pytest.raises(CatalogViolationError) as exc:
        validate_against_catalog_strict(spec, _ALLOWED, embed_allowed_hosts=_EMBED_HOSTS)
    assert exc.value.violations[0]["suggestion"] == "card"


def test_catalog_strict_raises_on_bad_embed_url() -> None:
    spec = {
        "ui": {
            "type": "embed",
            "props": {"mode": "url", "url": "http://codepen.io/x"},
        }
    }
    with pytest.raises(CatalogViolationError) as exc:
        validate_against_catalog_strict(spec, _ALLOWED, embed_allowed_hosts=_EMBED_HOSTS)
    assert "reason" in exc.value.violations[0]


def test_catalog_logged_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    spec = {"ui": {"type": "flex", "children": [{"type": "made-up", "props": {}}]}}
    with caplog.at_level(logging.WARNING):
        violations = validate_against_catalog_logged(
            spec, _ALLOWED, embed_allowed_hosts=_EMBED_HOSTS
        )
    # Logged path returns the violations but never raises.
    assert len(violations) == 1
    assert violations[0]["type"] == "made-up"
    assert any("unknown_widget_type" in r.message for r in caplog.records)


def test_catalog_logged_warns_on_embed_violation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = {
        "ui": {
            "type": "embed",
            "props": {"mode": "url", "url": "https://not-allowed.test/x"},
        }
    }
    with caplog.at_level(logging.WARNING):
        violations = validate_against_catalog_logged(
            spec, _ALLOWED, embed_allowed_hosts=_EMBED_HOSTS
        )
    assert len(violations) == 1
    assert any("embed_policy_violation" in r.message for r in caplog.records)


def test_catalog_embed_loopback_blocked_even_with_wildcard() -> None:
    """A `["*"]` embed allow-list must NOT re-enable an internal host."""
    spec = {
        "ui": {
            "type": "embed",
            "props": {"mode": "url", "url": "https://169.254.169.254/latest/meta-data/"},
        }
    }
    with pytest.raises(CatalogViolationError):
        validate_against_catalog_strict(spec, _ALLOWED, embed_allowed_hosts=["*"])


def test_format_violations_for_agent_is_actionable() -> None:
    violations = [
        {"path": "ui.children[0]", "type": "kpi-tile", "suggestion": "stat"},
        {"path": "ui.children[1]", "url": "http://x.test", "reason": "must be https"},
    ]
    text = format_violations_for_agent(violations)
    assert "kpi-tile" in text
    assert "stat" in text  # suggestion surfaced
    assert "must be https" in text
    assert format_violations_for_agent([]) == ""


def test_catalog_violation_error_message_caps_at_20() -> None:
    violations = [
        {"path": f"ui.children[{i}]", "type": f"bad-{i}", "suggestion": None} for i in range(25)
    ]
    err = CatalogViolationError(violations)
    assert err.violations == violations
    assert "and 5 more" in str(err)
