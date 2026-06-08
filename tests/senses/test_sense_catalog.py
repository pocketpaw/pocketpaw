# Sense catalog tests — vocabulary validation, static index, and anti-drift guard.
# Created: 2026-06-08 — RFC Sense tier chunk 1.
# The guard test (test_every_core_sense_has_a_connector) is the anti-drift
# rule: every core sense MUST be backed by at least one connector YAML, or the
# vocabulary and the connector catalog have drifted apart.

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.connectors.yaml_engine import ConnectorDef, parse_connector_yaml
from pocketpaw.senses import (
    CORE_SENSES,
    SenseValidationError,
    connectors_for_sense,
    is_core_sense,
    validate_sense_id,
)

# Repo-root connectors/ directory (worktree root is two parents up from this file).
CONNECTORS_DIR = Path(__file__).resolve().parents[2] / "connectors"


def _all_connector_defs() -> list[ConnectorDef]:
    """Parse every connector YAML in the repo (same path the registry scans)."""
    defs: list[ConnectorDef] = []
    for path in sorted(CONNECTORS_DIR.glob("*.yaml")):
        defs.append(parse_connector_yaml(path))
    return defs


# --- validate_sense_id ------------------------------------------------------


def test_core_ids_are_accepted():
    for sense in CORE_SENSES:
        assert validate_sense_id(sense.id) == sense.id
        assert is_core_sense(sense.id)


def test_unknown_paw_id_is_rejected():
    with pytest.raises(SenseValidationError):
        validate_sense_id("paw.crm.v1")
    assert not is_core_sense("paw.crm.v1")


def test_malformed_paw_id_is_rejected():
    with pytest.raises(SenseValidationError):
        validate_sense_id("paw.email")  # missing version
    with pytest.raises(SenseValidationError):
        validate_sense_id("paw.Email.v1")  # uppercase


def test_vendor_extension_id_is_accepted_freely():
    assert validate_sense_id("acme.crm.v1") == "acme.crm.v1"
    assert validate_sense_id("salesforce.leads.v2") == "salesforce.leads.v2"
    assert not is_core_sense("acme.crm.v1")


def test_malformed_extension_id_is_rejected():
    with pytest.raises(SenseValidationError):
        validate_sense_id("justone")
    with pytest.raises(SenseValidationError):
        validate_sense_id("acme.crm")  # missing version


def test_empty_id_is_rejected():
    with pytest.raises(SenseValidationError):
        validate_sense_id("")


# --- connectors_for_sense ---------------------------------------------------


def test_connectors_for_sense_returns_matching_names():
    defs = [
        ConnectorDef(name="gmail", display_name="Gmail", senses=["paw.email.v1"]),
        ConnectorDef(name="github", display_name="GitHub", senses=["paw.code.v1"]),
        ConnectorDef(name="airtable", display_name="Airtable"),  # no senses
    ]
    assert connectors_for_sense("paw.email.v1", defs) == ["gmail"]
    assert connectors_for_sense("paw.code.v1", defs) == ["github"]
    assert connectors_for_sense("paw.payments.v1", defs) == []


def test_connectors_for_sense_handles_multi_sense_connector():
    defs = [
        ConnectorDef(
            name="gworkspace",
            display_name="Google Workspace",
            senses=["paw.email.v1", "paw.calendar.v1", "paw.docs.v1"],
        ),
        ConnectorDef(name="gmail", display_name="Gmail", senses=["paw.email.v1"]),
    ]
    assert connectors_for_sense("paw.email.v1", defs) == ["gmail", "gworkspace"]
    assert connectors_for_sense("paw.calendar.v1", defs) == ["gworkspace"]
    assert connectors_for_sense("paw.docs.v1", defs) == ["gworkspace"]


def test_connectors_with_no_senses_key_default_to_empty():
    defs = [ConnectorDef(name="airtable", display_name="Airtable")]
    assert defs[0].senses == []
    assert connectors_for_sense("paw.email.v1", defs) == []


# --- anti-drift guard -------------------------------------------------------


def test_every_core_sense_has_a_connector():
    """Every core sense must be declared by at least one connector YAML."""
    defs = _all_connector_defs()
    assert defs, "no connector YAMLs found — check CONNECTORS_DIR"
    for sense in CORE_SENSES:
        matches = connectors_for_sense(sense.id, defs)
        assert matches, (
            f"core sense {sense.id!r} has no connector declaring it; "
            f"either annotate a connector YAML or drop it from CORE_SENSES"
        )


def test_all_declared_senses_validate():
    """Every senses entry across all connector YAMLs must be a valid id."""
    for path in sorted(CONNECTORS_DIR.glob("*.yaml")):
        # parse_connector_yaml validates at parse time; a bad id would raise.
        parse_connector_yaml(path)
