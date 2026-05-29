# tests/unit/test_fabric_resolver.py
# Created: 2026-05-28 (feat/rfc-03-v2-fabric) — unit tests for
# ``FabricResolver``, the strict :class:`IdentifierResolver`
# implementation that gates dotted paths against a ``FabricRegistry``.
# Companion to ``test_identifier_resolver.py`` (which covers the loose,
# template-only reference resolver shipped in PR 2c).
"""Tests for ``FabricResolver``.

The resolver layers two extra gates on top of the PR 2c reference
resolver:

* A dotted root must be declared in ``state.joined_entities[]`` AND
  registered in the supplied ``FabricRegistry`` (``link_exists`` returns
  True for the ``(state.entity_type, joined.entity_type, joined.via_link)``
  triple). Otherwise raise ``KeyError`` naming the unregistered link.
* Drilling into the resolved joined value still respects the row
  context: missing fields raise ``KeyError`` (same contract the CEL
  evaluator already understands).

Flat identifiers behave the same as the reference resolver.
"""

from __future__ import annotations

from typing import Any

import pytest

from pocketpaw.bundled_templates import (
    FabricRegistry,
    FabricResolver,
    NullFabricRegistry,
    PocketTemplate,
)

# ---------------------------------------------------------------------------
# Mock registries
# ---------------------------------------------------------------------------


class _MockRegistry:
    """Configurable in-memory :class:`FabricRegistry` for tests."""

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


def _lease_template() -> PocketTemplate:
    """Skinny lease template that declares two joins."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "fabric-resolver-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "property-management",
            "display_name": "Fabric Resolver Test",
            "description": "Fixture for FabricResolver tests.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Lease",
                "joined_entities": [
                    {"name": "tenant", "entity_type": "Tenant", "via_link": "lease_tenant"},
                    {
                        "name": "property",
                        "entity_type": "Property",
                        "via_link": "lease_property",
                    },
                ],
                "columns": [
                    {"field": "id", "widget": "text"},
                    {"field": "days_remaining", "widget": "trend"},
                ],
            },
        }
    )


def _full_registry() -> _MockRegistry:
    return _MockRegistry(
        entities={"Lease", "Tenant", "Property"},
        links={
            ("Lease", "Tenant", "lease_tenant"),
            ("Lease", "Property", "lease_property"),
        },
    )


# ---------------------------------------------------------------------------
# Protocol shape — the Mock satisfies the Protocol structurally
# ---------------------------------------------------------------------------


def test_mock_registry_satisfies_protocol() -> None:
    """A duck-typed registry implementing the three methods must satisfy
    the :class:`FabricRegistry` runtime-checkable Protocol."""
    registry: FabricRegistry = _full_registry()  # type-check assignment
    assert isinstance(registry, FabricRegistry)


def test_null_registry_satisfies_protocol() -> None:
    null: FabricRegistry = NullFabricRegistry()
    assert isinstance(null, FabricRegistry)


# ---------------------------------------------------------------------------
# Flat identifiers — same contract as TemplateIdentifierResolver
# ---------------------------------------------------------------------------


def test_flat_identifier_resolves_from_context() -> None:
    resolver = FabricResolver(_lease_template().state, _full_registry())
    assert resolver.resolve("days_remaining", {"days_remaining": 25}) == 25


def test_flat_identifier_missing_raises_keyerror() -> None:
    resolver = FabricResolver(_lease_template().state, _full_registry())
    with pytest.raises(KeyError):
        resolver.resolve("days_remaining", {})


# ---------------------------------------------------------------------------
# Dotted-root happy path — declared join + registered link
# ---------------------------------------------------------------------------


def test_declared_join_with_registered_link_resolves() -> None:
    resolver = FabricResolver(_lease_template().state, _full_registry())
    context = {"tenant": {"name": "alice", "late_payment_count_12mo": 4}}
    assert resolver.resolve("tenant", context) == {
        "name": "alice",
        "late_payment_count_12mo": 4,
    }


def test_both_declared_joins_resolve() -> None:
    """Sanity — the resolver is not hard-coded to ``tenant``."""
    resolver = FabricResolver(_lease_template().state, _full_registry())
    context = {"property": {"address": "123 Main St"}}
    assert resolver.resolve("property", context) == {"address": "123 Main St"}


# ---------------------------------------------------------------------------
# Strict failures — undeclared root, unregistered link, missing context
# ---------------------------------------------------------------------------


def _template_with_undeclared_column_root() -> PocketTemplate:
    """Template that uses ``vendor.name`` as a column dot-path but
    never declares a ``vendor`` join — the canonical
    ``tier: registered`` mismatch."""
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "undeclared-vendor",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "procurement",
            "display_name": "Undeclared Vendor",
            "description": "Fixture with an undeclared dotted root in columns.",
            "shape": "data-grid",
            "state": {
                "entity_type": "PurchaseOrder",
                "columns": [
                    {"field": "id", "widget": "text"},
                    {"field": "vendor.name", "widget": "text"},
                ],
            },
        }
    )


def test_undeclared_join_raises_keyerror_with_name() -> None:
    """``vendor`` appears as a column dotted root but isn't in
    ``joined_entities`` — strict resolver rejects even when the row
    context carries it. This is the canonical ``tier: registered``
    failure mode."""
    resolver = FabricResolver(
        _template_with_undeclared_column_root().state,
        _MockRegistry(entities={"PurchaseOrder"}),
    )
    with pytest.raises(KeyError) as exc:
        resolver.resolve("vendor", {"vendor": {"foo": 1}})
    assert "vendor" in str(exc.value)
    assert "undeclared" in str(exc.value).lower()


def test_truly_flat_identifier_falls_through_to_context() -> None:
    """If the identifier doesn't look like a dotted root (no matching
    join, no matching column dot-prefix), the resolver falls through
    to plain context lookup — matching PR 2c's loose runtime behaviour
    for free CEL helpers (e.g. ``within(some_property, duration)``)."""
    resolver = FabricResolver(_lease_template().state, _full_registry())
    assert resolver.resolve("expires_at", {"expires_at": 42}) == 42


def test_link_not_registered_raises_keyerror() -> None:
    """The join is declared on the template, but the registry says the
    ``via_link`` doesn't exist between ``Lease`` and ``Tenant``."""
    registry = _MockRegistry(
        entities={"Lease", "Tenant"},
        links=set(),  # nothing registered
    )
    resolver = FabricResolver(_lease_template().state, registry)
    context = {"tenant": {"name": "alice"}}
    with pytest.raises(KeyError) as exc:
        resolver.resolve("tenant", context)
    msg = str(exc.value).lower()
    assert "lease_tenant" in msg
    assert "via_link" in msg or "link" in msg


