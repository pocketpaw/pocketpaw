# tests/unit/test_identifier_resolver.py
# Created: 2026-05-28 (feat/rfc-03-v2-cel-eval) — unit tests for the
# ``IdentifierResolver`` Protocol's reference implementation
# (``TemplateIdentifierResolver``). The Protocol itself is checked by
# structural typing; only the reference impl carries observable
# behavior.
"""Tests for ``TemplateIdentifierResolver``.

The reference resolver covers two cases:

* Flat identifier (no dot) → look up directly in the context.
* Dotted identifier — the leading segment must match a
  ``state.joined_entities[].name`` declared on the template. If it
  does, drill into the context at that key. If not, raise
  ``KeyError`` with a clear message.

Fabric live-link traversal (calling out to a registered FabricLink
when the context lacks the joined entity inline) is out of scope for
PR 2c and lives in PR 2g.
"""

from __future__ import annotations

import pytest

from pocketpaw.bundled_templates import (
    PocketTemplate,
    TemplateIdentifierResolver,
)


def _template_with_tenant_join() -> PocketTemplate:
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "resolver-test",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "property-management",
            "display_name": "Resolver Test",
            "description": "Skinny fixture for identifier-resolver tests.",
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


def _template_without_joins() -> PocketTemplate:
    return PocketTemplate.model_validate(
        {
            "schema_version": "2",
            "name": "no-joins",
            "version": "1.0.0",
            "pattern": "app",
            "vertical": "general",
            "display_name": "No Joins",
            "description": "Skinny fixture without joined entities.",
            "shape": "data-grid",
            "state": {
                "entity_type": "Thing",
                "columns": [{"field": "id", "widget": "text"}],
            },
        }
    )


def test_flat_identifier_resolves_against_context() -> None:
    resolver = TemplateIdentifierResolver(_template_with_tenant_join())
    assert resolver.resolve("days_remaining", {"days_remaining": 25}) == 25


def test_flat_identifier_missing_raises_keyerror() -> None:
    resolver = TemplateIdentifierResolver(_template_with_tenant_join())
    with pytest.raises(KeyError):
        resolver.resolve("days_remaining", {})


def test_dotted_path_resolves_when_join_declared() -> None:
    resolver = TemplateIdentifierResolver(_template_with_tenant_join())
    # The resolver returns the top-level joined object — the CEL
    # evaluator (or any caller) then walks the rest of the path.
    context = {"tenant": {"name": "alice", "late_payment_count_12mo": 4}}
    value = resolver.resolve("tenant", context)
    assert value == {"name": "alice", "late_payment_count_12mo": 4}


def test_dotted_root_undeclared_raises_keyerror() -> None:
    resolver = TemplateIdentifierResolver(_template_with_tenant_join())
    # ``vendor`` is not a declared joined entity AND is not in the
    # context — should raise KeyError. The error message should name
    # ``vendor`` so the surrounding evaluator can surface it.
    with pytest.raises(KeyError) as exc:
        resolver.resolve("vendor", {})
    assert "vendor" in str(exc.value)


def test_undeclared_root_with_context_resolves_to_value() -> None:
    """If the row context carries ``vendor`` (e.g. an unjoined flat
    field that happens to share a name authors might use as a
    dotted root), the resolver returns the value. Whether the CEL
    expression *should* be using ``vendor`` at all is a lint-time
    concern (PR 2g's Fabric ``tier: registered`` enforcement) — at
    runtime, an entry in the row context is a legitimate lookup.

    This codifies the open boundary between PR 2c (runtime, loose)
    and PR 2g (lint, strict). Other resolvers (e.g. ``FabricResolver``
    in PR 2g) may layer extra gates.
    """
    resolver = TemplateIdentifierResolver(_template_without_joins())
    assert resolver.resolve("vendor", {"vendor": {"foo": 1}}) == {"foo": 1}


def test_property_join_also_resolves() -> None:
    """Both declared joins resolve. Sanity check that the lookup is
    not hardcoded to ``tenant``."""
    resolver = TemplateIdentifierResolver(_template_with_tenant_join())
    context = {"property": {"address": "123 Main St"}}
    assert resolver.resolve("property", context) == {"address": "123 Main St"}


def test_state_binding_constructor_overload() -> None:
    """The resolver should also accept a ``StateBinding`` directly —
    useful when the caller already has the inner block and doesn't
    want to round-trip through the full template."""
    template = _template_with_tenant_join()
    resolver = TemplateIdentifierResolver(template.state)
    context = {"tenant": {"name": "alice"}}
    assert resolver.resolve("tenant", context) == {"name": "alice"}
