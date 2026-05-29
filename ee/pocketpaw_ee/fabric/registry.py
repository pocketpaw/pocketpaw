# ee/pocketpaw_ee/fabric/registry.py
# Created: 2026-05-28 (feat/wave-4c-fabric-registry) — concrete EE
# implementation of the ``pocketpaw.bundled_templates.FabricRegistry``
# Protocol. The class is a thin read-side wrapper that delegates every
# query to a ``WorkspaceFabricStore`` bound to a single workspace.
# Mutations stay on the store; the registry stays read-only by design
# so wiring code can keep one Protocol type in its signatures and the
# call-site stays simple (``WorkspaceFabricRegistry(store, ws_id)``).
"""Concrete ``FabricRegistry`` implementation backed by
:class:`WorkspaceFabricStore`.

The class is the final piece of RFC 03 v2's Fabric tier-registered
seam. ``pocketpaw.bundled_templates.fabric_registry.FabricRegistry``
declares the Protocol; Wave 4b shipped the OSS-side JSON mock; this
module ships the EE-side concrete implementation that satisfies the
same Protocol against real workspace data.

Why this is a read-only wrapper
-------------------------------

Two reasons.

1. The Protocol surface is intentionally read-only — three boolean /
   set-returning methods, no mutators. Keeping the concrete class
   read-only matches that contract exactly so callers that program
   against the Protocol can swap registries without behavioural
   surprises.
2. Writes have a richer surface (per-entity, per-property, per-link,
   cascade-delete) that doesn't fit a generic read seam. They live on
   :class:`WorkspaceFabricStore`. The split mirrors how
   ``pocketpaw_ee.cloud.<entity>`` separates service writes from
   read-mapping in the 4-file shape.

How to wire it
--------------

EE callers construct one per workspace and pass it to the Wave 4b
validator / runtime resolver:

.. code-block:: python

    store = WorkspaceFabricStore()  # defaults to ~/.pocketpaw/fabric_registry.db
    registry = WorkspaceFabricRegistry(store=store, workspace_id=ws_id)
    errors = validate_template_with_registry(template, registry)

The OSS lint CLI still defaults to ``NullFabricRegistry`` /
``JSONFileFabricRegistry``; an EE auto-wire hook can land in a separate
PR.
"""

from __future__ import annotations

from pocketpaw_ee.fabric.storage import WorkspaceFabricStore


class WorkspaceFabricRegistry:
    """Concrete ``FabricRegistry`` for a single workspace.

    Bound to a workspace at construction; every Protocol query
    delegates to the store with the bound ``workspace_id``. Two
    registries built against the same store but different workspace
    ids return disjoint views — multi-tenancy is enforced by the store,
    not the registry.
    """

    __slots__ = ("_store", "_workspace_id")

    def __init__(self, store: WorkspaceFabricStore, workspace_id: str) -> None:
        self._store = store
        self._workspace_id = workspace_id

    @property
    def workspace_id(self) -> str:
        """The workspace this registry is bound to. Exposed so wiring
        code can sanity-check the binding without poking ``_slots__``
        internals."""
        return self._workspace_id

    # -- FabricRegistry Protocol surface -------------------------------------

    def entity_type_exists(self, name: str) -> bool:
        return self._store.entity_exists(self._workspace_id, name)

    def link_exists(self, from_type: str, to_type: str, link_name: str) -> bool:
        return self._store.link_exists(self._workspace_id, link_name, from_type, to_type)

    def get_entity_properties(self, name: str) -> set[str]:
        # Store already returns a fresh set (set comprehension result),
        # so callers can mutate the returned value without affecting
        # the store. We return it directly to avoid a redundant copy.
        return self._store.get_properties(self._workspace_id, name)


__all__ = [
    "WorkspaceFabricRegistry",
]