def test_declared_join_missing_from_context_raises() -> None:
    """Declared + registered, but the per-row context lacks the join
    payload. Resolver raises ``KeyError`` — the CEL evaluator surfaces
    it as a typed evaluation error."""
    resolver = FabricResolver(_lease_template().state, _full_registry())
    with pytest.raises(KeyError) as exc:
        resolver.resolve("tenant", {})
    assert "tenant" in str(exc.value)


def test_full_dot_path_passed_in_is_defensive_error() -> None:
    """Same defensive guard as the reference resolver — the evaluator
    only ever hands us leftmost segments. A full dot-path is a caller
    bug; surface it loudly."""
    resolver = FabricResolver(_lease_template().state, _full_registry())
    with pytest.raises(KeyError):
        resolver.resolve("tenant.name", {"tenant": {"name": "alice"}})


# ---------------------------------------------------------------------------
# NullFabricRegistry
# ---------------------------------------------------------------------------


def test_null_registry_rejects_every_dotted_root() -> None:
    """``NullFabricRegistry`` treats nothing as registered — any declared
    join will fail the ``link_exists`` check. This is the right
    behaviour for a no-Fabric default: a template that declares joins
    without a real registry to back them is broken, not silently
    permitted."""
    resolver = FabricResolver(_lease_template().state, NullFabricRegistry())
    with pytest.raises(KeyError):
        resolver.resolve("tenant", {"tenant": {"name": "alice"}})


def test_null_registry_passes_flat_identifiers() -> None:
    """Flat identifiers bypass the registry entirely — same passthrough
    semantics as the reference resolver."""
    resolver = FabricResolver(_lease_template().state, NullFabricRegistry())
    assert resolver.resolve("days_remaining", {"days_remaining": 25}) == 25


# ---------------------------------------------------------------------------
# Constructor overloads — PocketTemplate or StateBinding
# ---------------------------------------------------------------------------


def test_constructor_accepts_full_template() -> None:
    template = _lease_template()
    resolver = FabricResolver(template, _full_registry())
    assert resolver.resolve("tenant", {"tenant": {"name": "alice"}}) == {"name": "alice"}


def test_constructor_accepts_state_binding() -> None:
    template = _lease_template()
    resolver = FabricResolver(template.state, _full_registry())
    assert resolver.resolve("tenant", {"tenant": {"name": "alice"}}) == {"name": "alice"}


# ---------------------------------------------------------------------------
# Drop-in compatibility with the CEL evaluator
# ---------------------------------------------------------------------------


def test_works_with_evaluate_cel_for_declared_dotted_path() -> None:
    """End-to-end sanity: ``FabricResolver`` plugs into ``evaluate_cel``
    exactly where ``TemplateIdentifierResolver`` would. CEL expressions
    referencing declared joined entities evaluate correctly."""
    from pocketpaw.bundled_templates import evaluate_cel

    resolver = FabricResolver(_lease_template().state, _full_registry())
    context: dict[str, Any] = {"tenant": {"late_payment_count_12mo": 4}}
    result = evaluate_cel("tenant.late_payment_count_12mo >= 3", context, resolver)
    assert result is True


def test_works_with_evaluate_cel_for_flat_identifier() -> None:
    from pocketpaw.bundled_templates import evaluate_cel

    resolver = FabricResolver(_lease_template().state, _full_registry())
    result = evaluate_cel("days_remaining <= 30", {"days_remaining": 25}, resolver)
    assert result is True
