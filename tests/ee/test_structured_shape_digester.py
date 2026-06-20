# tests/ee/test_structured_shape_digester.py — unit tests for SZD-3.
#
# Created: 2026-06-19 (SZD-3 / feat/szd-3-digester) — covers StructuredShapeDigester:
#   * object types + property types inferred from sample records;
#   * a stable, named primary key with high confidence;
#   * links inferred from foreign-key shapes between two types;
#   * the OntologyDraft round-trips into a fabric_ingest.FabricMapping;
#   * clean degradation: empty records, nested/polymorphic records, no clear key.
#
# Pure-logic tests — no DB / network / async. Run with:
#   uv run --group ee pytest tests/ee/test_structured_shape_digester.py -q

from __future__ import annotations

from datetime import datetime

import pytest
from pocketpaw_ee.discovery import OntologyDraft, StructuredShapeDigester

from pocketpaw.connectors.fabric_ingest import FabricMapping


@pytest.fixture
def digester() -> StructuredShapeDigester:
    return StructuredShapeDigester()


# --------------------------------------------------------------------------- #
# Object types + property types
# --------------------------------------------------------------------------- #
def test_infers_types_and_property_types(digester: StructuredShapeDigester) -> None:
    records = {
        "Customer": [
            {"id": "c1", "name": "Acme", "revenue": 1000, "active": True, "signed": "2026-01-02"},
            {
                "id": "c2",
                "name": "Globex",
                "revenue": 2500.5,
                "active": False,
                "signed": "2026-02-15",
            },
        ]
    }
    draft = digester.digest(records, {"connector": "demo"})

    assert [ot.name for ot in draft.object_types] == ["Customer"]
    ot = draft.type_by_name("Customer")
    assert ot is not None
    props = {p.name: p.type for p in ot.properties}
    assert props["id"] == "string"
    assert props["name"] == "string"
    assert props["revenue"] == "number"  # int + float both vote number
    assert props["active"] == "boolean"
    assert props["signed"] == "date"  # ISO-ish string detected as date

    # required = present non-null in every record
    required = {p.name: p.required for p in ot.properties}
    assert required["id"] is True
    assert required["name"] is True


def test_native_datetime_typed_as_date(digester: StructuredShapeDigester) -> None:
    records = {"Event": [{"id": "e1", "at": datetime(2026, 6, 19, 22, 0, 0)}]}
    draft = digester.digest(records)
    ot = draft.type_by_name("Event")
    assert ot is not None
    assert {p.name: p.type for p in ot.properties}["at"] == "date"


# --------------------------------------------------------------------------- #
# Primary key inference
# --------------------------------------------------------------------------- #
def test_named_id_field_is_primary_key_high_confidence(
    digester: StructuredShapeDigester,
) -> None:
    records = {
        "Order": [
            {"id": "o1", "total": 10},
            {"id": "o2", "total": 20},
            {"id": "o3", "total": 30},
        ]
    }
    draft = digester.digest(records)
    ot = draft.type_by_name("Order")
    assert ot is not None
    assert ot.source_id_field == "id"
    assert ot.key_confidence >= 0.9  # named, unique, fully populated


def test_type_prefixed_id_field_preferred(digester: StructuredShapeDigester) -> None:
    # No plain "id" — the <type>_id field should win.
    records = {
        "Customer": [
            {"customer_id": "c1", "name": "A"},
            {"customer_id": "c2", "name": "B"},
        ]
    }
    draft = digester.digest(records)
    ot = draft.type_by_name("Customer")
    assert ot is not None
    assert ot.source_id_field == "customer_id"
    assert ot.key_confidence >= 0.8


def test_high_cardinality_unique_field_fallback_key(
    digester: StructuredShapeDigester,
) -> None:
    # No id-named field; "email" is unique + fully populated → fallback key.
    records = {
        "Person": [
            {"email": "a@x.com", "city": "NY"},
            {"email": "b@x.com", "city": "NY"},
            {"email": "c@x.com", "city": "LA"},
        ]
    }
    draft = digester.digest(records)
    ot = draft.type_by_name("Person")
    assert ot is not None
    assert ot.source_id_field == "email"
    # fallback key is less certain than a named id
    assert 0.0 < ot.key_confidence < 0.9


def test_source_id_extracted_onto_objects(digester: StructuredShapeDigester) -> None:
    records = {"Order": [{"id": "o1", "x": 1}, {"id": "o2", "x": 2}]}
    draft = digester.digest(records)
    sids = sorted(o.source_id for o in draft.objects)
    assert sids == ["o1", "o2"]


