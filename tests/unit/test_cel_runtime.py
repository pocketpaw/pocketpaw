# tests/unit/test_cel_runtime.py
# Created: 2026-05-28 (feat/rfc-03-v2-cel-eval) — unit tests for the
# CEL runtime evaluator (``cel_runtime.evaluate_cel``) plus the
# ``CelEvaluationError`` exception path. Targets the PR 2c scope from
# RFC 03 v2 — parse-side (``expressions.CelExpression``) already lives
# in main; this is the runtime layer that the Instinct 5-step
# composer (PR 2d), the temporal trigger sweeper (PR 2f), and the
# Fabric ``tier: registered`` linter (PR 2g) will all consume.
"""Tests for the CEL runtime evaluator.

Covered cases (per the PR 2c brief):

* Numeric comparison: ``days_remaining <= 30``.
* String equality: ``renewal_stage == 'sent'``.
* Null compare: ``privilege_flag == null``.
* Arithmetic + comparison (RFC worked example A):
  ``rent_proposed < rent_current * 0.95``.
* Joined-entity dot-path declared on the template:
  ``tenant.late_payment_count_12mo >= 3``.
* Joined-entity NOT declared on the template → ``CelEvaluationError``
  naming the undeclared root.
* Custom temporal function: ``within(expires_at, duration('60d'))``
  with deterministic ``now`` injected.
* Missing identifier → ``CelEvaluationError`` with the identifier name.
* Type error (string < int) → ``CelEvaluationError``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pocketpaw.bundled_templates import (
    CelEvaluationError,
    PocketTemplate,
    TemplateIdentifierResolver,
    evaluate_cel,
)

# ---------------------------------------------------------------------------
# Template fixtures (minimal — only the bits the resolver inspects)
# ---------------------------------------------------------------------------


def _lease_template() -> PocketTemplate:
    """A skinny template that declares the ``tenant`` joined entity so
    dot-paths in CEL resolve through ``TemplateIdentifierResolver``."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "lease-renewal-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "property-management",
            "display_name": "Lease Renewal (test)",
            "description": "Skinny lease-renewal fixture for CEL runtime tests.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Lease",
                "id_field": "id",
                "joined_entities": [
                    {"name": "tenant", "entity_type": "Tenant", "via_link": "lease_tenant"},
                ],
                "columns": [
                    {"field": "id", "widget": "text"},
                    {"field": "days_remaining", "widget": "trend"},
                    {"field": "renewal_stage", "widget": "status_dot"},
                    {"field": "rent_proposed", "widget": "currency_editable"},
                    {"field": "rent_current", "widget": "currency"},
                    {"field": "privilege_flag", "widget": "text"},
                    {"field": "expires_at", "widget": "date"},
                    {"field": "tenant.late_payment_count_12mo", "widget": "trend"},
                ],
            },
        }
    )


