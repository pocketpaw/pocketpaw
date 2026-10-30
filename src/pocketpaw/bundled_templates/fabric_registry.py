# src/pocketpaw/bundled_templates/fabric_registry.py
# Created: 2026-05-28 (feat/rfc-03-v2-fabric) — Protocol for the Fabric
# ObjectType / FabricLink registry. RFC 03 v2's ``tier: registered``
# enforcement gates schema references through this Protocol; the
# concrete registry lives in ``ee/fabric/`` and is supplied at runtime
# wiring time. PR 2g ships the OSS-side library + the ``NullFabricRegistry``
# no-op default; the EE-side concrete implementation is a separate,
# out-of-tree change.
"""Protocol seam between RFC 03 v2 OSS-side enforcement and the EE
Fabric ObjectType registry.

The pattern
-----------

PocketPaw is an open-core project. The Fabric ObjectType / FabricLink
registry — the source of truth for which entity types exist and which
links connect them — lives in the enterprise package (``ee/fabric/``)
and is not part of the OSS wheel. The OSS schema layer still needs to
ask *some* registry "does this entity type exist? does this link
exist?" so RFC 03 v2's ``tier: registered`` enforcement can fail
templates loudly at lint / runtime time.

The :class:`FabricRegistry` Protocol is that seam:

* OSS code (the validator and the :class:`FabricResolver`) **consumes**
  the Protocol — never imports a concrete registry.
* EE code provides a concrete implementation that walks the live
  ObjectType graph.
* For OSS-only environments (developer machines, library tests,
  CLI ``template lint`` with no Fabric wired), :class:`NullFabricRegistry`
  is a no-op default that returns ``False`` for every query. Templates
  that don't declare joins (synthetic-tier) stay clean; templates that
  declare joins get flagged — which is the right behaviour, since you
  can't satisfy ``tier: registered`` without a registry to register
  against.

Surface area
------------

The Protocol is deliberately minimal. Three methods cover every check
RFC 03 v2's library layer needs today:

* :meth:`entity_type_exists` — does the registry know ``name``?
* :meth:`link_exists` — does a link ``link_name`` connect ``from_type``
  to ``to_type``?
* :meth:`get_entity_properties` — which property names does the entity
  type declare? (Reserved for a future property-level lint that PR 2g
  does not enable yet — it ships so EE implementations can populate
  the data without another Protocol change later.)

Adding methods is a Protocol change. Keep the surface small until a
real consumer needs more.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FabricRegistry(Protocol):
    """Read-only view of the Fabric ObjectType / FabricLink registry.

    Implementations are supplied at wiring time. The OSS-side
    :func:`validate_template_with_registry` and :class:`FabricResolver`
    both consume the Protocol but never import a concrete class.

    The Protocol is :func:`typing.runtime_checkable` so callers can
    assert ``isinstance(reg, FabricRegistry)`` in tests / wiring code
    without pulling in a concrete dependency.
    """

    def entity_type_exists(self, name: str) -> bool:
        """Return True iff the registry knows an ObjectType called
        ``name``."""
        ...

    def link_exists(self, from_type: str, to_type: str, link_name: str) -> bool:
        """Return True iff a FabricLink called ``link_name`` connects
        ``from_type`` -> ``to_type``."""
        ...

    def get_entity_properties(self, name: str) -> set[str]:
        """Return the declared property names of the ObjectType
        ``name``. Implementations that don't track properties may
        return an empty set; PR 2g does not require property-level
        lint, so the validator tolerates ``set()`` everywhere."""
        ...


class NullFabricRegistry:
    """No-op :class:`FabricRegistry` — the default when no Fabric is
    wired.

    Every query returns ``False`` / ``set()``. Consequences:

    * Synthetic-tier templates (no dot-paths, no joined_entities) lint
      clean — there is nothing to register, so a "miss" is not raised.
    * Registered-tier templates fail every check. That is the correct
      diagnostic: a template that demands joins cannot satisfy
      ``tier: registered`` without a real registry behind it.

    Using ``NullFabricRegistry`` as a placeholder lets the rest of the
    stack (CLI, runtime composer, tests) keep a single Protocol type
    in their signatures regardless of whether Fabric is wired.
    """

    __slots__ = ()

    def entity_type_exists(self, name: str) -> bool:  # noqa: ARG002
        return False

    def link_exists(  # noqa: ARG002
        self,
        from_type: str,
        to_type: str,
        link_name: str,
    ) -> bool:
        return False

    def get_entity_properties(self, name: str) -> set[str]:  # noqa: ARG002
        return set()


__all__ = [
    "FabricRegistry",
    "NullFabricRegistry",
]