# --------------------------------------------------------------------------- #
# Link inference
# --------------------------------------------------------------------------- #
def test_links_inferred_from_foreign_keys(digester: StructuredShapeDigester) -> None:
    records = {
        "Customer": [
            {"id": "c1", "name": "Acme"},
            {"id": "c2", "name": "Globex"},
        ],
        "Order": [
            {"id": "o1", "customer_id": "c1", "total": 10},
            {"id": "o2", "customer_id": "c1", "total": 20},
            {"id": "o3", "customer_id": "c2", "total": 30},
        ],
    }
    draft = digester.digest(records)

    # Three orders, each linking to its customer.
    cust_links = [lk for lk in draft.links if lk.from_type == "Order" and lk.to_type == "Customer"]
    assert len(cust_links) == 3
    pairs = {(lk.from_source_id, lk.to_source_id) for lk in cust_links}
    assert pairs == {("o1", "c1"), ("o2", "c1"), ("o3", "c2")}
    # FK field name → link_type + via_field captured
    assert all(lk.via_field == "customer_id" for lk in cust_links)
    assert all(lk.link_type == "belongs_to_customer" for lk in cust_links)
    # name + coverage signal → reasonably high confidence
    assert all(lk.confidence >= 0.6 for lk in cust_links)


def test_no_links_when_no_foreign_keys(digester: StructuredShapeDigester) -> None:
    records = {
        "Customer": [{"id": "c1"}, {"id": "c2"}],
        "Product": [{"id": "p1"}, {"id": "p2"}],
    }
    draft = digester.digest(records)
    assert draft.links == []


# --------------------------------------------------------------------------- #
# FabricMapping round-trip — the draft must be directly usable by ingest
# --------------------------------------------------------------------------- #
def test_draft_type_builds_fabric_mapping(digester: StructuredShapeDigester) -> None:
    records = {
        "Invoice": [
            {"invoice_id": "i1", "amount": 100, "paid": True},
            {"invoice_id": "i2", "amount": 200, "paid": False},
        ]
    }
    draft = digester.digest(records)
    ot = draft.type_by_name("Invoice")
    assert ot is not None

    mapping = FabricMapping(**ot.to_fabric_mapping_kwargs())
    assert mapping.type_name == "Invoice"
    assert mapping.source_id_field == "invoice_id"

    # the mapping's own extract/project must work on the raw records
    rec = records["Invoice"][0]
    assert mapping.extract_source_id(rec) == "i1"
    projected = mapping.project(rec)
    assert projected["amount"] == 100
    assert projected["paid"] is True
    assert projected["invoice_id"] == "i1"


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #
def test_empty_records_yield_empty_draft(digester: StructuredShapeDigester) -> None:
    assert digester.digest({}).is_empty
    assert digester.digest([]).is_empty
    assert digester.digest(None).is_empty
    # mapping of empty lists also degrades to empty
    draft = digester.digest({"Customer": []})
    assert draft.is_empty
    assert draft.meta.get("degraded") == "empty"


def test_nested_polymorphic_records_no_crash(digester: StructuredShapeDigester) -> None:
    records = {
        "Thing": [
            {"id": "t1", "blob": {"nested": [1, 2, 3]}, "tags": ["a", "b"]},
            {"id": "t2", "blob": "now a string", "tags": None},
            {"id": "t3", "blob": 42},
        ]
    }
    draft = digester.digest(records)  # must not raise
    ot = draft.type_by_name("Thing")
    assert ot is not None
    assert ot.source_id_field == "id"
    # nested/polymorphic field still gets a type (string) and is projectable
    props = {p.name: p.type for p in ot.properties}
    assert "blob" in props
    assert len(draft.objects) == 3


def test_no_clear_key_objects_only_low_confidence(
    digester: StructuredShapeDigester,
) -> None:
    # No id-named field, no unique field — every value repeats.
    records = {
        "Log": [
            {"level": "info", "scope": "auth"},
            {"level": "info", "scope": "auth"},
            {"level": "warn", "scope": "auth"},
        ]
    }
    draft = digester.digest(records)
    ot = draft.type_by_name("Log")
    assert ot is not None
    assert ot.source_id_field is None  # no usable key
    assert ot.key_confidence < 0.3  # low confidence
    # objects still produced (objects-only), but with no source_id and no links
    assert len(draft.objects) == 3
    assert all(o.source_id is None for o in draft.objects)
    assert draft.links == []
    assert draft.meta.get("degraded") == "objects-only"


def test_flat_list_input_single_anonymous_type(
    digester: StructuredShapeDigester,
) -> None:
    records = [{"id": "r1", "v": 1}, {"id": "r2", "v": 2}]
    draft = digester.digest(records, {"default_type": "Widget"})
    assert [ot.name for ot in draft.object_types] == ["Widget"]
    assert draft.type_by_name("Widget").source_id_field == "id"


def test_returns_ontology_draft_type(digester: StructuredShapeDigester) -> None:
    draft = digester.digest({"X": [{"id": "1"}]})
    assert isinstance(draft, OntologyDraft)
