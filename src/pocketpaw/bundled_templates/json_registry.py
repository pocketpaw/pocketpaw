# src/pocketpaw/bundled_templates/json_registry.py
# Created: 2026-05-28 (feat/wave-4b-lint-fabric) — OSS-side
# :class:`FabricRegistry` implementation backed by a JSON manifest on
# disk. Wave 4b ships it as the ``--registry <path>`` override for
# ``pocketpaw template lint``. Wave 4c will replace it with the live EE
# FabricRegistry inside the enterprise path; until then, this is what
# lets a developer lint a ``tier: registered`` template without
# standing up the EE backend.
"""JSON-file backed :class:`FabricRegistry` for OSS lint workflows.

The class is a thin, immutable view over a hand-authored JSON manifest:

.. code-block:: json

    {
      "entity_types": ["Lease", "Tenant", "Property"],
      "links": [
        {"from": "Lease", "to": "Tenant", "name": "lease_tenant"},
        {"from": "Lease", "to": "Property", "name": "lease_property"}
      ],
      "entity_properties": {
        "Lease": ["expiry_date", "rent_current", "rent_proposed"],
        "Tenant": ["name", "email", "late_payment_count_12mo"]
      }
    }

Every top-level key defaults to empty when absent, so ``{}`` is a
valid, no-op manifest equivalent to :class:`NullFabricRegistry`. A
malformed file (missing the JSON top-level, malformed link entry,
unreadable I/O) raises :class:`JSONFileFabricRegistryError` — a
:class:`ValueError` subclass so the lint CLI can catch it alongside
its own template-parse errors.

The class is not registered as the default in any wiring. The CLI
constructs one only when the operator passes ``--registry <path>``;
otherwise lint runs against :class:`NullFabricRegistry`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JSONFileFabricRegistryError(ValueError):
    """Raised by :class:`JSONFileFabricRegistry` on a malformed manifest.

    Subclasses :class:`ValueError` so the lint CLI's existing
    ``ValueError`` catch (used for template-parse errors) also covers
    registry-load failures without a separate ``except`` clause.
    """


class JSONFileFabricRegistry:
    """A :class:`FabricRegistry` implementation backed by a JSON file.

    Loads the manifest once at construction time and answers every
    Protocol method from in-memory data structures — no I/O on the hot
    path. Instances are conceptually immutable; do not mutate the
    backing collections from outside.
    """

    __slots__ = ("_entities", "_links", "_properties", "_path")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entities: set[str] = set()
        self._links: set[tuple[str, str, str]] = set()
        self._properties: dict[str, set[str]] = {}
        self._load(path)

    # -- Protocol surface ----------------------------------------------------

    def entity_type_exists(self, name: str) -> bool:
        return name in self._entities

    def link_exists(self, from_type: str, to_type: str, link_name: str) -> bool:
        return (from_type, to_type, link_name) in self._links

    def get_entity_properties(self, name: str) -> set[str]:
        # Return a copy so external callers can't mutate the backing set.
        return set(self._properties.get(name, set()))

    # -- Internals -----------------------------------------------------------

    def _load(self, path: Path) -> None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JSONFileFabricRegistryError(
                f"failed to read Fabric registry file {path}: {exc}"
            ) from exc
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JSONFileFabricRegistryError(
                f"failed to parse Fabric registry file {path}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise JSONFileFabricRegistryError(
                f"{path}: expected a JSON object at top level, got {type(data).__name__}"
            )

        entity_types = data.get("entity_types", [])
        if not isinstance(entity_types, list):
            raise JSONFileFabricRegistryError(f"{path}: 'entity_types' must be a list of strings")
        for name in entity_types:
            if not isinstance(name, str):
                raise JSONFileFabricRegistryError(
                    f"{path}: 'entity_types' entries must be strings (got {name!r})"
                )
            self._entities.add(name)

        links = data.get("links", [])
        if not isinstance(links, list):
            raise JSONFileFabricRegistryError(f"{path}: 'links' must be a list of objects")
        for entry in links:
            if not isinstance(entry, dict):
                raise JSONFileFabricRegistryError(
                    f"{path}: each 'links' entry must be an object (got {type(entry).__name__})"
                )
            missing = [k for k in ("from", "to", "name") if k not in entry]
            if missing:
                raise JSONFileFabricRegistryError(
                    f"{path}: link entry {entry!r} is missing key(s): {', '.join(missing)}"
                )
            from_type = entry["from"]
            to_type = entry["to"]
            link_name = entry["name"]
            if not (
                isinstance(from_type, str)
                and isinstance(to_type, str)
                and isinstance(link_name, str)
            ):
                raise JSONFileFabricRegistryError(
                    f"{path}: link entry {entry!r} must have string 'from', 'to', 'name'"
                )
            self._links.add((from_type, to_type, link_name))

        props = data.get("entity_properties", {})
        if not isinstance(props, dict):
            raise JSONFileFabricRegistryError(
                f"{path}: 'entity_properties' must be an object mapping "
                "entity name -> list of strings"
            )
        for entity_name, prop_list in props.items():
            if not isinstance(entity_name, str):
                raise JSONFileFabricRegistryError(
                    f"{path}: 'entity_properties' keys must be strings"
                )
            if not isinstance(prop_list, list):
                raise JSONFileFabricRegistryError(
                    f"{path}: 'entity_properties[{entity_name}]' must be a list of strings"
                )
            prop_set: set[str] = set()
            for prop in prop_list:
                if not isinstance(prop, str):
                    raise JSONFileFabricRegistryError(
                        f"{path}: 'entity_properties[{entity_name}]' entries must be strings"
                    )
                prop_set.add(prop)
            self._properties[entity_name] = prop_set


__all__ = [
    "JSONFileFabricRegistry",
    "JSONFileFabricRegistryError",
]
