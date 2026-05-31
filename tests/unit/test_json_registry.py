# tests/unit/test_json_registry.py
# Created: 2026-05-28 (feat/wave-4b-lint-fabric) — RED-first tests for
# ``JSONFileFabricRegistry``, the OSS-side mock that lets developers
# lint ``tier: registered`` templates against a synthetic entity-type +
# link manifest without standing up the EE Fabric backend.
"""Tests for ``pocketpaw.bundled_templates.json_registry``.

The registry implements the :class:`FabricRegistry` Protocol from a
JSON manifest on disk. Wave 4b ships it as the ``--registry <path>``
override for ``pocketpaw template lint``. Wave 4c will replace it with
the live EE FabricRegistry inside the enterprise path.

Coverage:

1. Round-trip — a manifest with entities, links, and properties loads
   and answers every Protocol method correctly.
2. Defaults — missing top-level keys default to empty (so ``{}`` is a
   valid, no-op registry).
3. Malformed JSON / missing file — raises a typed error subclassing
   :class:`ValueError` so the lint CLI can catch alongside its own
   parse errors.
4. Protocol conformance — the class satisfies
   ``isinstance(reg, FabricRegistry)`` at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pocketpaw.bundled_templates import FabricRegistry, JSONFileFabricRegistry
from pocketpaw.bundled_templates.json_registry import JSONFileFabricRegistryError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _full_manifest() -> dict:
    return {
        "entity_types": ["Lease", "Tenant", "Property"],
        "links": [
            {"from": "Lease", "to": "Tenant", "name": "lease_tenant"},
            {"from": "Lease", "to": "Property", "name": "lease_property"},
        ],
        "entity_properties": {
            "Lease": ["expiry_date", "rent_current", "rent_proposed", "renewal_stage"],
            "Tenant": ["name", "email", "late_payment_count_12mo"],
        },
    }


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_load_full_manifest_round_trips(tmp_path: Path) -> None:
    path = _write(tmp_path / "fabric.json", _full_manifest())
    reg = JSONFileFabricRegistry(path)

    assert reg.entity_type_exists("Lease") is True
    assert reg.entity_type_exists("Tenant") is True
    assert reg.entity_type_exists("Property") is True
    assert reg.entity_type_exists("Ghost") is False

    assert reg.link_exists("Lease", "Tenant", "lease_tenant") is True
    assert reg.link_exists("Lease", "Property", "lease_property") is True
    assert reg.link_exists("Lease", "Tenant", "wrong_name") is False
    assert reg.link_exists("Tenant", "Lease", "lease_tenant") is False  # direction matters
    assert reg.link_exists("Ghost", "Tenant", "lease_tenant") is False

    assert reg.get_entity_properties("Lease") == {
        "expiry_date",
        "rent_current",
        "rent_proposed",
        "renewal_stage",
    }
    assert reg.get_entity_properties("Tenant") == {
        "name",
        "email",
        "late_payment_count_12mo",
    }
    # Unknown entity → empty set, matching NullFabricRegistry.
    assert reg.get_entity_properties("Ghost") == set()


def test_runtime_protocol_check(tmp_path: Path) -> None:
    """``isinstance(reg, FabricRegistry)`` must succeed at runtime."""
    path = _write(tmp_path / "fabric.json", _full_manifest())
    reg = JSONFileFabricRegistry(path)
    assert isinstance(reg, FabricRegistry)


# ---------------------------------------------------------------------------
# Defaults — partial manifests
# ---------------------------------------------------------------------------


def test_missing_entity_types_key_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(tmp_path / "fabric.json", {"links": [], "entity_properties": {}})
    reg = JSONFileFabricRegistry(path)
    assert reg.entity_type_exists("Lease") is False
    # Other methods still work — properties returns empty, link returns False.
    assert reg.get_entity_properties("Lease") == set()
    assert reg.link_exists("Lease", "Tenant", "x") is False


def test_missing_links_key_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(tmp_path / "fabric.json", {"entity_types": ["Lease"]})
    reg = JSONFileFabricRegistry(path)
    assert reg.entity_type_exists("Lease") is True
    assert reg.link_exists("Lease", "Tenant", "lease_tenant") is False


def test_missing_entity_properties_key_defaults_to_empty(tmp_path: Path) -> None:
    path = _write(tmp_path / "fabric.json", {"entity_types": ["Lease"]})
    reg = JSONFileFabricRegistry(path)
    assert reg.get_entity_properties("Lease") == set()


def test_empty_object_manifest_loads_cleanly(tmp_path: Path) -> None:
    """``{}`` is a valid manifest — yields a no-op registry equivalent
    to :class:`NullFabricRegistry`."""
    path = _write(tmp_path / "fabric.json", {})
    reg = JSONFileFabricRegistry(path)
    assert reg.entity_type_exists("Anything") is False
    assert reg.link_exists("A", "B", "x") is False
    assert reg.get_entity_properties("X") == set()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_file_raises_typed_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(JSONFileFabricRegistryError) as excinfo:
        JSONFileFabricRegistry(missing)
    # ValueError-compatible so the lint CLI can catch it alongside its
    # own parse errors.
    assert isinstance(excinfo.value, ValueError)
    assert "nope.json" in str(excinfo.value)


def test_malformed_json_raises_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ not: valid, json", encoding="utf-8")
    with pytest.raises(JSONFileFabricRegistryError) as excinfo:
        JSONFileFabricRegistry(path)
    assert isinstance(excinfo.value, ValueError)


def test_non_object_top_level_raises(tmp_path: Path) -> None:
    """Top-level array / scalar is not a valid manifest shape."""
    path = tmp_path / "bad.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(JSONFileFabricRegistryError):
        JSONFileFabricRegistry(path)


def test_malformed_link_entry_raises(tmp_path: Path) -> None:
    """A link entry missing ``from``/``to``/``name`` is a hard error so
    typos surface at lint time rather than silently flipping a check
    to ``False``."""
    path = _write(
        tmp_path / "fabric.json",
        {"entity_types": ["Lease"], "links": [{"from": "Lease", "to": "Tenant"}]},
    )
    with pytest.raises(JSONFileFabricRegistryError):
        JSONFileFabricRegistry(path)


# ---------------------------------------------------------------------------
# Plays nicely with the validator
# ---------------------------------------------------------------------------


def test_full_manifest_lints_lease_fixture_clean(tmp_path: Path) -> None:
    """Loaded against the lease fixture, the full manifest produces no
    Fabric errors — proving the OSS-side mock can stand in for an EE
    Fabric registry."""
    import yaml

    from pocketpaw.bundled_templates import (
        PocketTemplate,
        validate_template_with_registry,
    )

    path = _write(tmp_path / "fabric.json", _full_manifest())
    reg = JSONFileFabricRegistry(path)

    lease_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "templates" / "lease-renewal-v2.yaml"
    )
    template = PocketTemplate.model_validate(yaml.safe_load(lease_path.read_text()))
    errors = validate_template_with_registry(template, reg)
    assert errors == []


def test_partial_manifest_surfaces_only_unknown_link(tmp_path: Path) -> None:
    """Manifest knows the entity types but not the via_link → the
    validator emits the expected unregistered-link errors and nothing
    else."""
    import yaml

    from pocketpaw.bundled_templates import (
        PocketTemplate,
        validate_template_with_registry,
    )

    path = _write(
        tmp_path / "fabric.json",
        {"entity_types": ["Lease", "Tenant", "Property"], "links": []},
    )
    reg = JSONFileFabricRegistry(path)

    lease_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "templates" / "lease-renewal-v2.yaml"
    )
    template = PocketTemplate.model_validate(yaml.safe_load(lease_path.read_text()))
    errors = validate_template_with_registry(template, reg)

    # Two joined entities both fail via_link — but entity_type checks
    # pass (we declared them above).
    via_link_errors = [e for e in errors if "via_link" in e.path]
    assert len(via_link_errors) == 2
    entity_type_errors = [e for e in errors if e.path.endswith(".entity_type")]
    assert entity_type_errors == []
