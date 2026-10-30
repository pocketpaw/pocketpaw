# tests/unit/test_fabric_validator.py
# Created: 2026-05-28 (feat/rfc-03-v2-fabric) — unit tests for
# ``validate_template_with_registry``, the lint-time Fabric
# ``tier: registered`` enforcement entry point. Pairs with
# ``test_fabric_resolver.py`` (runtime side) — same Protocol, different
# call site. Validator returns a list of typed errors so callers
# (CLI ``template lint``, future tools) can render all of them at once
# instead of stopping on the first miss.
"""Tests for ``validate_template_with_registry``.

The validator is the lint-time complement to ``FabricResolver``:

* For ``state.entity_type`` — error when the registry doesn't know the
  type AND the template uses dot-paths (synthetic-tier templates with
  no dots stay clean).
* For each ``state.joined_entities[i]`` — error when ``entity_type`` is
  not registered, or when ``via_link`` isn't registered between
  primary and join.
* For each CEL expression on the template (``saved_views[].filter``,
  ``columns[].filter``, ``triggers[].when`` / ``.filter``,
  ``instinct_rules.rules[].when``) and for column ``field``-side
  dot-paths — error when the leading segment isn't a declared join.

Multiple errors collect in a single call. ``NullFabricRegistry`` is
the no-Fabric default: it returns clean for synthetic templates (no
dots) and surfaces errors for any template that demands joins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pocketpaw.bundled_templates import (
    FabricValidationError,
    NullFabricRegistry,
    PocketTemplate,
    validate_template_with_registry,
)

# Repo-root anchor — tests run from anywhere in the worktree, so we
# resolve fixtures relative to this file rather than ``cwd``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEASE_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "templates" / "lease-renewal-v2.yaml"
_TODO_FIXTURE = (
    _REPO_ROOT
    / "src"
    / "pocketpaw"
    / "bundled_templates"
    / "_bundled"
    / "todo-task-tracker"
    / "template.pocket.yaml"
)


# ---------------------------------------------------------------------------
# Mock registry
# ---------------------------------------------------------------------------


class _MockRegistry:
    """Configurable mock — built once per test, immutable thereafter."""

    def __init__(
        self,
        *,
        entities: set[str] | None = None,
        links: set[tuple[str, str, str]] | None = None,
        properties: dict[str, set[str]] | None = None,
    ) -> None:
        self._entities = entities or set()
        self._links = links or set()
        self._properties = properties or {}

    def entity_type_exists(self, name: str) -> bool:
        return name in self._entities

    def link_exists(self, from_type: str, to_type: str, link_name: str) -> bool:
        return (from_type, to_type, link_name) in self._links

    def get_entity_properties(self, name: str) -> set[str]:
        return self._properties.get(name, set())


# ---------------------------------------------------------------------------
# Template loaders
# ---------------------------------------------------------------------------


def _load_lease_template() -> PocketTemplate:
    data = yaml.safe_load(_LEASE_FIXTURE.read_text())
    return PocketTemplate.model_validate(data)


def _load_todo_template() -> PocketTemplate:
    data = yaml.safe_load(_TODO_FIXTURE.read_text())
    return PocketTemplate.model_validate(data)


def _clean_lease_registry() -> _MockRegistry:
    return _MockRegistry(
        entities={"Lease", "Tenant", "Property"},
        links={
            ("Lease", "Tenant", "lease_tenant"),
            ("Lease", "Property", "lease_property"),
        },
    )


# ---------------------------------------------------------------------------
# Error model contract
# ---------------------------------------------------------------------------


def test_validation_error_is_frozen_pydantic() -> None:
    err = FabricValidationError(
        message="boom",
        path="state.entity_type",
        severity="error",
        data={"entity_type": "Foo"},
    )
    assert err.message == "boom"
    # ``frozen=True`` raises ``pydantic.ValidationError`` on mutation
    # — we catch the generic ``Exception`` to avoid coupling to the
    # exact subclass while still asserting immutability.
    try:
        err.message = "different"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("FabricValidationError should be frozen")


# ---------------------------------------------------------------------------
# Clean path — full registry, all references resolve
# ---------------------------------------------------------------------------


def test_lease_with_clean_registry_returns_empty() -> None:
    """lease-renewal-v2 + registry that knows everything → no errors."""
    template = _load_lease_template()
    errors = validate_template_with_registry(template, _clean_lease_registry())
    assert errors == []


# ---------------------------------------------------------------------------
# state.entity_type unknown
# ---------------------------------------------------------------------------


def test_unknown_primary_entity_type_flagged_when_dots_used() -> None:
    """If the template references joined entities but the registry
    doesn't know the primary type, that's a registered-tier error."""
    template = _load_lease_template()
    registry = _MockRegistry(entities=set(), links=set())
    errors = validate_template_with_registry(template, registry)
    # At least one error pointing at state.entity_type.
    entity_errors = [e for e in errors if e.path == "state.entity_type"]
    assert len(entity_errors) == 1
    assert "Lease" in entity_errors[0].message


# ---------------------------------------------------------------------------
# joined_entities errors — unknown type + missing link
# ---------------------------------------------------------------------------


def test_unknown_joined_entity_type_flagged() -> None:
    """Registry knows the primary but not one of the joined entity
    types → typed error per missing join."""
    template = _load_lease_template()
    registry = _MockRegistry(
        entities={"Lease", "Property"},  # missing Tenant
        links={("Lease", "Property", "lease_property")},
    )
    errors = validate_template_with_registry(template, registry)
    messages = [e.message for e in errors]
    # Exactly one ``unknown joined entity_type: Tenant`` style error.
    assert any("Tenant" in m and "joined" in m.lower() for m in messages)


def test_unregistered_via_link_flagged() -> None:
    """Registry knows both ends but not the link between them."""
    template = _load_lease_template()
    registry = _MockRegistry(
        entities={"Lease", "Tenant", "Property"},
        links={("Lease", "Property", "lease_property")},  # missing lease_tenant
    )
    errors = validate_template_with_registry(template, registry)
    link_errors = [e for e in errors if "lease_tenant" in e.message]
    assert len(link_errors) == 1
    msg = link_errors[0].message
    assert "Lease" in msg
    assert "Tenant" in msg


def test_multiple_errors_collected_in_single_call() -> None:
    """Validator returns a *list* — it doesn't raise — so the CLI can
    render every problem at once."""
    template = _load_lease_template()
    registry = _MockRegistry(entities={"Lease"}, links=set())
    errors = validate_template_with_registry(template, registry)
    # Expect at least: Tenant unknown, Property unknown, lease_tenant
    # missing, lease_property missing. Four typed errors minimum.
    assert len(errors) >= 4
    # Each error carries a structured path so callers can sort / group.
    paths = {e.path for e in errors}
    assert any(p.startswith("state.joined_entities") for p in paths)


# ---------------------------------------------------------------------------
# CEL identifier collection — undeclared dotted roots in expressions
# ---------------------------------------------------------------------------


def test_undeclared_dot_path_in_cel_filter_flagged() -> None:
    """A saved_view filter references ``vendor.foo`` but no
    ``vendor`` join is declared → flagged as an undeclared dot-path."""
    template = PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "vendor-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "procurement",
            "display_name": "Vendor Test",
            "description": "Fixture with an undeclared dot-path.",
            "shape": "data-grid",
            "state": {
                "entity_type": "PurchaseOrder",
                "columns": [{"field": "id", "widget": "text"}],
                "saved_views": [
                    {"name": "Vendor Alpha", "filter": "vendor.foo == 1"},
                ],
            },
        }
    )
    registry = _MockRegistry(entities={"PurchaseOrder"}, links=set())
    errors = validate_template_with_registry(template, registry)
    # Find the dot-path lint error — message names ``vendor``.
    dot_errors = [e for e in errors if "vendor" in e.message.lower()]
    assert len(dot_errors) >= 1


def test_declared_dot_path_in_cel_filter_passes() -> None:
    """Same shape as above but with the join declared — passes."""
    template = PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "vendor-test-ok",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "procurement",
            "display_name": "Vendor Test OK",
            "description": "Fixture with a declared dot-path.",
            "shape": "data-grid",
            "state": {
                "entity_type": "PurchaseOrder",
                "joined_entities": [
                    {"name": "vendor", "entity_type": "Vendor", "via_link": "po_vendor"},
                ],
                "columns": [{"field": "id", "widget": "text"}],
                "saved_views": [
                    {"name": "Vendor Alpha", "filter": "vendor.foo == 1"},
                ],
            },
        }
    )
    registry = _MockRegistry(
        entities={"PurchaseOrder", "Vendor"},
        links={("PurchaseOrder", "Vendor", "po_vendor")},
    )
    errors = validate_template_with_registry(template, registry)
    assert errors == []


def test_undeclared_dot_path_in_instinct_rule_when_flagged() -> None:
    """Same lint applies to ``instinct_rules.rules[].when``."""
    template = PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "instinct-undecl",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "test",
            "display_name": "Instinct undecl",
            "description": "Fixture with an undeclared root in instinct rule.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [{"field": "id", "widget": "text"}],
            },
            "instinct_rules": {
                "rules": [{"when": "ghost.attribute == true", "action": "block"}],
            },
        }
    )
    registry = _MockRegistry(entities={"Thing"})
    errors = validate_template_with_registry(template, registry)
    assert any("ghost" in e.message.lower() for e in errors)


def test_undeclared_dot_path_in_trigger_when_flagged() -> None:
    """Same lint applies to ``triggers[].when``."""
    template = PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "trigger-undecl",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "test",
            "display_name": "Trigger undecl",
            "description": "Fixture with an undeclared root in a trigger.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [{"field": "id", "widget": "text"}],
            },
            "triggers": [
                {"type": "temporal", "when": "ghost.attribute == true"},
            ],
        }
    )
    registry = _MockRegistry(entities={"Thing"})
    errors = validate_template_with_registry(template, registry)
    assert any("ghost" in e.message.lower() for e in errors)


# ---------------------------------------------------------------------------
# Column field dot-paths — also gated
# ---------------------------------------------------------------------------


def test_undeclared_dot_path_in_column_field_flagged() -> None:
    """A column ``field`` like ``vendor.name`` is a dot-path too — the
    leading segment must be declared. (RFC 03 v2 lines 170-179.)"""
    template = PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "col-undecl",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "test",
            "display_name": "Column undecl",
            "description": "Column with an undeclared dot root.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [
                    {"field": "id", "widget": "text"},
                    {"field": "vendor.name", "widget": "text"},
                ],
            },
        }
    )
    registry = _MockRegistry(entities={"Thing"})
    errors = validate_template_with_registry(template, registry)
    assert any("vendor" in e.message.lower() for e in errors)


# ---------------------------------------------------------------------------
# Synthetic-tier passthrough — no dots, no enforcement
# ---------------------------------------------------------------------------


def test_synthetic_template_with_null_registry_returns_empty() -> None:
    """``todo-task-tracker`` is synthetic-tier: ``Task`` is not in any
    Fabric registry, the template has zero dot-paths. The validator
    must treat it as a passthrough — zero errors against
    ``NullFabricRegistry``."""
    template = _load_todo_template()
    errors = validate_template_with_registry(template, NullFabricRegistry())
    assert errors == []


def test_synthetic_template_with_empty_registry_returns_empty() -> None:
    """Same passthrough with a non-Null but empty mock registry."""
    template = _load_todo_template()
    errors = validate_template_with_registry(template, _MockRegistry())
    assert errors == []


# ---------------------------------------------------------------------------
# NullFabricRegistry behaviour for registered-tier shapes
# ---------------------------------------------------------------------------


def test_lease_with_null_registry_flags_everything() -> None:
    """``NullFabricRegistry`` knows nothing; a template that demands
    joins should surface errors. This is the right behaviour for the
    'no Fabric wired yet' default — silent passthrough would let a
    registered-tier template ship broken."""
    template = _load_lease_template()
    errors = validate_template_with_registry(template, NullFabricRegistry())
    # Many errors expected: primary entity_type, both joins, all
    # via_links, plus any dotted CEL roots.
    assert len(errors) >= 4


# ---------------------------------------------------------------------------
# Severity surface
# ---------------------------------------------------------------------------


def test_all_emitted_errors_are_error_severity() -> None:
    """PR 2g emits ``severity='error'`` for every miss — the validator
    has no warning path yet. Lock that in so a future tweak adding
    warnings is an intentional, reviewed change."""
    template = _load_lease_template()
    errors = validate_template_with_registry(template, NullFabricRegistry())
    for e in errors:
        assert e.severity == "error"


# ---------------------------------------------------------------------------
# Data payload — callers can sort / group / link to docs
# ---------------------------------------------------------------------------


def test_errors_carry_structured_data_payload() -> None:
    """Each error should carry a ``data`` dict the CLI / IDE can use to
    surface remediation hints — at minimum, the offending name."""
    template = _load_lease_template()
    registry = _MockRegistry(entities={"Lease"}, links=set())
    errors = validate_template_with_registry(template, registry)
    assert any(isinstance(e.data, dict) and e.data for e in errors)


# ---------------------------------------------------------------------------
# Repeat calls are pure
# ---------------------------------------------------------------------------


def test_validator_is_pure() -> None:
    """Re-running the validator with the same inputs returns the same
    output — no hidden state on the registry, template, or function."""
    template = _load_lease_template()
    reg = _clean_lease_registry()
    first: list[Any] = validate_template_with_registry(template, reg)
    second: list[Any] = validate_template_with_registry(template, reg)
    assert first == second
