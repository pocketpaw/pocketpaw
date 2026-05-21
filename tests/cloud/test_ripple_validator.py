"""Tests for ripple_validator — grammar warnings on AI-generated specs."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ripple_validator import (
    ExpressionWarning,
    RippleSpecGrammarError,
    format_warnings_for_agent,
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