def _flat_template() -> PocketTemplate:
    """A template with NO joined entities — used to prove that a
    dotted path against an undeclared root raises."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "flat-only",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "general",
            "display_name": "Flat only",
            "description": "Template without joined entities.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [{"field": "id", "widget": "text"}],
            },
        }
    )


@pytest.fixture
def resolver() -> TemplateIdentifierResolver:
    return TemplateIdentifierResolver(_lease_template())


# ---------------------------------------------------------------------------
# Plain comparisons & arithmetic
# ---------------------------------------------------------------------------


def test_days_remaining_le_30_true(resolver: TemplateIdentifierResolver) -> None:
    result = evaluate_cel("days_remaining <= 30", {"days_remaining": 25}, resolver)
    assert result is True


def test_days_remaining_le_30_false(resolver: TemplateIdentifierResolver) -> None:
    result = evaluate_cel("days_remaining <= 30", {"days_remaining": 60}, resolver)
    assert result is False


def test_renewal_stage_equality(resolver: TemplateIdentifierResolver) -> None:
    assert evaluate_cel("renewal_stage == 'sent'", {"renewal_stage": "sent"}, resolver) is True
    assert evaluate_cel("renewal_stage == 'sent'", {"renewal_stage": "draft"}, resolver) is False


def test_null_compare(resolver: TemplateIdentifierResolver) -> None:
    assert evaluate_cel("privilege_flag == null", {"privilege_flag": None}, resolver) is True
    assert evaluate_cel("privilege_flag == null", {"privilege_flag": "x"}, resolver) is False


def test_lease_renewal_block_rule_worked_example_a(
    resolver: TemplateIdentifierResolver,
) -> None:
    # RFC 03 v2 worked example A: rent_proposed < rent_current * 0.95
    # is the "block 5%+ rent cut" Instinct rule.
    context = {"rent_proposed": 1800.0, "rent_current": 2000.0}
    assert evaluate_cel("rent_proposed < rent_current * 0.95", context, resolver) is True
    context_ok = {"rent_proposed": 1950.0, "rent_current": 2000.0}
    assert evaluate_cel("rent_proposed < rent_current * 0.95", context_ok, resolver) is False


# ---------------------------------------------------------------------------
# Joined-entity dot-paths
# ---------------------------------------------------------------------------


def test_joined_entity_dot_path_true(resolver: TemplateIdentifierResolver) -> None:
    context = {"tenant": {"late_payment_count_12mo": 4, "name": "alice"}}
    assert evaluate_cel("tenant.late_payment_count_12mo >= 3", context, resolver) is True


def test_joined_entity_dot_path_false(resolver: TemplateIdentifierResolver) -> None:
    context = {"tenant": {"late_payment_count_12mo": 1, "name": "alice"}}
    assert evaluate_cel("tenant.late_payment_count_12mo >= 3", context, resolver) is False


def test_undeclared_joined_entity_raises() -> None:
    """``vendor`` is NOT declared on the template; the resolver gates
    the lookup and ``evaluate_cel`` surfaces a typed error."""
    flat_resolver = TemplateIdentifierResolver(_flat_template())
    with pytest.raises(CelEvaluationError) as exc:
        evaluate_cel("vendor.foo == 1", {}, flat_resolver)
    assert "vendor" in str(exc.value)


# ---------------------------------------------------------------------------
# within() custom function — deterministic via injected ``now``
# ---------------------------------------------------------------------------


def test_within_inside_window(resolver: TemplateIdentifierResolver) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expires_at = now + timedelta(days=30)
    context = {"expires_at": expires_at}
    assert evaluate_cel("within(expires_at, duration('60d'))", context, resolver, now=now) is True


def test_within_outside_window(resolver: TemplateIdentifierResolver) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expires_at = now + timedelta(days=100)
    context = {"expires_at": expires_at}
    assert evaluate_cel("within(expires_at, duration('60d'))", context, resolver, now=now) is False


def test_within_past_inside_window(resolver: TemplateIdentifierResolver) -> None:
    """within(field, d) is symmetric: now() - d <= field <= now() + d."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expires_at = now - timedelta(days=30)
    context = {"expires_at": expires_at}
    assert evaluate_cel("within(expires_at, duration('60d'))", context, resolver, now=now) is True


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_identifier_raises(resolver: TemplateIdentifierResolver) -> None:
    with pytest.raises(CelEvaluationError) as exc:
        evaluate_cel("days_remaining <= 30", {}, resolver)
    # Error message must name the missing identifier so authors can fix
    # it without re-running with a debugger.
    assert "days_remaining" in str(exc.value)


def test_type_error_raises(resolver: TemplateIdentifierResolver) -> None:
    # string < int — celpy raises CELEvalError; the runtime wraps it.
    with pytest.raises(CelEvaluationError):
        evaluate_cel(
            "renewal_stage < 5",
            {"renewal_stage": "sent"},
            resolver,
        )


def test_malformed_expression_raises(resolver: TemplateIdentifierResolver) -> None:
    # Parse failure at eval time (the chokepoint catches most syntax
    # errors earlier, but evaluate_cel is also called directly with raw
    # strings — it must surface a CelEvaluationError, not a stray
    # celpy ``CELParseError``.)
    with pytest.raises(CelEvaluationError):
        evaluate_cel("days_remaining <=", {"days_remaining": 1}, resolver)
